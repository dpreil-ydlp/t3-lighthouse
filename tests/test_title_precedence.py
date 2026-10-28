import json
import sqlite3
import uuid

import pytest

from collector.collector import discover_roots
from conftest import make_project
from lighthouse.db import connection
from lighthouse.project_discovery import ensure_auto_thread


def make_t3_fixture(home, title, native_id):
    database = home / ".t3/userdata/statev2.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE orchestration_v2_projection_threads(
                thread_id TEXT, title TEXT, deleted_at TEXT, payload_json TEXT
            );
            CREATE TABLE orchestration_v2_projection_provider_threads(
                thread_id TEXT, provider TEXT, provider_session_id TEXT,
                provider_thread_id TEXT, payload_json TEXT
            );
        """)
        conn.execute(
            "INSERT INTO orchestration_v2_projection_threads VALUES('t3-review',?,NULL,'{}')",
            (title,),
        )
        conn.execute(
            "INSERT INTO orchestration_v2_projection_provider_threads VALUES('t3-review','codex','provider-review','provider-thread',?)",
            (json.dumps({"nativeThreadRef": {"nativeId": native_id}}),),
        )
    return database


@pytest.mark.parametrize("existing_native", [False, True])
def test_api_keeps_native_fallback_and_t3_identity(client, headers, existing_native):
    native_id = "native-" + uuid.uuid4().hex

    def upload(title, origin, **identity):
        response = client.post("/api/v1/discovery/roots", headers=headers, json={
            "titles": [{"source": "codex", "external_thread_id": native_id,
                        "title": title, "title_origin": origin, **identity}],
        })
        assert response.status_code == 200, response.text

    if existing_native:
        upload("Useful native title", "native")
    upload("New thread", "t3", t3_thread_id="t3-review", t3_subagent=False)
    upload("Updated native title", "native")

    with connection() as conn:
        row = conn.execute(
            "SELECT title,title_origin,t3_thread_id,t3_subagent FROM thread_titles WHERE source='codex' AND external_thread_id=%s",
            (native_id,),
        ).fetchone()
    assert row["title"] == "Updated native title"
    assert row["title_origin"] == "native"
    assert row["t3_thread_id"] == "t3-review" and row["t3_subagent"] is False


@pytest.mark.parametrize("t3_title", ["New thread", None])
def test_native_title_is_visible_in_recent_and_session_readbacks(tmp_path, client, headers, principals, t3_title):
    native_id = "visible-" + uuid.uuid4().hex
    make_t3_fixture(tmp_path, t3_title, native_id)
    index = tmp_path / ".codex/session_index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"id": native_id, "thread_name": "Useful native title"}) + "\n")
    _, titles = discover_roots({}, str(tmp_path))
    project = make_project(client, headers, "native title")
    with connection() as conn:
        thread = ensure_auto_thread(conn, project["id"], "codex", native_id)
        conn.execute(
            "INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,observed_at) VALUES(%s,%s,'collector_observation','codex','medium',now())",
            (project["id"], thread),
        )

    uploaded = client.post("/api/v1/discovery/roots", headers=headers, json={
        "environment_id": "env-review", "titles": titles,
    })
    assert uploaded.status_code == 200, uploaded.text
    user_headers = {"Authorization": f"Bearer {principals['user'][0]}"}
    projects = client.get("/api/v1/projects?view=all", headers=user_headers).json()["items"]
    listed = next(item for item in projects if item["id"] == project["id"])
    detail = client.get(f"/api/v1/projects/{project['id']}", headers=user_headers).json()

    assert listed["recent_threads"][0]["title"] == "Useful native title"
    assert detail["threads"][0]["title"] == "Useful native title"
    expected_url = "t3code://threads/env-review/t3-review"
    assert listed["recent_threads"][0]["t3_url"] == expected_url
    assert detail["threads"][0]["t3_url"] == expected_url
