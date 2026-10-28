import json
import hashlib
import hmac
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from conftest import make_project
from lighthouse.db import connection


def assert_project_summary_matches(project_id):
    with connection() as conn:
        mismatches = conn.execute("""
          WITH expected AS (
            SELECT to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900) AS bucket_at,
              source, parent_thread_id, max(observed_at) AS last_observed_at
            FROM events WHERE project_id=%s
            GROUP BY to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900), source, parent_thread_id
          ), actual AS (
            SELECT bucket_at, source, parent_thread_id, last_observed_at
            FROM project_event_summary WHERE project_id=%s
          )
          SELECT count(*) AS n FROM (
            (SELECT * FROM expected EXCEPT SELECT * FROM actual)
            UNION ALL
            (SELECT * FROM actual EXCEPT SELECT * FROM expected)
          ) differences
        """, (project_id, project_id)).fetchone()["n"]
    assert mismatches == 0


def test_unmapped_sessions_group_and_owner_can_bind(client, headers, principals):
    project = make_project(client, headers, "bind-target")
    cwd = f"/tmp/unmapped-{uuid.uuid4().hex}"
    for suffix in ("a", "b"):
        response = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
            "source": "codex", "confidence": "high", "event_type": "collector_observation",
            "source_event_id": f"{cwd}-{suffix}", "observed_at": datetime.now(timezone.utc).isoformat(),
            "working_directory": cwd, "external_thread_id": f"thread-{suffix}", "summary": "metadata only",
        })
        assert response.status_code == 200 and response.json()["mapped"] is False
    owner = {"Authorization": f"Bearer {principals['user'][0]}"}
    assert client.get("/api/v1/unassigned", headers=headers).status_code == 403
    groups = client.get("/api/v1/unassigned", headers=owner)
    group = next(item for item in groups.json()["items"] if item["working_directory"] == cwd)
    assert group["observations"] == 2
    result = client.post(f"/api/v1/unassigned/{group['observation_id']}/bind", headers=owner, json={"project_id": project["id"]})
    assert result.status_code == 200
    assert not any(item["working_directory"] == cwd for item in client.get("/api/v1/unassigned", headers=owner).json()["items"])
    with connection() as conn:
        mapping = conn.execute("SELECT project_id,confidence FROM project_sources WHERE source='codex' AND match_kind='working_directory' AND match_value=%s", (cwd,)).fetchone()
        bound = conn.execute("SELECT count(*) n FROM unmapped_observations WHERE working_directory=%s AND resolved_project_id=%s", (cwd, project["id"])).fetchone()
    assert str(mapping["project_id"]) == project["id"] and mapping["confidence"] == "high"
    assert bound["n"] == 2
    assert_project_summary_matches(project["id"])
    future = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
        "source": "codex", "confidence": "high", "event_type": "collector_observation",
        "source_event_id": f"{cwd}-after-bind", "observed_at": datetime.now(timezone.utc).isoformat(),
        "working_directory": cwd, "external_thread_id": "thread-after-bind", "summary": "metadata only",
    })
    assert future.status_code == 200 and future.json()["mapped"] is True
    with connection() as conn:
        future_project = conn.execute("SELECT project_id FROM events WHERE source_event_id=%s", (f"{cwd}-after-bind",)).fetchone()["project_id"]
    assert str(future_project) == project["id"]
    assert_project_summary_matches(project["id"])


def test_create_project_from_unassigned_binds_in_one_action(client, headers, principals):
    cwd = f"/tmp/new-project-{uuid.uuid4().hex}"
    response = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
        "source": "claude_code", "confidence": "high", "source_event_id": uuid.uuid4().hex,
        "observed_at": datetime.now(timezone.utc).isoformat(), "working_directory": cwd,
        "external_thread_id": "claude-thread", "summary": "metadata only",
    })
    owner = {"Authorization": f"Bearer {principals['user'][0]}"}
    group = next(item for item in client.get("/api/v1/unassigned", headers=owner).json()["items"] if item["working_directory"] == cwd)
    created = client.post(f"/api/v1/unassigned/{group['observation_id']}/create-project", headers=owner, json={"name": "Created from session", "kind": "other"})
    assert response.status_code == 200 and created.status_code == 200
    assert not any(item["working_directory"] == cwd for item in client.get("/api/v1/unassigned", headers=owner).json()["items"])
    assert_project_summary_matches(created.json()["project_id"])


def test_external_thread_binding_maps_future_events(client, headers, principals):
    external_id = f"desktop-{uuid.uuid4()}"
    event = {"source": "claude_desktop", "confidence": "low", "source_event_id": uuid.uuid4().hex, "observed_at": datetime.now(timezone.utc).isoformat(), "external_thread_id": external_id, "summary": "mtime only"}
    first = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json=event)
    owner = {"Authorization": f"Bearer {principals['user'][0]}"}
    group = next(item for item in client.get("/api/v1/unassigned", headers=owner).json()["items"] if item["external_thread_id"] == external_id)
    result = client.post(f"/api/v1/unassigned/{group['observation_id']}/create-project", headers=owner, json={"name": "Desktop liveness", "kind": "other"})
    event["source_event_id"] = uuid.uuid4().hex
    event["observed_at"] = datetime.now(timezone.utc).isoformat()
    second = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json=event)
    assert first.json()["mapped"] is False and result.status_code == 200
    assert second.json()["mapped"] is True and second.json()["confidence"] == "low"
    with connection() as conn:
        mapped_project = conn.execute("SELECT project_id FROM events WHERE id=%s", (second.json()["event_id"],)).fetchone()["project_id"]
    assert_project_summary_matches(str(mapped_project))


def test_controller_channel_is_per_controller_and_url_is_not_returned(client, headers, principals):
    project = make_project(client, headers, "channel-project")
    registered = client.post(f"/api/v1/projects/{project['id']}/controllers", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={})
    controller_id = registered.json()["controller_id"]
    owner = {"Authorization": f"Bearer {principals['user'][0]}"}
    url = "https://example.invalid/unique-private-webhook-token"
    saved = client.put(f"/api/v1/controllers/{controller_id}/channel", headers=owner, json={"channel": "t3_webhook", "t3_webhook_url": url})
    detail = client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    assert saved.status_code == 200 and saved.json()["channel"] == "t3_webhook"
    assert url not in detail.text and detail.json()["controller"]["policy"]["channel"] == "t3_webhook"
    client.post(f"/api/v1/projects/{project['id']}/controllers", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"policy": {"channel": "mac_alert", "t3_webhook_url": "https://attacker.invalid"}})
    after_agent_update = client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    assert after_agent_update.json()["controller"]["policy"]["channel"] == "t3_webhook"
    assert url not in after_agent_update.text and "attacker.invalid" not in after_agent_update.text
    assert client.put(f"/api/v1/controllers/{controller_id}/channel", headers=owner, json={"channel": "t3_webhook", "t3_webhook_url": "https://"}).status_code == 422
    denied = client.put(f"/api/v1/controllers/{controller_id}/channel", headers=headers, json={"channel": "mac_alert"})
    assert denied.status_code == 403


def test_t3_channel_signs_exact_compact_body_without_prefix(tmp_path, monkeypatch):
    from lighthouse.channels import T3Webhook

    secret = tmp_path / "hmac"
    secret.write_bytes(b"test-secret\n")
    secret.chmod(0o600)
    observed = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def open_request(request, timeout):
        body = request.data
        observed["body"] = body
        observed["signature"] = request.get_header("X-t3-signature")
        observed["user_agent"] = request.get_header("User-agent")
        return Response()

    monkeypatch.setattr("lighthouse.channels.urllib.request.urlopen", open_request)
    payload = {"incident_id": "incident-1", "reason": "stalled"}
    T3Webhook("https://example.invalid/hook", str(secret)).send(payload)
    expected_body = b'{"incident_id":"incident-1","reason":"stalled"}'
    expected = hmac.new(b"test-secret", expected_body, hashlib.sha256).hexdigest()
    assert observed["body"] == expected_body and observed["signature"] == expected
    assert not observed["signature"].startswith("sha256=")
    # The T3 relay sits behind Cloudflare, which rejects the default Python-urllib agent (error 1010).
    assert observed["user_agent"] == "Lighthouse/1.0"


def test_escalated_incident_adds_mac_alert_and_delivery_is_recorded(client, headers, principals):
    from lighthouse.worker import advance_incidents, evaluate_stalls

    project = make_project(client, headers, "mac-escalation")
    controller = client.post(f"/api/v1/projects/{project['id']}/controllers", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"policy": {"ack_deadline_seconds": 1}}).json()
    thread = client.post(f"/api/v1/projects/{project['id']}/threads", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"source": "codex", "external_thread_id": uuid.uuid4().hex, "state": "running"}).json()
    task = client.post(f"/api/v1/projects/{project['id']}/tasks", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"title": "alert drill", "thresholds": {"meaningful_progress_seconds": 1}}).json()["task"]
    old = datetime.now(timezone.utc) - timedelta(seconds=10)
    with connection() as conn:
        conn.execute("UPDATE tasks SET status='running',parent_thread_id=%s,last_meaningful_progress_at=%s WHERE id=%s", (thread["parent_thread_id"], old, task["id"]))
    evaluate_stalls()
    with connection() as conn:
        incident_id = str(conn.execute("SELECT id FROM incidents WHERE task_id=%s ORDER BY opened_at DESC LIMIT 1", (task["id"],)).fetchone()["id"])
    owner_headers = headers
    pending = client.get("/api/v1/mac-alerts/pending", headers=owner_headers)
    first = next(row for row in pending.json()["items"] if row["incident_id"] == incident_id)
    delivered = client.post(f"/api/v1/mac-alerts/{first['id']}/delivered", headers=owner_headers, json={"incident_id": incident_id})
    assert delivered.status_code == 200
    advance_incidents(datetime.now(timezone.utc) + timedelta(seconds=10))
    with connection() as conn:
        rows = conn.execute("SELECT recipient,state,delivered_at FROM notification_outbox WHERE incident_id=%s AND channel='mac_alert' ORDER BY created_at", (incident_id,)).fetchall()
        event = conn.execute("SELECT 1 FROM events WHERE source='mac_collector' AND source_event_id=%s", (f"mac-alert-delivered:{first['id']}",)).fetchone()
    assert len(rows) == 2 and rows[0]["state"] == "delivered" and rows[0]["delivered_at"] is not None
    assert rows[1]["recipient"] == "escalation" and rows[1]["state"] == "pending" and event


def test_pending_mac_alerts_return_newest_escalation_first(client, headers):
    project = make_project(client, headers, "mac-alert-order")
    with connection() as conn:
        older = conn.execute("INSERT INTO incidents(project_id,kind,state,reason) VALUES(%s,'stall','escalated','older') RETURNING id", (project["id"],)).fetchone()["id"]
        newer = conn.execute("INSERT INTO incidents(project_id,kind,state,reason) VALUES(%s,'stall','escalated','newer') RETURNING id", (project["id"],)).fetchone()["id"]
        conn.execute("INSERT INTO notification_outbox(incident_id,channel,recipient,payload,created_at) VALUES(%s,'mac_alert','owner','{}',now()-interval '1 minute')", (older,))
        conn.execute("INSERT INTO notification_outbox(incident_id,channel,recipient,payload,created_at) VALUES(%s,'mac_alert','escalation','{}',now())", (newer,))

    response = client.get("/api/v1/mac-alerts/pending", headers=headers)

    assert response.status_code == 200
    incident_ids = [item["incident_id"] for item in response.json()["items"]]
    assert incident_ids.index(str(newer)) < incident_ids.index(str(older))


def test_collector_scans_every_session_path_and_redacts_text(tmp_path, monkeypatch):
    from collector import collector

    claude = tmp_path / "anywhere" / "claude.jsonl"
    codex = tmp_path / "elsewhere" / "codex.jsonl"
    claude.parent.mkdir()
    codex.parent.mkdir()
    claude.write_text(json.dumps({"type": "user", "timestamp": "2026-10-23T10:00:00Z", "sessionId": "c1", "cwd": "/tmp/other", "uuid": "c-event", "message": "private"}) + "\n")
    codex.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-10-23T10:00:00Z", "payload": {"session_id": "x1", "cwd": "/tmp/elsewhere"}}) + "\n")
    originals = collector.glob.glob
    monkeypatch.setattr(collector.glob, "glob", lambda pattern, recursive=False, include_hidden=False: [str(claude)] if "claude" in pattern else [str(codex)] if "codex" in pattern else originals(pattern, recursive=recursive, include_hidden=include_hidden))
    events = []
    monkeypatch.setattr(collector, "send_event", lambda host, token, event: events.append(collector.redact(event)))
    collector.scan_jsonl("http://localhost", "token", {})
    assert {event["source"] for event in events} == {"claude_code", "codex"}
    assert {event["working_directory"] for event in events} == {"/tmp/other", "/tmp/elsewhere"}
    assert "private" not in json.dumps(events)


def test_desktop_liveness_uses_file_mtime_with_low_confidence(tmp_path, monkeypatch):
    from collector import collector

    app_root = tmp_path / "chat"
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    app_root.mkdir()
    codex_root.mkdir()
    (claude_root / "local-agent-mode-sessions").mkdir(parents=True)
    chat_file = app_root / "metadata.db"
    codex_file = codex_root / "metadata.json"
    claude_file = claude_root / "local-agent-mode-sessions" / "session.jsonl"
    chat_file.write_text("opaque fixture")
    codex_file.write_text("opaque fixture")
    claude_file.write_text("opaque fixture")
    roots = {"~/Library/Application Support/com.openai.chat": str(app_root), "~/Library/Application Support/com.openai.codex": str(codex_root), "~/Library/Application Support/Claude": str(claude_root)}
    expanduser = collector.os.path.expanduser
    monkeypatch.setattr(collector.os.path, "expanduser", lambda path: roots.get(path, expanduser(path)))
    events = []
    monkeypatch.setattr(collector, "send_event", lambda host, token, event: events.append(event))
    state = {}
    collector.scan_liveness("http://localhost", "token", state)
    assert events == []
    os.utime(chat_file, (time.time() + 2, time.time() + 2))
    os.utime(codex_file, (time.time() + 2, time.time() + 2))
    os.utime(claude_file, (time.time() + 2, time.time() + 2))
    collector.scan_liveness("http://localhost", "token", state)
    assert {event["source"] for event in events} == {"chatgpt_desktop", "codex_desktop", "claude_desktop"}
    assert all(event["confidence"] == "low" and event["details"]["signal"] == "file_mtime_only" for event in events)


def test_collector_delivers_alerts_before_full_session_scan(tmp_path, monkeypatch):
    from collector import collector

    token_file = tmp_path / "token"
    token_file.write_text("test-token")
    token_file.chmod(0o600)
    state_file = tmp_path / "collector-state.json"
    expanduser = collector.os.path.expanduser
    monkeypatch.setattr(collector.os.path, "expanduser", lambda path: str(state_file) if path == "~/.config/lighthouse/collector-state.json" else expanduser(path))
    calls = []
    monkeypatch.setattr(collector, "deliver_mac_alerts", lambda *args: calls.append("deliver"))
    def scan_sessions(_host, _token, _state, progress):
        calls.append("sessions")
        progress()
    def scan_desktop(_host, _token, _state, progress):
        calls.append("liveness")
        progress()
    monkeypatch.setattr(collector, "scan_jsonl", scan_sessions)
    monkeypatch.setattr(collector, "scan_liveness", scan_desktop)
    monkeypatch.setattr(collector.time, "sleep", lambda _interval: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        collector.run("http://localhost", str(token_file), 1)
    except KeyboardInterrupt:
        pass
    assert calls == ["deliver", "sessions", "deliver", "liveness", "deliver", "deliver"]
    assert state_file.exists()


def test_collector_reads_state_from_the_older_file_name(tmp_path, monkeypatch):
    from collector import collector

    token_file = tmp_path / "token"
    token_file.write_text("test-token")
    token_file.chmod(0o600)
    state_file = tmp_path / "collector-state.json"
    (tmp_path / "collector-state-cr1.json").write_text('{"offsets": {"kept": {"offset": 7}}}')
    expanduser = collector.os.path.expanduser
    monkeypatch.setattr(collector.os.path, "expanduser", lambda path: str(state_file) if path == "~/.config/lighthouse/collector-state.json" else expanduser(path))
    seen = []
    monkeypatch.setattr(collector, "deliver_mac_alerts", lambda _host, _token, state: seen.append(dict(state)))
    monkeypatch.setattr(collector, "deliver_pending_t3_mcp", lambda *args: None)
    monkeypatch.setattr(collector, "run_cycle", lambda *args: 0.0)
    monkeypatch.setattr(collector.time, "sleep", lambda _interval: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        collector.run("http://localhost", str(token_file), 1)
    except KeyboardInterrupt:
        pass
    assert seen[0]["offsets"] == {"kept": {"offset": 7}}
    assert '"kept"' in state_file.read_text()

def test_jsonl_scan_checkpoints_each_batch_of_25(tmp_path, monkeypatch):
    from collector import collector

    files = []
    for index in range(25):
        path = tmp_path / f"session-{index}.jsonl"
        path.write_text("")
        files.append(str(path))
    monkeypatch.setattr(collector.glob, "glob", lambda pattern, recursive=False, include_hidden=False: files if "claude" in pattern else [])
    checkpoints = []
    collector.scan_jsonl("http://localhost", "token", {}, lambda: checkpoints.append(True))
    assert len(checkpoints) == 1


def test_collector_scans_sessions_in_hidden_directories(tmp_path, monkeypatch):
    from collector import collector

    root = tmp_path / "claude-projects"
    hidden_session = root / ".hidden-project" / "nested" / "session.jsonl"
    hidden_session.parent.mkdir(parents=True)
    hidden_session.write_text(json.dumps({"type": "user", "timestamp": "2026-10-23T10:00:00Z", "sessionId": "hidden-session", "cwd": "/tmp/hidden", "uuid": "hidden-event"}) + "\n")
    original_expanduser = collector.os.path.expanduser
    monkeypatch.setattr(collector.os.path, "expanduser", lambda path: path.replace("~/.claude/projects", str(root)) if "~/.claude/projects" in path else original_expanduser(path))
    events = []
    monkeypatch.setattr(collector, "send_event", lambda host, token, event: events.append(event))
    collector.scan_jsonl("http://localhost", "token", {})
    assert len(events) == 1 and events[0]["working_directory"] == "/tmp/hidden"


def test_claude_fallback_event_ids_are_content_specific():
    from collector import collector

    base = {"type": "queue-operation", "timestamp": "2026-10-23T10:00:00Z", "sessionId": "same-session", "operation": "one"}
    first = collector.parse_record(base, "claude_code", "/tmp/session.jsonl")
    second_record = {**base, "operation": "two"}
    second = collector.parse_record(second_record, "claude_code", "/tmp/session.jsonl")
    assert first["source_event_id"] != second["source_event_id"]


def test_collector_idempotency_key_hashes_redacted_body(monkeypatch):
    from collector import collector

    observed = {}
    def capture(request, timeout):
        observed["body"] = request.data
        observed["key"] = request.get_header("Idempotency-key")
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
        return Response()
    monkeypatch.setattr(collector.urllib.request, "urlopen", capture)
    collector.send_event("http://localhost", "token", {"source": "claude_code", "source_event_id": "one", "summary": "safe", "private_text": "must be dropped"})
    assert b"private_text" not in observed["body"]
    assert observed["key"] == hashlib.sha256(observed["body"]).hexdigest()


def test_desktop_scan_checkpoints_each_100_files(tmp_path, monkeypatch):
    from collector import collector

    roots = [tmp_path / "chat", tmp_path / "codex", tmp_path / "claude"]
    for root in roots:
        root.mkdir()
    for index in range(100):
        (roots[0] / f"file-{index}.db").write_text("opaque")
    expanded = {
        "~/Library/Application Support/com.openai.chat": str(roots[0]),
        "~/Library/Application Support/com.openai.codex": str(roots[1]),
        "~/Library/Application Support/Claude": str(roots[2]),
    }
    original = collector.os.path.expanduser
    monkeypatch.setattr(collector.os.path, "expanduser", lambda path: expanded.get(path, original(path)))
    checkpoints = []
    collector.scan_liveness("http://localhost", "token", {}, lambda: checkpoints.append(True))
    assert len(checkpoints) == 1
