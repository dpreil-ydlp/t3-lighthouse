import uuid

from conftest import make_project
from lighthouse.db import connection


def owner(principals):
    return {"Authorization": f"Bearer {principals['user'][0]}"}


def make_task(client, headers, project_id):
    created = client.post(f"/api/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"title": "dashboard task"})
    assert created.status_code == 200, created.text
    return created.json()["task"]


def open_incident(project_id, task_id, state="escalated"):
    with connection() as conn:
        row = conn.execute("INSERT INTO incidents(project_id,task_id,kind,state,reason) VALUES(%s,%s,'stall',%s,'No meaningful progress within the task threshold') RETURNING id", (project_id, task_id, state)).fetchone()
        conn.execute("INSERT INTO notification_outbox(incident_id,channel,recipient,payload) VALUES(%s,'mac_alert','owner','{}')", (row["id"],))
    return str(row["id"])


def test_owner_can_set_next_action_and_status_with_audit_event(client, headers, principals):
    project = make_project(client, headers)
    r = client.patch(f"/api/v1/projects/{project['id']}", headers=owner(principals), json={"next_action": "  Ship the MVP  ", "status": "paused"})
    assert r.status_code == 200, r.text
    assert r.json()["project"]["next_action"] == "Ship the MVP"
    assert r.json()["project"]["status"] == "paused"
    events = client.get(f"/api/v1/projects/{project['id']}/events", headers=headers).json()["items"]
    assert any(e["event_type"] == "project_updated" and e["source"] == "dashboard" for e in events)


def test_agent_cannot_patch_project_and_invalid_status_is_rejected(client, headers, principals):
    project = make_project(client, headers)
    assert client.patch(f"/api/v1/projects/{project['id']}", headers=headers, json={"next_action": "x"}).status_code == 403
    assert client.patch(f"/api/v1/projects/{project['id']}", headers=owner(principals), json={"status": "deleted"}).status_code == 422
    assert client.patch(f"/api/v1/projects/{project['id']}", headers=owner(principals), json={}).status_code == 422
    assert client.patch(f"/api/v1/projects/{uuid.uuid4()}", headers=owner(principals), json={"next_action": "x"}).status_code == 404


def test_owner_closes_task_but_cannot_verify_through_patch(client, headers, principals):
    project = make_project(client, headers)
    task = make_task(client, headers, project["id"])
    assert client.patch(f"/api/v1/tasks/{task['id']}", headers=owner(principals), json={"status": "verified"}).status_code == 422
    assert client.patch(f"/api/v1/tasks/{task['id']}", headers=headers, json={"status": "cancelled"}).status_code == 403
    r = client.patch(f"/api/v1/tasks/{task['id']}", headers=owner(principals), json={"status": "cancelled"})
    assert r.status_code == 200 and r.json()["task"]["status"] == "cancelled"
    open_tasks = client.get(f"/api/v1/projects/{project['id']}", headers=headers).json()["open_tasks"]
    assert all(t["id"] != task["id"] for t in open_tasks)
    assert client.patch(f"/api/v1/tasks/{task['id']}", headers=owner(principals), json={"status": "complete"}).status_code == 404


def test_owner_closes_escalated_incident_and_pending_notice_is_cancelled(client, headers, principals):
    project = make_project(client, headers)
    task = make_task(client, headers, project["id"])
    incident_id = open_incident(project["id"], task["id"])
    assert client.post(f"/api/v1/incidents/{incident_id}/close", headers=headers, json={}).status_code == 403
    r = client.post(f"/api/v1/incidents/{incident_id}/close", headers=owner(principals), json={"note": "drill"})
    assert r.status_code == 200 and r.json()["state"] == "closed"
    with connection() as conn:
        incident = conn.execute("SELECT state,resolved_at FROM incidents WHERE id=%s", (incident_id,)).fetchone()
        outbox = conn.execute("SELECT state FROM notification_outbox WHERE incident_id=%s", (incident_id,)).fetchall()
    assert incident["state"] == "closed" and incident["resolved_at"] is not None
    assert [o["state"] for o in outbox] == ["failed"]
    assert client.post(f"/api/v1/incidents/{incident_id}/close", headers=owner(principals), json={}).status_code == 409


def test_incident_list_includes_task_title(client, headers, principals):
    project = make_project(client, headers)
    task = make_task(client, headers, project["id"])
    incident_id = open_incident(project["id"], task["id"])
    items = client.get("/api/v1/incidents", headers=headers).json()["items"]
    assert next(i for i in items if i["id"] == incident_id)["task_title"] == "dashboard task"


def test_event_limit_returns_newest_first_and_is_bounded(client, headers):
    project = make_project(client, headers)
    for minute in range(3):
        r = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"project_id": project["id"], "source": "mcp", "summary": f"m{minute}", "source_event_id": uuid.uuid4().hex, "observed_at": f"2026-10-23T10:0{minute}:00Z"})
        assert r.status_code == 200
    limited = client.get(f"/api/v1/projects/{project['id']}/events?limit=2", headers=headers).json()["items"]
    assert [e["details"]["summary"] for e in limited] == ["m2", "m1"]
    unlimited = client.get(f"/api/v1/projects/{project['id']}/events", headers=headers).json()["items"]
    assert len(unlimited) == 3 and unlimited[0]["details"]["summary"] == "m0"


def test_project_detail_lists_linked_sources(client, headers, principals):
    project = make_project(client, headers)
    directory = f"/tmp/linked-{uuid.uuid4().hex}"
    r = client.post(f"/api/v1/projects/{project['id']}/sources", headers={**owner(principals), "Idempotency-Key": uuid.uuid4().hex}, json={"source": "codex", "match_kind": "working_directory", "match_value": directory})
    assert r.status_code == 200, r.text
    sources = client.get(f"/api/v1/projects/{project['id']}", headers=headers).json()["sources"]
    assert [s["match_value"] for s in sources] == [directory]


def test_owner_closed_stall_stays_quiet_until_a_distinct_stall(client, headers, principals):
    from datetime import datetime, timedelta, timezone
    from lighthouse.worker import evaluate_stalls

    project = make_project(client, headers)
    task = make_task(client, headers, project["id"])
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=2)
    with connection() as conn:
        conn.execute("UPDATE tasks SET status='running',last_meaningful_progress_at=%s,thresholds='{\"meaningful_progress_seconds\":60}' WHERE id=%s", (stale, task["id"]))
    first = evaluate_stalls(now)
    assert len([i for i in first]) >= 1
    with connection() as conn:
        incident = conn.execute("SELECT id FROM incidents WHERE task_id=%s", (task["id"],)).fetchone()
        conn.execute("UPDATE incidents SET state='escalated',cooldown_until=%s WHERE id=%s", (now - timedelta(minutes=1), incident["id"]))
    assert client.post(f"/api/v1/incidents/{incident['id']}/close", headers=owner(principals), json={}).status_code == 200

    # Same stale evidence after the cooldown: no new incident, no new notice.
    evaluate_stalls(now + timedelta(minutes=5))
    with connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM incidents WHERE task_id=%s", (task["id"],)).fetchone()["n"]
    assert count == 1

    # New progress, then a fresh stall: a distinct incident opens.
    fresh = now + timedelta(minutes=10)
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE id=%s", (fresh, task["id"]))
    reopened = evaluate_stalls(fresh + timedelta(minutes=5))
    with connection() as conn:
        rows = conn.execute("SELECT state FROM incidents WHERE task_id=%s ORDER BY opened_at", (task["id"],)).fetchall()
    assert [r["state"] for r in rows] == ["closed", "open"] and len(reopened) >= 1


def test_project_patch_rejects_explicit_null_name_or_status(client, headers, principals):
    project = make_project(client, headers)
    assert client.patch(f"/api/v1/projects/{project['id']}", headers=owner(principals), json={"name": None}).status_code == 422
    assert client.patch(f"/api/v1/projects/{project['id']}", headers=owner(principals), json={"status": None}).status_code == 422
    assert client.patch(f"/api/v1/projects/{project['id']}", headers=owner(principals), json={"next_action": None}).status_code == 200
