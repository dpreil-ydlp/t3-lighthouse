from datetime import datetime, timedelta, timezone
import uuid

from psycopg.types.json import Jsonb

from conftest import make_project
from lighthouse.db import connection
from lighthouse.project_discovery import ensure_auto_thread


def test_t3_urls_cover_session_rows_and_latest_top_level_project_chat(client, headers, principals):
    project = make_project(client, headers, "t3 links")
    now = datetime.now(timezone.utc)
    thread_specs = [
        ("native-old", "thread:old", now - timedelta(minutes=4)),
        ("native-new", "thread:new%chat", now - timedelta(minutes=3)),
        ("native-generic", "thread:generic", now - timedelta(minutes=2)),
        ("native-missing", "thread:missing", now - timedelta(minutes=1)),
        ("native-child", "thread:delegated-task:child", now),
        ("native-plain", None, now + timedelta(minutes=1)),
    ]
    with connection() as conn:
        for native_id, _, observed_at in thread_specs:
            thread_id = ensure_auto_thread(conn, project["id"], "codex", native_id)
            conn.execute("""INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details)
              VALUES(%s,%s,'collector_observation','codex','high',%s,%s,%s)""",
              (project["id"], thread_id, uuid.uuid4().hex, observed_at, Jsonb({"record_type":"fixture"})))

    agent_headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    uploaded = client.post("/api/v1/discovery/roots", headers=agent_headers, json={
        "roots": [],
        "environment_id": "env:prod",
        "titles": [
            {"external_thread_id":"native-old","source":"codex","title":"Old chat","title_origin":"t3","t3_thread_id":"thread:old","t3_subagent":False},
            {"external_thread_id":"native-new","source":"codex","title":"Newest top chat","title_origin":"t3","t3_thread_id":"thread:new%chat","t3_subagent":False},
            {"external_thread_id":"native-generic","source":"codex","title":"New thread","title_origin":"t3","t3_thread_id":"thread:generic","t3_subagent":False},
            {"external_thread_id":"native-missing","source":"codex","title":None,"title_origin":"t3","t3_thread_id":"thread:missing","t3_subagent":False},
            {"external_thread_id":"native-child","source":"codex","title":"Subagent chat","title_origin":"t3","t3_thread_id":"thread:delegated-task:child","t3_subagent":True},
            {"external_thread_id":"native-plain","source":"codex","title":"Native session","title_origin":"native"},
        ],
    })
    assert uploaded.status_code == 200, uploaded.text
    with connection() as conn:
        untitled_rows = conn.execute("""SELECT external_thread_id,title FROM thread_titles
          WHERE external_thread_id IN ('native-generic','native-missing') ORDER BY external_thread_id""").fetchall()
    assert [(row["external_thread_id"], row["title"]) for row in untitled_rows] == [
        ("native-generic", None), ("native-missing", None),
    ]

    expected_project = "t3code://threads/env%3Aprod/thread%3Amissing"
    expected_new = "t3code://threads/env%3Aprod/thread%3Anew%25chat"
    expected_generic = "t3code://threads/env%3Aprod/thread%3Ageneric"
    expected_child = "t3code://threads/env%3Aprod/thread%3Adelegated-task%3Achild"
    items = client.get("/api/v1/projects?view=all", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["items"]
    listed = next(item for item in items if item["id"] == project["id"])
    assert listed["t3_url"] == expected_project

    detail = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()
    assert detail["project"]["t3_url"] == expected_project
    sessions = {item["external_thread_id"]: item for item in detail["threads"]}
    assert sessions["native-new"]["t3_url"] == expected_new
    assert sessions["native-generic"]["t3_url"] == expected_generic
    assert sessions["native-missing"]["t3_url"] == expected_project
    assert sessions["native-child"]["t3_url"] == expected_child
    assert sessions["native-plain"]["t3_url"] is None
    assert listed["recent_threads"][0]["t3_url"] is None


def test_project_and_sessions_return_null_without_t3_metadata(client, headers, principals):
    project = make_project(client, headers, "no T3")
    with connection() as conn:
        thread_id = ensure_auto_thread(conn, project["id"], "codex", "native-only")
        conn.execute("""INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at)
          VALUES(%s,%s,'collector_observation','codex','high',%s,now())""",
          (project["id"], thread_id, uuid.uuid4().hex))
    user_headers = {"Authorization": f"Bearer {principals['user'][0]}"}
    listed = client.get("/api/v1/projects?view=all", headers=user_headers).json()["items"]
    assert next(item for item in listed if item["id"] == project["id"])["t3_url"] is None
    detail = client.get(f"/api/v1/projects/{project['id']}", headers=user_headers).json()
    assert detail["threads"][0]["t3_url"] is None
