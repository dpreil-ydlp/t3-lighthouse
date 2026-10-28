import json
import uuid
from datetime import datetime, timedelta, timezone
from psycopg.types.json import Jsonb

from lighthouse.db import connection
from lighthouse.project_discovery import ensure_auto_thread, match_project_root, normalize_root
from lighthouse.worker import backfill_unmapped_observations


def user_headers(principals):
    return {"Authorization": f"Bearer {principals['user'][0]}"}


def project_root(client, headers, path, name, origin="folder"):
    response = client.post("/api/v1/discovery/roots", headers=headers, json={
        "roots": [{"path": path, "name": name, "origin": origin}], "titles": []
    })
    assert response.status_code == 200, response.text
    return response.json()["projects"][0]["id"]


def assert_summary_matches_project(project_id):
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


def test_case_insensitive_longest_prefix_and_worktree_mapping():
    roots = [
        {"path": "/Users/owner/Projects/Shop", "name": "Shop", "origin": "folder"},
        {"path": "/Users/owner/Projects/Shop/api", "name": "API", "origin": "folder"},
    ]
    assert normalize_root("/Users/owner/Projects/Shop/") == "/users/owner/projects/shop"
    assert match_project_root("/users/OWNER/projects/shop/API/src", roots)["name"] == "API"
    assert match_project_root("/Users/owner/.t3/worktrees/Shop/feature", roots)["name"] == "Shop"
    assert match_project_root("/Users/owner/.codex/worktrees/abc/Shop", roots)["name"] == "Shop"


def test_discovery_excludes_scratch_and_reuses_hidden_or_renamed_root(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    root = "/Users/owner/Projects/Example"
    pid = project_root(client, headers, root, "Example")
    scratch = client.post("/api/v1/discovery/roots", headers=headers, json={
        "roots": [{"path": "/Users/owner/.t3/scratch", "name": "scratch", "origin": "t3"}], "titles": []
    })
    assert scratch.status_code == 200 and scratch.json()["projects"] == []
    with connection() as conn:
        assert conn.execute("SELECT 1 FROM project_roots WHERE path=%s", (normalize_root("/Users/owner/.t3/scratch"),)).fetchone() is None
    assert client.patch(f"/api/v1/projects/{pid}", headers=user_headers(principals), json={"name": "Renamed", "hidden": True}).status_code == 200
    again = project_root(client, headers, root, "Example")
    assert again == pid
    with connection() as conn:
        row = conn.execute("SELECT name,hidden FROM projects WHERE id=%s", (pid,)).fetchone()
        roots = conn.execute("SELECT path FROM project_roots WHERE project_id=%s", (pid,)).fetchall()
    assert row == {"name": "Renamed", "hidden": True}
    assert [item["path"] for item in roots] == [normalize_root(root)]
    active_ids = {item["id"] for item in client.get("/api/v1/projects?view=active", headers=user_headers(principals)).json()["items"]}
    assert pid not in active_ids


def test_merge_moves_roots_sources_and_events_then_archives(client, principals):
    headers = user_headers(principals)
    discovery_headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    source = project_root(client, discovery_headers, "/Users/owner/Projects/Source", "Source")
    target = project_root(client, discovery_headers, "/Users/owner/Projects/Target", "Target")
    event_id = uuid.uuid4().hex
    with connection() as conn:
        conn.execute("INSERT INTO project_sources(project_id,source,match_kind,match_value,confidence) VALUES(%s,'codex','working_directory','/Users/owner/Projects/Source','medium')", (source,))
        conn.execute("INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at) VALUES(%s,'collector_observation','codex','medium',%s,now())", (source, event_id))
    response = client.post(f"/api/v1/projects/{source}/merge", headers=headers, json={"into": target})
    assert response.status_code == 200, response.text
    active_ids = {item["id"] for item in client.get("/api/v1/projects?view=active", headers=headers).json()["items"]}
    archived_ids = {item["id"] for item in client.get("/api/v1/projects?view=archived", headers=headers).json()["items"]}
    assert source not in active_ids and source in archived_ids
    with connection() as conn:
        assert conn.execute("SELECT status FROM projects WHERE id=%s", (source,)).fetchone()["status"] == "archived"
        assert conn.execute("SELECT count(*) n FROM project_roots WHERE project_id=%s", (target,)).fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) n FROM project_sources WHERE project_id=%s", (target,)).fetchone()["n"] == 1
        assert conn.execute("SELECT 1 FROM events WHERE project_id=%s AND source_event_id=%s", (target, event_id)).fetchone()
        assert conn.execute("SELECT count(*) n FROM events WHERE project_id=%s", (target,)).fetchone()["n"] == 2


def test_agent_cannot_create_high_confidence_source(client, principals):
    project = client.post("/api/v1/projects", headers={**{ "Authorization": f"Bearer {principals['agent'][0]}"}, "Idempotency-Key": uuid.uuid4().hex}, json={"name": "agent project"}).json()["project"]
    response = client.post(f"/api/v1/projects/{project['id']}/sources", headers={"Authorization": f"Bearer {principals['agent'][0]}", "Idempotency-Key": uuid.uuid4().hex}, json={"source": "codex", "match_kind": "working_directory", "match_value": "/tmp"})
    assert response.status_code == 403


def test_ingest_maps_a_nested_session_to_discovered_root(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    pid = project_root(client, headers, "/Users/owner/Projects/Auto", "Auto")
    response = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
        "source": "codex", "confidence": "high", "event_type": "collector_observation",
        "source_event_id": "auto-map-1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "working_directory": "/Users/owner/Projects/Auto/src", "external_thread_id": "auto-thread",
        "summary": "Observed codex session", "meaningful": False,
    })
    assert response.status_code == 200, response.text
    assert response.json()["mapped"] is True and response.json()["confidence"] == "medium"
    with connection() as conn:
        event = conn.execute("SELECT project_id,confidence,parent_thread_id FROM events WHERE source_event_id='auto-map-1'").fetchone()
    assert str(event["project_id"]) == pid and event["confidence"] == "medium" and event["parent_thread_id"]


def test_auto_mapped_thread_events_remain_medium(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    pid = project_root(client, headers, "/Users/owner/Projects/AutoRepeat", "AutoRepeat")
    responses = []
    for event_id in ("auto-repeat-1", "auto-repeat-2"):
        response = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
            "source": "codex", "confidence": "high", "event_type": "collector_observation",
            "source_event_id": event_id, "observed_at": datetime.now(timezone.utc).isoformat(),
            "working_directory": "/Users/owner/Projects/AutoRepeat/src", "external_thread_id": "auto-repeat-thread",
            "summary": "Observed codex session", "meaningful": False,
        })
        assert response.status_code == 200, response.text
        responses.append(response.json())
    assert [response["confidence"] for response in responses] == ["medium", "medium"]
    with connection() as conn:
        rows = conn.execute("SELECT project_id,confidence FROM events WHERE source_event_id IN ('auto-repeat-1','auto-repeat-2') ORDER BY source_event_id").fetchall()
    assert all(str(row["project_id"]) == pid and row["confidence"] == "medium" for row in rows)


def test_new_thread_uses_longest_current_root_over_stale_auto_mapping(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    outer_id = project_root(client, headers, "/Users/owner/Projects/Outer", "Outer")
    directory = "/Users/owner/Projects/Outer/api/src"
    def ingest(event_id, thread_id):
        response = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
            "source": "codex", "confidence": "high", "event_type": "collector_observation",
            "source_event_id": event_id, "observed_at": datetime.now(timezone.utc).isoformat(),
            "working_directory": directory, "external_thread_id": thread_id,
            "summary": "Observed codex session", "meaningful": False,
        })
        assert response.status_code == 200, response.text
        return response.json()
    assert ingest("nested-root-old-thread", "old-thread")["confidence"] == "medium"
    inner_id = project_root(client, headers, "/Users/owner/Projects/Outer/api", "Inner")
    result = ingest("nested-root-new-thread", "new-thread")
    assert result["confidence"] == "medium"
    with connection() as conn:
        row = conn.execute("SELECT project_id FROM events WHERE source_event_id='nested-root-new-thread'").fetchone()
    assert str(row["project_id"]) == inner_id
    assert inner_id != outer_id
    assert_summary_matches_project(outer_id)
    assert_summary_matches_project(inner_id)


def test_owner_thread_binding_wins_over_auto_root_refresh(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    outer_id = project_root(client, headers, "/Users/owner/Projects/OwnerOuter", "OwnerOuter")
    directory = "/Users/owner/Projects/OwnerOuter/api/src"

    def ingest(event_id, thread_id):
        response = client.post("/api/v1/events", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
            "source": "codex", "confidence": "high", "event_type": "collector_observation",
            "source_event_id": event_id, "observed_at": datetime.now(timezone.utc).isoformat(),
            "working_directory": directory, "external_thread_id": thread_id,
            "summary": "Observed codex session", "meaningful": False,
        })
        assert response.status_code == 200, response.text
        return response.json()

    assert ingest("owner-binding-seed", "seed-thread")["confidence"] == "medium"
    inner_id = project_root(client, headers, "/Users/owner/Projects/OwnerOuter/api", "OwnerInner")
    thread_id = "owner-bound-thread"
    bound = client.post(f"/api/v1/projects/{outer_id}/sources", headers={**user_headers(principals), "Idempotency-Key": uuid.uuid4().hex}, json={
        "source": "codex", "match_kind": "external_thread_id", "match_value": thread_id,
    })
    assert bound.status_code == 200, bound.text

    result = ingest("owner-binding-event", thread_id)
    with connection() as conn:
        event = conn.execute("SELECT project_id,confidence FROM events WHERE source_event_id='owner-binding-event'").fetchone()
    assert str(event["project_id"]) == outer_id and event["confidence"] == "high"
    assert result["confidence"] == "high"
    assert inner_id != outer_id
    assert_summary_matches_project(outer_id)
    assert_summary_matches_project(inner_id)


def test_title_truncation_and_no_message_bodies_in_collector_metadata(tmp_path):
    from collector.collector import thread_title_record

    record = {"type": "summary", "summary": "x" * 200, "message": {"content": "private body"}}
    result = thread_title_record(record, "claude_code", "thread-1")
    assert result == {"external_thread_id": "thread-1", "source": "claude_code", "title": "x" * 120, "title_origin": "native"}
    assert "private body" not in json.dumps(result)


def test_discovery_stores_bounded_title_metadata(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    response = client.post("/api/v1/discovery/roots", headers=headers, json={"roots": [], "titles": [{
        "external_thread_id": "title-thread", "source": "codex", "title": "A" * 180,
    }]})
    assert response.status_code == 200
    with connection() as conn:
        title = conn.execute("SELECT title FROM thread_titles WHERE source='codex' AND external_thread_id='title-thread'").fetchone()["title"]
    assert title == "A" * 120


def test_t3_title_has_precedence_over_native_and_generic_titles_are_ignored(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    endpoint = "/api/v1/discovery/roots"
    for title, origin in (("Native title", "native"), ("T3 title", "t3"), ("Later native title", "native")):
        response = client.post(endpoint, headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
            "roots": [], "titles": [{"external_thread_id":"precedence-thread","source":"codex","title":title,"title_origin":origin}]
        })
        assert response.status_code == 200, response.text
    response = client.post(endpoint, headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
        "roots": [], "titles": [{"external_thread_id":"generic-thread","source":"codex","title":"Untitled session","title_origin":"t3"}]
    })
    assert response.status_code == 200
    with connection() as conn:
        row = conn.execute("SELECT title,title_origin FROM thread_titles WHERE source='codex' AND external_thread_id='precedence-thread'").fetchone()
        generic = conn.execute("SELECT count(*) n FROM thread_titles WHERE source='codex' AND external_thread_id='generic-thread'").fetchone()["n"]
    assert row["title"] == "T3 title" and row["title_origin"] == "t3"
    assert generic == 0


def test_backfill_is_idempotent_and_maps_only_matched_roots(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    pid = project_root(client, headers, "/Users/owner/Projects/Backfill", "Backfill")
    now = datetime.now(timezone.utc)
    with connection() as conn:
        conn.execute("INSERT INTO unmapped_observations(source,external_thread_id,working_directory,source_event_id,observed_at,details,confidence) VALUES('codex','thread-bf','/Users/owner/Projects/Backfill/src','backfill-1',%s,%s,'unknown')", (now, Jsonb({"record_type": "turn"})))
        conn.execute("INSERT INTO unmapped_observations(source,external_thread_id,working_directory,source_event_id,observed_at,details,confidence) VALUES('codex','thread-other','/tmp/no-project','backfill-2',%s,%s,'unknown')", (now, Jsonb({})))
        first = backfill_unmapped_observations(conn)
        second = backfill_unmapped_observations(conn)
        event = conn.execute("SELECT project_id,confidence FROM events WHERE source='codex' AND source_event_id='backfill-1'").fetchone()
        unmapped = conn.execute("SELECT count(*) n FROM unmapped_observations WHERE source_event_id='backfill-2' AND resolved_project_id IS NULL").fetchone()["n"]
    assert first == 1 and second == 0
    assert str(event["project_id"]) == pid and event["confidence"] == "medium"
    assert unmapped == 1
    assert_summary_matches_project(pid)


def test_project_activity_buckets_and_response_shape(client, principals):
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    pid = project_root(client, headers, "/Users/owner/Projects/Active", "Active")
    now = datetime.now(timezone.utc)
    with connection() as conn:
        for index, age in enumerate((timedelta(minutes=2), timedelta(hours=2), timedelta(days=2), timedelta(days=8))):
            conn.execute("INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,'collector_observation','codex','medium',%s,%s,%s)", (pid, f"activity-{index}", now-age, Jsonb({"summary":"Observed"})))
    data = client.get("/api/v1/projects?view=all", headers=user_headers(principals)).json()["items"]
    item = next(project for project in data if project["id"] == pid)
    assert item["activity_state"] == "working_now"
    assert item["tools"] == ["codex"]
    assert "recent_threads" in item and "active_threads" in item and "open_incidents" in item
    assert item["last_activity_at"]
    assert item["activity_state"] == "working_now"
    expected = {"today": timedelta(hours=2), "this_week": timedelta(days=2), "quiet": timedelta(days=8)}
    for state, age in expected.items():
        path = f"/Users/owner/Projects/{state}"
        bucket_id = project_root(client, headers, path, state)
        with connection() as conn:
            conn.execute("INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at) VALUES(%s,'collector_observation','claude_code','medium',%s,%s)", (bucket_id, f"bucket-{state}", now-age))
        projects = client.get("/api/v1/projects?view=all", headers=user_headers(principals)).json()["items"]
        assert next(project for project in projects if project["id"] == bucket_id)["activity_state"] == state


def test_lighthouse_own_events_are_not_project_activity(client, principals):
    """Incident and notice steps Lighthouse records must not make a stalled project look busy."""
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    pid = project_root(client, headers, "/Users/owner/Projects/Stalled", "Stalled")
    now = datetime.now(timezone.utc)
    agent_at = now - timedelta(minutes=50)
    with connection() as conn:
        thread_id = ensure_auto_thread(conn, pid, "codex", "stalled-thread")
        conn.execute("INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,'collector_observation','codex','medium','stalled-agent',%s,%s)", (pid, thread_id, agent_at, Jsonb({"summary": "Observed"})))
        for index, (event_type, source) in enumerate((("incident_opened", "worker"), ("incident_notifying", "worker"), ("notification_delivered", "worker"),
                                                      ("notification_delivered", "mac_collector"), ("incident_escalated", "worker"),
                                                      ("incident_acknowledged", "mcp"), ("incident_resolved", "api"), ("cooldown_suppressed", "worker"))):
            conn.execute("INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,%s,'high',%s,%s,%s)",
                         (pid, thread_id, event_type, source, f"stalled-own-{index}", now - timedelta(minutes=1), Jsonb({})))
    item = next(project for project in client.get("/api/v1/projects?view=all", headers=user_headers(principals)).json()["items"] if project["id"] == pid)
    assert item["activity_state"] == "today"
    assert datetime.fromisoformat(item["last_activity_at"]) == agent_at
    assert not {"worker", "api", "mac_collector"} & set(item["tools"])
    assert [datetime.fromisoformat(thread["last_activity_at"]) for thread in item["recent_threads"]] == [agent_at]
    detail = client.get(f"/api/v1/projects/{pid}", headers=user_headers(principals)).json()
    assert detail["project"]["activity_state"] == "today"
    assert [datetime.fromisoformat(thread["last_activity_at"]) for thread in detail["threads"]] == [agent_at]

