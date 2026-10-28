from datetime import datetime, timezone, timedelta

from conftest import make_project
from lighthouse.db import connection
from lighthouse.worker import evaluate_stalls
import pytest
import uuid


def make_stale_task(client,headers):
    project=make_project(client,headers,"stall")
    task=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"stalled","thresholds":{"meaningful_progress_seconds":1}}).json()["task"]
    now=datetime.now(timezone.utc)
    with connection() as conn: conn.execute("UPDATE tasks SET status='running',last_meaningful_progress_at=%s WHERE id=%s",(now-timedelta(seconds=10),task['id']))
    return project,task,now


def test_stall_opens_once_and_cooldown_suppresses(client,headers):
    project,task,now=make_stale_task(client,headers)
    opened=evaluate_stalls(now)
    assert len(opened)==1
    assert evaluate_stalls(now+timedelta(seconds=1))==[]
    progress=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"project_id":project['id'],"task_id":task['id'],"source":"mcp","status":"running","summary":"resumed before notification delivery","meaningful":True,"source_event_id":uuid.uuid4().hex,"observed_at":(now+timedelta(seconds=1)).isoformat()})
    assert progress.status_code==200
    with connection() as conn:
        incident=conn.execute("SELECT * FROM incidents WHERE id=%s",(opened[0],)).fetchone()
        assert incident['state']=='resolved' and incident['acknowledged_at'] is None and incident['resumed_progress_at'] is not None
        assert conn.execute("SELECT 1 FROM events WHERE source_event_id=%s",(f"resolved-before-delivery:{opened[0]}",)).fetchone()
        conn.execute("UPDATE incidents SET cooldown_until=%s WHERE id=%s",(now+timedelta(minutes=30),opened[0]))
    assert evaluate_stalls(now+timedelta(seconds=2))==[]
    with connection() as conn:
        suppressed=conn.execute("SELECT count(*) n FROM events WHERE project_id=%s AND event_type='cooldown_suppressed'",(project['id'],)).fetchone()['n']
        count=conn.execute("SELECT count(*) n FROM incidents WHERE task_id=%s",(task['id'],)).fetchone()['n']
    assert suppressed==1 and count==1


@pytest.mark.parametrize("threshold",["oops",0,-1,True,1.5])
def test_task_thresholds_must_be_positive_integers(client,headers,threshold):
    project=make_project(client,headers,"invalid threshold")
    response=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"invalid","thresholds":{"meaningful_progress_seconds":threshold}})
    assert response.status_code==422


def test_worker_skips_bad_threshold_row_and_evaluates_other_tasks(client,headers,caplog):
    project=make_project(client,headers,"threshold isolation")
    bad=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"bad row"}).json()["task"]
    good=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"good row","thresholds":{"meaningful_progress_seconds":1}}).json()["task"]
    stale=datetime.now(timezone.utc)-timedelta(seconds=10)
    with connection() as conn:
        conn.execute("UPDATE tasks SET status='running',last_meaningful_progress_at=%s,thresholds=%s WHERE id=%s",(stale,'{"meaningful_progress_seconds":"oops"}',bad["id"]))
        conn.execute("UPDATE tasks SET status='running',last_meaningful_progress_at=%s WHERE id=%s",(stale,good["id"]))
    try:
        evaluate_stalls(datetime.now(timezone.utc))
        with connection() as conn:
            good_incident=conn.execute("SELECT id FROM incidents WHERE task_id=%s",(good["id"],)).fetchone()
            bad_incident=conn.execute("SELECT id FROM incidents WHERE task_id=%s",(bad["id"],)).fetchone()
    finally:
        # The malformed row breaks project list queries in later tests, so remove it.
        with connection() as conn:
            conn.execute("UPDATE tasks SET thresholds='{}'::jsonb WHERE id=%s",(bad["id"],))
    assert good_incident is not None
    assert bad_incident is None
    assert bad["id"] in caplog.text
