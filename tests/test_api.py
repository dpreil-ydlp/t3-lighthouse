import uuid

from conftest import assert_test_database_is_isolated, make_project


def test_suite_uses_disposable_database():
    assert_test_database_is_isolated()


def test_database_connection_refuses_live_database_during_pytest(monkeypatch, tmp_path):
    import pytest
    from lighthouse.db import database_url

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "guard-check")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test@db:5432/lighthouse")
    with pytest.raises(RuntimeError, match="must use lighthouse_test"):
        database_url()
    password_file = tmp_path / "live-password-fixture"
    with password_file.open("w") as output:
        output.write("fixture-password")
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("LIGHTHOUSE_DB_PASSWORD_FILE", str(password_file))
    with pytest.raises(RuntimeError, match="must use lighthouse_test"):
        database_url()


def test_project_task_and_progress_are_visible(client, headers):
    project = make_project(client, headers)
    created = client.post(f"/api/v1/projects/{project['id']}/tasks", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"title":"observed work","thresholds":{"meaningful_progress_seconds":60}})
    assert created.status_code == 200
    task = created.json()["task"]
    progress = client.post("/api/v1/events", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"project_id":project["id"],"task_id":task["id"],"source":"mcp","confidence":"high","status":"running","summary":"progress evidence","meaningful":True,"source_event_id":uuid.uuid4().hex,"observed_at":"2026-10-23T10:00:00Z"})
    assert progress.status_code == 200
    assert progress.json()["event_id"]
    events = client.get(f"/api/v1/projects/{project['id']}/events", headers=headers)
    assert len(events.json()["items"]) == 1
    assert client.get("/api/v1/projects", headers=headers).json()["items"]


def test_idempotent_project_replay(client, headers):
    key = uuid.uuid4().hex
    payload = {"name":"replay-"+uuid.uuid4().hex,"kind":"personal"}
    first = client.post("/api/v1/projects", headers={**headers,"Idempotency-Key":key}, json=payload)
    replay = client.post("/api/v1/projects", headers={**headers,"Idempotency-Key":key}, json=payload)
    assert first.json() == replay.json()


def test_project_attention_counts_are_mutually_exclusive(client, headers):
    items = client.get("/api/v1/projects", headers=headers).json()["items"]
    counts = {state: sum(item["attention_state"] == state for item in items) for state in {item["attention_state"] for item in items}}
    needs_attention = sum(count for state, count in counts.items() if state not in {"healthy", "stalled"})
    assert needs_attention + counts.get("stalled", 0) + counts.get("healthy", 0) == len(items)


def test_passive_event_uses_exact_user_confirmed_mapping(client, headers, principals):
    project=make_project(client,headers,"mapped")
    working_directory=f"/tmp/lighthouse-source-{uuid.uuid4().hex}"
    owner={"Authorization":f"Bearer {principals['user'][0]}","Idempotency-Key":uuid.uuid4().hex}
    bind=client.post(f"/api/v1/projects/{project['id']}/sources",headers=owner,json={"source":"claude_code","match_kind":"working_directory","match_value":working_directory})
    assert bind.status_code==200
    event={"project_id":project['id'],"source":"claude_code","confidence":"high","event_type":"collector_observation","source_event_id":uuid.uuid4().hex,"observed_at":"2026-10-23T10:00:00Z","working_directory":working_directory,"summary":"Observed Claude Code user record","meaningful":False}
    mapped=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json=event)
    assert mapped.status_code==200 and mapped.json()['mapped'] is True and mapped.json()['confidence']=='high'
    event['source_event_id']=uuid.uuid4().hex
    event['working_directory']=f"/tmp/unmapped-source-{uuid.uuid4().hex}"
    unmapped=client.post("/api/v1/events",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json=event)
    assert unmapped.status_code==200 and unmapped.json()['mapped'] is False and unmapped.json()['confidence']=='unknown'


def test_unassigned_group_ignore_is_owner_only_and_reversible(client,headers,principals):
    from lighthouse.db import connection
    directory=f"/tmp/ignored-source-{uuid.uuid4().hex}"
    source="codex"
    with connection() as conn:
        conn.execute("INSERT INTO unmapped_observations(source,working_directory,source_event_id,observed_at,confidence) VALUES(%s,%s,%s,now(),'unknown')",(source,directory,uuid.uuid4().hex))
    owner={"Authorization":f"Bearer {principals['user'][0]}"}
    def group(show=False):
        rows=client.get(f"/api/v1/unassigned?show_ignored={str(show).lower()}",headers=owner).json()["items"]
        return next((item for item in rows if item["working_directory"]==directory),None)
    assert group() is not None
    ignored=client.post("/api/v1/unassigned/ignore",headers=owner,json={"source":source,"working_directory":directory})
    assert ignored.status_code==200 and group() is None
    shown=group(True)
    assert shown is not None and shown["ignored"] is True
    denied=client.post("/api/v1/unassigned/ignore",headers=headers,json={"source":source,"working_directory":directory})
    assert denied.status_code==403
    restored=client.post("/api/v1/unassigned/restore",headers=owner,json={"source":source,"working_directory":directory})
    assert restored.status_code==200 and group() is not None
