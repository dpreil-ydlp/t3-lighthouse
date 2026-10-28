import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from conftest import make_project
from lighthouse.db import connection
from lighthouse.project_discovery import ensure_auto_thread


def add_t3_parent(project, t3_id):
    native_id = "mcp-parent-" + uuid.uuid4().hex
    with connection() as conn:
        parent_id = ensure_auto_thread(conn, project["id"], "codex", native_id)
        conn.execute("INSERT INTO thread_titles(source,external_thread_id,title,title_origin,t3_thread_id,t3_subagent) VALUES('codex',%s,'Parent','t3',%s,false)", (native_id, t3_id))
    return parent_id


def test_render_kick_wording_and_title_limit():
    from collector.t3_mcp import render_kick

    payload = {"kind": "first", "child_title": "x" * 90, "silent_minutes": 30, "child_link": "t3code://threads/child"}
    first = render_kick(payload)
    assert first == f"Lighthouse: your subagent “{'x' * 80}” has been marked working but silent for 30 min. Check on it: read its thread, nudge it, or cancel it. t3code://threads/child"
    payload["kind"] = "second"
    assert render_kick(payload) == f"Lighthouse: your subagent “{'x' * 80}” is still silent (30 min). It needs a decision: nudge it or cancel it. t3code://threads/child"


def test_collector_calls_only_t3_read_and_send_tools(monkeypatch, tmp_path):
    from collector import t3_mcp

    token_file = tmp_path / "t3-token"
    token_file.write_text(json.dumps({"access_token": "local-test-token", "expires_at": 2_000_000_000}))
    os.chmod(token_file, 0o600)
    outbox = {"id": str(uuid.uuid4()), "payload": {"kind": "first", "parent_thread_id": "t3-parent",
        "child_thread_id": "thread:delegated-task:child", "child_title": "Child", "silent_minutes": 30,
        "child_link": "t3code://threads/child"}}
    calls = []

    class Client:
        def __init__(self, token):
            assert token == "local-test-token"
        def connect(self):
            pass
        def child_status(self, child):
            calls.append(("t3_thread_read", child))
            return "running"
        def send(self, parent, message, request_id):
            calls.append(("t3_thread_send", parent, message, request_id))

    def request(url, token, payload=None):
        if url.endswith("/status"):
            return {}
        if url.endswith("/pending"):
            return {"items": [outbox]}
        if url.endswith("/delivered"):
            return {"state": "delivered"}
        raise AssertionError("unexpected API request")

    monkeypatch.setattr(t3_mcp, "_json_request", request)
    state = {}
    assert t3_mcp.deliver_pending("http://api", "api-token", state, token_file=token_file, client_factory=Client) == 1
    assert [call[0] for call in calls] == ["t3_thread_read", "t3_thread_send"]
    assert calls[1][1] == "t3-parent"


def test_collector_rejects_malformed_thread_ids_without_sending(monkeypatch, tmp_path):
    from collector import t3_mcp

    token_file = tmp_path / "t3-token"
    token_file.write_text(json.dumps({"access_token": "local-test-token", "expires_at": 2_000_000_000}))
    os.chmod(token_file, 0o600)
    outbox = {"id": str(uuid.uuid4()), "payload": {"kind": "first", "parent_thread_id": "bad id",
        "child_thread_id": "thread:delegated-task:child", "child_title": "Child", "silent_minutes": 30,
        "child_link": "t3code://threads/child"}}
    requests = []
    monkeypatch.setattr(t3_mcp, "_json_request", lambda url, token, payload=None: requests.append((url, payload)) or ({"items": [outbox]} if url.endswith("/pending") else {}))

    class NoClient:
        def __init__(self, token):
            raise AssertionError("malformed IDs must be rejected before connecting to T3")

    assert t3_mcp.deliver_pending("http://api", "api-token", {}, token_file=token_file, client_factory=NoClient) == 0
    assert any(url.endswith("/failed") and payload == {"retryable": False, "reason": "malformed_thread_id"} for url, payload in requests)


def test_t3_send_failure_retries_and_incident_shows_not_delivered(client, headers):
    project = make_project(client, headers, "t3 retry")
    add_t3_parent(project, "t3-retry-parent")
    child_id = "thread:delegated-task:" + uuid.uuid4().hex
    activity = datetime.now(timezone.utc) - timedelta(minutes=31)
    response = client.post("/api/v1/discovery/subagents", headers=headers, json={"subagents": [{
        "parent_t3_thread_id": "t3-retry-parent", "parent_title": "Parent", "parent_project_root": None,
        "child_t3_thread_id": child_id, "title": "Child", "status": "running", "last_activity_at": activity.isoformat(),
    }]})
    assert response.status_code == 200, response.text
    from lighthouse.worker import evaluate_stalls
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s", (activity, child_id))
    incident_id = evaluate_stalls()[0]
    with connection() as conn:
        outbox = conn.execute("SELECT id FROM notification_outbox WHERE incident_id=%s", (incident_id,)).fetchone()
    failed = client.post(f"/api/v1/t3-mcp/{outbox['id']}/failed", headers=headers, json={"retryable": True, "reason": "send_failed"})
    assert failed.status_code == 200 and failed.json()["state"] == "pending"
    incident = next(item for item in client.get("/api/v1/incidents", headers=headers).json()["items"] if item["id"] == incident_id)
    assert incident["kick_delivery_status"] == "kick not delivered"
    with connection() as conn:
        conn.execute("UPDATE notification_outbox SET next_attempt_at=now() WHERE id=%s", (outbox["id"],))
    assert client.get("/api/v1/t3-mcp/pending", headers=headers).json()["items"][0]["id"] == str(outbox["id"])


def test_t3_token_expiry_is_exposed_through_api(client, headers):
    expiry = "2026-11-25T10:50:52Z"
    response = client.post("/api/v1/t3-mcp/status", headers=headers, json={"expires_at": expiry})
    assert response.status_code == 200, response.text
    status = client.get("/api/v1/t3-mcp/status", headers=headers)
    assert status.status_code == 200 and status.json()["expires_at"].startswith("2026-11-25T10:50:52")
