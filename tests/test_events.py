import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from conftest import make_project
from lighthouse.db import connection


def test_events_are_append_only_for_api_role(client, headers):
    project=make_project(client,headers)
    response=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"project_id":project['id'],"source":"mcp","confidence":"high","summary":"append-only","source_event_id":uuid.uuid4().hex,"observed_at":"2026-10-23T10:00:00Z"})
    event_id=response.json()["event_id"]
    with pytest.raises(InsufficientPrivilege):
        with connection() as conn: conn.execute("UPDATE events SET event_type='changed' WHERE id=%s",(event_id,))
    with pytest.raises(InsufficientPrivilege):
        with connection() as conn: conn.execute("DELETE FROM events WHERE id=%s",(event_id,))


def test_mapped_collector_event_updates_thread_and_task_signals(client, headers, principals):
    from datetime import datetime, timezone

    project = make_project(client, headers, "signal timestamps")
    controller = client.post(f"/api/v1/projects/{project['id']}/controllers", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={})
    task = client.post(f"/api/v1/projects/{project['id']}/tasks", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"title":"mapped collector task"}).json()["task"]
    external_id = "codex-thread-" + uuid.uuid4().hex
    working_directory = f"/tmp/lighthouse-codex-{uuid.uuid4().hex}"
    thread = client.post(f"/api/v1/projects/{project['id']}/threads", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"source":"codex","external_thread_id":external_id,"state":"running"})
    assert controller.status_code == 200 and thread.status_code == 200
    owner = {"Authorization":f"Bearer {principals['user'][0]}","Idempotency-Key":uuid.uuid4().hex}
    binding = client.post(f"/api/v1/projects/{project['id']}/sources", headers=owner, json={"source":"codex","match_kind":"working_directory","match_value":working_directory})
    assert binding.status_code == 200
    observed = datetime.now(timezone.utc).isoformat()
    response = client.post("/api/v1/events", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={
        "source":"codex","event_type":"collector_observation","source_event_id":uuid.uuid4().hex,
        "external_thread_id":"unregistered-"+uuid.uuid4().hex,"working_directory":working_directory,
        "observed_at":observed,"summary":"observed Codex event",
        "meaningful":False,"task_id":task["id"],"details":{"record_type":"event_msg"}
    })
    assert response.status_code == 200 and response.json()["mapped"] is True and response.json()["confidence"] == "high"
    with connection() as conn:
        summary = conn.execute("""SELECT count(*) AS n FROM project_event_summary
          WHERE project_id=%s AND source='codex' AND parent_thread_id=%s""", (project["id"], thread.json()["parent_thread_id"])).fetchone()["n"]
        thread_row = conn.execute("SELECT * FROM parent_threads WHERE id=%s", (thread.json()["parent_thread_id"],)).fetchone()
        task_row = conn.execute("SELECT * FROM tasks WHERE id=%s", (task["id"],)).fetchone()
    assert summary > 0
    assert thread_row["last_agent_event_at"] is not None
    assert thread_row["last_process_heartbeat_at"] is not None
    assert thread_row["last_meaningful_progress_at"] is None
    assert task_row["last_agent_event_at"] is not None
    assert task_row["last_process_heartbeat_at"] is not None
    assert task_row["last_meaningful_progress_at"] is None
    progress = client.post("/api/v1/events", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={
        "source":"codex","event_type":"progress_report","source_event_id":uuid.uuid4().hex,
        "external_thread_id":"unregistered-"+uuid.uuid4().hex,"working_directory":working_directory,
        "observed_at":observed,"summary":"explicit task progress",
        "meaningful":True,"status":"running","task_id":task["id"]
    })
    assert progress.status_code == 200
    with connection() as conn:
        thread_row = conn.execute("SELECT last_meaningful_progress_at FROM parent_threads WHERE id=%s", (thread.json()["parent_thread_id"],)).fetchone()
        task_row = conn.execute("SELECT last_meaningful_progress_at FROM tasks WHERE id=%s", (task["id"],)).fetchone()
    assert thread_row["last_meaningful_progress_at"] is not None
    assert task_row["last_meaningful_progress_at"] is not None
    second_task = client.post(f"/api/v1/projects/{project['id']}/tasks", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"title":"ambiguous thread mapping"}).json()["task"]
    second_thread = client.post(f"/api/v1/projects/{project['id']}/threads", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"source":"codex","external_thread_id":"second-"+uuid.uuid4().hex,"state":"running"})
    assert second_thread.status_code == 200
    ambiguous = client.post("/api/v1/events", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={
        "source":"codex","event_type":"collector_observation","source_event_id":uuid.uuid4().hex,
        "external_thread_id":"unregistered-"+uuid.uuid4().hex,"working_directory":working_directory,
        "observed_at":observed,"summary":"ambiguous Codex mapping","meaningful":False,"task_id":second_task["id"]
    })
    assert ambiguous.status_code == 200 and ambiguous.json()["mapped"] is True and ambiguous.json()["confidence"] == "high"
    with connection() as conn:
        ambiguous_event = conn.execute("SELECT parent_thread_id FROM events WHERE id=%s", (ambiguous.json()["event_id"],)).fetchone()
    assert ambiguous_event["parent_thread_id"] is None


def test_exact_thread_mapping_wins_over_conflicting_directory_mapping(client,headers,principals):
    from psycopg.errors import CheckViolation
    import pytest
    project_a=make_project(client,headers,"thread takes precedence")
    project_b=make_project(client,headers,"directory must not override")
    for project in (project_a,project_b):
        assert client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={}).status_code==200
    external_id="exact-thread-"+uuid.uuid4().hex
    thread=client.post(f"/api/v1/projects/{project_a['id']}/threads",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"source":"codex","external_thread_id":external_id,"state":"running"}).json()
    directory=f"/tmp/conflicting-{uuid.uuid4().hex}"
    owner={"Authorization":f"Bearer {principals['user'][0]}","Idempotency-Key":uuid.uuid4().hex}
    binding=client.post(f"/api/v1/projects/{project_b['id']}/sources",headers=owner,json={"source":"codex","match_kind":"working_directory","match_value":directory})
    assert binding.status_code==200
    event=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"source":"codex","source_event_id":uuid.uuid4().hex,"external_thread_id":external_id,"working_directory":directory,"event_type":"collector_observation","observed_at":datetime.now(timezone.utc).isoformat(),"summary":"conflicting mapping"})
    assert event.status_code==200 and event.json()["mapped"] is True
    with connection() as conn:
        row=conn.execute("SELECT project_id,parent_thread_id FROM events WHERE id=%s",(event.json()["event_id"],)).fetchone()
        assert str(row["project_id"])==project_a["id"]
        assert str(row["parent_thread_id"])==thread["parent_thread_id"]
        with pytest.raises(CheckViolation):
            conn.execute("INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at) VALUES(%s,%s,'test','codex','high',%s,now())",(project_b["id"],thread["parent_thread_id"],uuid.uuid4().hex))
