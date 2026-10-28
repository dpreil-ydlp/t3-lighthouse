import json
import sqlite3

import pytest

from collector.collector import discover_roots


def write_t3_db(home, title):
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
            (json.dumps({"nativeThreadRef": {"nativeId": "native-review"}}),),
        )


@pytest.mark.parametrize("t3_title", ["New thread", None])
def test_native_title_survives_generic_or_missing_t3_title(tmp_path, t3_title):
    write_t3_db(tmp_path, t3_title)
    index = tmp_path / ".codex/session_index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"id": "native-review", "thread_name": "Useful native title"}) + "\n")

    _, titles = discover_roots({}, str(tmp_path))

    assert titles == [{
        "external_thread_id": "native-review", "source": "codex",
        "title": "Useful native title", "title_origin": "native",
        "t3_thread_id": "t3-review", "t3_subagent": False,
    }]
