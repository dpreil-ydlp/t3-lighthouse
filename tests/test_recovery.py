from datetime import datetime, timezone, timedelta
import uuid
from psycopg.types.json import Jsonb

from conftest import make_project
from lighthouse.db import connection
from lighthouse.worker import advance_incidents, deliver_outbox, evaluate_stalls


def stale_task(client,headers,principals):
    project=make_project(client,headers,"recovery")
    controller=client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"policy":{"ack_deadline_seconds":60,"progress_deadline_seconds":5,"cooldown_seconds":1800,"retry_budget":2}}).json()
    client.put(f"/api/v1/controllers/{controller['controller_id']}/channel",headers={"Authorization":f"Bearer {principals['user'][0]}"},json={"channel":"test_sink"})
    thread=client.post(f"/api/v1/projects/{project['id']}/threads",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"source":"test_sink","external_thread_id":"test-controller-"+uuid.uuid4().hex,"state":"running"}).json()
    task=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"drill","thresholds":{"meaningful_progress_seconds":1}}).json()['task']
    now=datetime.now(timezone.utc)
    with connection() as conn: conn.execute("UPDATE tasks SET parent_thread_id=%s,status='running',last_meaningful_progress_at=%s WHERE id=%s",(thread['parent_thread_id'],now-timedelta(seconds=30),task['id']))
    return project,task,now


def open_incident(task,now):
    evaluate_stalls(now)
    with connection() as conn:
        return conn.execute("SELECT id FROM incidents WHERE task_id=%s ORDER BY opened_at DESC LIMIT 1",(task["id"],)).fetchone()["id"]


def test_ack_progress_resolves_and_no_ack_escalates(client,headers,principals):
    project,task,now=stale_task(client,headers,principals)
    incident_id=open_incident(task,now)
    with connection() as conn:
        initial=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident_id,)).fetchone()
    assert initial['state']=='open'
    deliver_outbox()
    with connection() as conn:
        incident=conn.execute("SELECT * FROM incidents WHERE id=%s",(incident_id,)).fetchone()
        outbox=conn.execute("SELECT * FROM notification_outbox WHERE incident_id=%s",(incident_id,)).fetchone()
    assert incident['state']=='awaiting_ack' and outbox['delivered_at'] is not None
    ack=client.post(f"/api/v1/incidents/{incident_id}/ack",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert ack.status_code==200
    assert client.get(f"/api/v1/incidents/{incident_id}",headers=headers).json()["incident"]["progress_deadline"] is not None
    progress_at=datetime.now(timezone.utc)+timedelta(seconds=1)
    progress=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"project_id":project['id'],"task_id":task['id'],"source":"mcp","confidence":"high","status":"running","summary":"resumed","meaningful":True,"source_event_id":uuid.uuid4().hex,"observed_at":progress_at.isoformat()})
    assert progress.status_code==200
    advance_incidents(progress_at+timedelta(seconds=1))
    with connection() as conn: resolved=conn.execute("SELECT * FROM incidents WHERE id=%s",(incident_id,)).fetchone()
    assert resolved['state']=='resolved' and resolved['acknowledged_at'] and resolved['resumed_progress_at']
    assert resolved['acknowledged_at'] < resolved['resumed_progress_at']
    with connection() as conn:
        marks=conn.execute("SELECT event_type FROM events WHERE task_id=%s ORDER BY observed_at",(task['id'],)).fetchall()
    mark_types={r['event_type'] for r in marks}
    assert {'incident_opened','incident_notifying','notification_delivered','incident_acknowledged','progress_resumed','incident_resolved'} <= mark_types
    detail=client.get(f"/api/v1/projects/{project['id']}",headers=headers).json()
    visible_task=next(t for t in detail['open_tasks'] if t['id']==task['id'])
    assert visible_task['status']=='running' and visible_task['latest_incident_state']=='resolved'

    _,noack_task,later=stale_task(client,headers,principals)
    noack_id=open_incident(noack_task,later)
    deliver_outbox()
    advance_incidents(later+timedelta(seconds=70))
    with connection() as conn: escalated=conn.execute("SELECT * FROM incidents WHERE id=%s",(noack_id,)).fetchone()
    assert escalated['state']=='escalated' and escalated['acknowledged_at'] is None


def test_notification_retry_budget_is_bounded(client,headers,principals,monkeypatch):
    _,task,now=stale_task(client,headers,principals)
    incident=open_incident(task,now)
    class Broken:
        def send(self,payload): raise RuntimeError('offline')
    monkeypatch.setattr('lighthouse.worker.channel_for',lambda name,policy=None:Broken())
    for i in range(3):
        deliver_outbox()
        with connection() as conn: conn.execute("UPDATE notification_outbox SET next_attempt_at=now() WHERE incident_id=%s",(incident,))
    with connection() as conn:
        row=conn.execute("SELECT * FROM notification_outbox WHERE incident_id=%s",(incident,)).fetchone()
        incident_row=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()
        audit=conn.execute("SELECT details FROM events WHERE source_event_id=%s",(f"incident-escalated:{incident}",)).fetchone()
        alert=conn.execute("SELECT * FROM notification_outbox WHERE incident_id=%s AND channel='mac_alert'",(incident,)).fetchone()
    assert row['state']=='failed' and row['attempt_count']==3
    assert incident_row['state']=='escalated'
    assert audit and audit['details']['reason']=='Notification delivery failed after three attempts'
    assert alert and alert['state']=='pending'


def test_ack_without_progress_escalates_after_policy_deadline(client,headers,principals):
    _,task,now=stale_task(client,headers,principals)
    incident=open_incident(task,now)
    deliver_outbox()
    ack=client.post(f"/api/v1/incidents/{incident}/ack",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert ack.status_code==200
    with connection() as conn:
        row=conn.execute("SELECT progress_deadline FROM incidents WHERE id=%s",(incident,)).fetchone()
    assert row["progress_deadline"] is not None
    advance_incidents(row["progress_deadline"]+timedelta(seconds=1))
    with connection() as conn:
        escalated=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()
        audit=conn.execute("SELECT details FROM events WHERE source_event_id=%s",(f"incident-escalated:{incident}",)).fetchone()
        alert=conn.execute("SELECT * FROM notification_outbox WHERE incident_id=%s AND channel='mac_alert'",(incident,)).fetchone()
    assert escalated["state"]=="escalated"
    assert audit and audit["details"]["reason"]=="Progress deadline expired after acknowledgement"
    assert alert and alert["state"]=="pending"


def test_meaningful_progress_before_delivery_resolves_with_audit_event(client, headers):
    project = make_project(client, headers, "pre-delivery recovery")
    task = client.post(f"/api/v1/projects/{project['id']}/tasks", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"title":"recover before delivery"}).json()["task"]
    with connection() as conn:
        incident = conn.execute("INSERT INTO incidents(project_id,task_id,kind,reason) VALUES(%s,%s,'stall','No progress') RETURNING id", (project["id"],task["id"])).fetchone()
        conn.execute("INSERT INTO notification_outbox(incident_id,channel,recipient,payload,next_attempt_at) VALUES(%s,'test_sink','controller',%s,now())", (incident["id"],Jsonb({"incident_id":str(incident["id"])})))
    source_id = uuid.uuid4().hex
    response = client.post("/api/v1/events", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={
        "project_id":project["id"],"task_id":task["id"],"source":"mcp","status":"running",
        "event_type":"progress","summary":"work resumed before delivery","meaningful":True,
        "source_event_id":source_id,"observed_at":(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
    })
    assert response.status_code == 200
    with connection() as conn:
        row = conn.execute("SELECT state,acknowledged_at,resumed_progress_at FROM incidents WHERE id=%s", (incident["id"],)).fetchone()
        outbox = conn.execute("SELECT state,last_error FROM notification_outbox WHERE incident_id=%s", (incident["id"],)).fetchone()
        event = conn.execute("SELECT event_type,details FROM events WHERE source='mcp' AND source_event_id=%s", (f"resolved-before-delivery:{incident['id']}",)).fetchone()
    assert deliver_outbox() == []
    assert row["state"] == "resolved" and row["acknowledged_at"] is None and row["resumed_progress_at"] is not None
    assert outbox["state"] == "failed" and outbox["last_error"] == "resolved before notification delivery"
    assert event["event_type"] == "incident_resolved_before_delivery"


def test_progress_before_incident_open_does_not_resolve_or_cancel_notice(client,headers,principals):
    project,task,now=stale_task(client,headers,principals)
    incident=open_incident(task,now)
    stale=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"project_id":project["id"],"task_id":task["id"],"source":"mcp","status":"running","meaningful":True,"summary":"stale progress","source_event_id":uuid.uuid4().hex,"observed_at":(now-timedelta(seconds=1)).isoformat()})
    assert stale.status_code==200
    with connection() as conn:
        incident_row=conn.execute("SELECT state,resumed_progress_at FROM incidents WHERE id=%s",(incident,)).fetchone()
        outbox=conn.execute("SELECT state FROM notification_outbox WHERE incident_id=%s",(incident,)).fetchone()
    assert incident_row["state"]=="open" and incident_row["resumed_progress_at"] is None
    assert outbox["state"]=="pending"
    fresh=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"project_id":project["id"],"task_id":task["id"],"source":"mcp","status":"running","meaningful":True,"summary":"fresh progress","source_event_id":uuid.uuid4().hex,"observed_at":(now+timedelta(seconds=2)).isoformat()})
    assert fresh.status_code==200
    with connection() as conn:
        resolved=conn.execute("SELECT state,resumed_progress_at FROM incidents WHERE id=%s",(incident,)).fetchone()
        outbox=conn.execute("SELECT state FROM notification_outbox WHERE incident_id=%s",(incident,)).fetchone()
    assert resolved["state"]=="resolved" and resolved["resumed_progress_at"] is not None
    assert outbox["state"]=="failed"
