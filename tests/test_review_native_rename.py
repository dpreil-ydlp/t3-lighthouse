import json, uuid
import pytest
from collector.collector import discover_roots
from conftest import make_project
from lighthouse.db import connection
from lighthouse.project_discovery import ensure_auto_thread

@pytest.mark.parametrize("source", ["codex", "claude_code"])
def test_latest_native_title_reaches_readbacks(tmp_path, client, headers, principals, source):
    native = "rename-" + uuid.uuid4().hex
    path = tmp_path / (".codex/session_index.jsonl" if source == "codex" else ".claude/projects/synthetic/session.jsonl")
    path.parent.mkdir(parents=True)
    project = make_project(client, headers, "Native rename regression")
    with connection() as conn:
        thread = ensure_auto_thread(conn, project["id"], source, native)
        conn.execute("INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,observed_at) VALUES(%s,%s,'collector_observation',%s,'medium',now())", (project["id"],thread,source))
    for title in ["Old native title", "Current native title"]:
        record = {"id":native,"thread_name":title} if source == "codex" else {"sessionId":native,"type":"summary","summary":title}
        with path.open("a") as out:
            out.write(json.dumps(record)+"\n")
        _, titles = discover_roots({}, str(tmp_path))
        assert client.post("/api/v1/discovery/roots", headers=headers, json={"titles":titles}).status_code == 200
    user = {"Authorization":f"Bearer {principals['user'][0]}"}
    listed = next(p for p in client.get("/api/v1/projects?view=all", headers=user).json()["items"] if p["id"] == project["id"])
    detail = client.get(f"/api/v1/projects/{project['id']}", headers=user).json()
    with connection() as conn:
        stored = conn.execute("SELECT title FROM thread_titles WHERE source=%s AND external_thread_id=%s", (source,native)).fetchone()["title"]
    observed = {"collector":titles[0]["title"], "database":stored, "recent":listed["recent_threads"][0]["title"], "detail":detail["threads"][0]["title"]}
    assert observed == dict.fromkeys(observed, "Current native title"), observed
