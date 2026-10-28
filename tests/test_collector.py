import json
import subprocess
import sys

from collector import collector
from collector.collector import parse_record, redact, session_context


def test_claude_fixture_produces_redacted_event_with_exact_mapping_metadata(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type":"user","timestamp":"2026-10-23T12:00:00Z","sessionId":"thread-1","uuid":"event-1","cwd":"/tmp/project","message":{"content":"private prompt"}})+"\n")
    event = parse_record(json.loads(path.read_text()), "claude_code", path.name)
    clean = redact(event)
    assert clean["working_directory"] == "/tmp/project"
    assert clean["external_thread_id"] == "thread-1"
    assert clean["confidence"] == "high"
    assert "private prompt" not in json.dumps(clean)


def test_codex_tail_recovers_context_from_leading_session_metadata(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join([
        json.dumps({"type":"session_meta","payload":{"session_id":"thread-2","cwd":"/Users/owner/Projects/Lighthouse"}}),
        json.dumps({"type":"event_msg","payload":{"type":"turn_started"}}),
    ]) + "\n")
    context = session_context(str(path), "codex")
    event = parse_record({"type":"event_msg","payload":{"type":"turn_started"}}, "codex", str(path), context)
    assert event["working_directory"] == "/Users/owner/Projects/Lighthouse"
    assert event["external_thread_id"] == "thread-2"


def test_discovery_upload_runs_before_session_event_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(collector.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(collector, "discover_roots", lambda state: ([], []))
    monkeypatch.setattr(collector, "send_discovery", lambda host, token, roots, titles, environment_id=None: calls.append("discovery"))
    monkeypatch.setattr(collector, "collect_t3_subagents", lambda: [])
    monkeypatch.setattr(collector, "scan_jsonl", lambda host, token, state, progress=None: calls.append("events"))
    monkeypatch.setattr(collector, "scan_liveness", lambda host, token, state, progress=None: calls.append("liveness"))

    collector.run_cycle("http://unused", "token", {}, last_discovery=0.0)

    assert calls == ["discovery", "events", "liveness"]


def test_subagent_discovery_runs_before_full_project_discovery(monkeypatch):
    calls=[]
    monkeypatch.setattr(collector.time,"monotonic",lambda:1000.0)
    monkeypatch.setattr(collector,"collect_t3_subagents",lambda:[{"child_t3_thread_id":"child"}])
    monkeypatch.setattr(collector,"send_subagents",lambda host,token,rows:calls.append("subagents"))
    monkeypatch.setattr(collector,"discover_roots",lambda state:(calls.append("projects") or ([],[])))
    monkeypatch.setattr(collector,"send_discovery",lambda *args,**kwargs:None)
    monkeypatch.setattr(collector,"scan_jsonl",lambda *args,**kwargs:calls.append("events"))
    monkeypatch.setattr(collector,"scan_liveness",lambda *args,**kwargs:calls.append("liveness"))
    collector.run_cycle("http://unused","token",{},last_discovery=0.0)
    assert calls==["subagents","projects","events","liveness"]


def test_mac_alert_delivery_attempts_only_newest_pending_alert_per_poll(monkeypatch):
    items = [
        {"id": "new-outbox", "incident_id": "new-incident", "payload": {}},
        {"id": "old-outbox", "incident_id": "old-incident", "payload": {}},
    ]
    attempted = []
    monkeypatch.setattr(collector, "request_json", lambda url, token, body=None: {"items": items} if body is None else {})

    def slow_notification(item):
        attempted.append(item["incident_id"])
        raise subprocess.TimeoutExpired("osascript", 15)

    monkeypatch.setattr(collector, "show_notification", slow_notification)

    collector.deliver_mac_alerts("http://unused", "token", {})

    assert attempted == ["new-incident"]


def test_idless_codex_records_get_stable_distinct_ids_and_advance_cursor(tmp_path,monkeypatch):
    path=tmp_path/"rollout.jsonl"
    path.write_text(json.dumps({"type":"session_meta","payload":{"session_id":"thread-idless","cwd":"/tmp/project"}})+"\n")
    sent=[]
    monkeypatch.setattr(collector.glob,"glob",lambda pattern,recursive,include_hidden:[str(path)] if "codex" in pattern else [])
    monkeypatch.setattr(collector,"send_event",lambda host,token,event:sent.append(event.copy()))
    state={}
    collector.scan_jsonl("http://unused","token",state)
    first_size=path.stat().st_size
    with path.open("a") as output:
        output.write(json.dumps({"type":"event_msg","payload":{"type":"turn_started"}})+"\n")
        output.write(json.dumps({"type":"event_msg","payload":{"type":"turn_started"}})+"\n")
    collector.scan_jsonl("http://unused","token",state)
    assert len(sent)==3
    appended_ids=[event["source_event_id"] for event in sent[1:]]
    assert appended_ids[0]!=appended_ids[1]
    assert state["offsets"][str(path)]["offset"]==path.stat().st_size
    assert state["offsets"][str(path)]["offset"]>first_size
    collector.scan_jsonl("http://unused","token",state)
    assert len(sent)==3
    offsets=[]
    with path.open() as file:
        file.readline()
        while True:
            offset=file.tell()
            line=file.readline()
            if not line:
                break
            offsets.append((offset,json.loads(line)))
    context={"cwd":"/tmp/project","thread":"thread-idless"}
    ids_a=[parse_record(record,"codex",str(path),context.copy(),offset)["source_event_id"] for offset,record in offsets]
    ids_b=[parse_record(record,"codex",str(path),context.copy(),offset)["source_event_id"] for offset,record in offsets]
    assert ids_a==ids_b and ids_a[0]!=ids_a[1]


def test_t3_provider_sessions_map_titles_to_provider_ids_read_only(tmp_path):
    import sqlite3
    from collector.collector import collect_t3_metadata

    db = tmp_path / ".t3/userdata/statev2.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT);
        CREATE TABLE orchestration_v2_projection_provider_threads(thread_id TEXT,provider TEXT,provider_session_id TEXT,provider_thread_id TEXT,payload_json TEXT);
        INSERT INTO orchestration_v2_projection_threads VALUES('t3-1','Project task',NULL);
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('t3-1','codex','provider-session-1','provider-thread-1','{"nativeThreadRef":{"nativeId":"provider-native-id"}}');
        INSERT INTO orchestration_v2_projection_threads VALUES('t3-2','Claude project task',NULL);
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('t3-2','claudeAgent','provider-session-2','provider-thread-2','{"nativeThreadRef":{"nativeId":"provider-native-2"}}');
        """)
    roots, titles = collect_t3_metadata(str(tmp_path))
    assert roots == []
    assert titles == [
        {"external_thread_id":"provider-native-id","source":"codex","title":"Project task","title_origin":"t3","t3_thread_id":"t3-1","t3_subagent":False},
        {"external_thread_id":"provider-native-2","source":"claude_code","title":"Claude project task","title_origin":"t3","t3_thread_id":"t3-2","t3_subagent":False},
    ]


def test_t3_metadata_captures_chat_ids_and_subagent_lineage_from_fixture(tmp_path):
    import sqlite3
    from collector.collector import collect_t3_metadata

    db = tmp_path / ".t3/userdata/statev2.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT,payload_json TEXT);
        CREATE TABLE orchestration_v2_projection_provider_threads(thread_id TEXT,provider TEXT,provider_session_id TEXT,provider_thread_id TEXT,payload_json TEXT);
        INSERT INTO orchestration_v2_projection_threads VALUES('top-chat','Top chat',NULL,'{}');
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('top-chat','codex','native-top','provider-top','{"nativeThreadRef":{"nativeId":"native-top"}}');
        INSERT INTO orchestration_v2_projection_threads VALUES('thread:delegated-task:child','Child chat',NULL,'{"lineage":{"relationshipToParent":"subagent"}}');
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('thread:delegated-task:child','codex','native-child','provider-child','{"nativeThreadRef":{"nativeId":"native-child"}}');
        """)
    roots, titles = collect_t3_metadata(str(tmp_path))
    assert roots == []
    assert titles == [
        {"external_thread_id":"native-top","source":"codex","title":"Top chat","title_origin":"t3","t3_thread_id":"top-chat","t3_subagent":False},
        {"external_thread_id":"native-child","source":"codex","title":"Child chat","title_origin":"t3","t3_thread_id":"thread:delegated-task:child","t3_subagent":True},
    ]


def test_t3_subagent_state_and_latest_message_time_are_read_from_transaction(tmp_path):
    import sqlite3
    from collector.collector import collect_t3_subagents
    database = tmp_path / ".t3/userdata/statev2.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.executescript("""
        CREATE TABLE orchestration_v2_projection_subagents(thread_id TEXT,child_thread_id TEXT,status TEXT,origin TEXT,updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT,updated_at TEXT,project_id TEXT);
        CREATE TABLE projection_projects(project_id TEXT,workspace_root TEXT);
        CREATE TABLE orchestration_v2_projection_messages(thread_id TEXT,updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_turn_items(thread_id TEXT,type TEXT,status TEXT,updated_at TEXT);
        CREATE TABLE projection_threads(thread_id TEXT,pending_approval_count INTEGER,pending_user_input_count INTEGER,updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_runtime_requests(thread_id TEXT,kind TEXT,status TEXT);
        INSERT INTO orchestration_v2_projection_subagents VALUES('parent','child','running','app_owned','2026-10-25T10:04:00+00:00');
        INSERT INTO orchestration_v2_projection_threads VALUES('parent','Parent title',NULL,'2026-10-25T09:59:00+00:00','project-1');
        INSERT INTO projection_projects VALUES('project-1','/Users/example/Projects/Test');
        INSERT INTO orchestration_v2_projection_threads VALUES('child','Child title',NULL,'2026-10-25T10:01:00+00:00',NULL);
        INSERT INTO orchestration_v2_projection_subagents VALUES('parent','waiting-child','running','provider_native','2026-10-25T10:00:00+00:00');
        INSERT INTO orchestration_v2_projection_threads VALUES('waiting-child','Waiting title',NULL,'2026-10-25T10:02:00+00:00',NULL);
        INSERT INTO projection_threads VALUES('child',0,0,'2026-10-25T10:01:00+00:00');
        INSERT INTO projection_threads VALUES('waiting-child',1,0,'2026-10-25T10:02:00+00:00');
        INSERT INTO orchestration_v2_projection_messages VALUES('child','2026-10-25T10:03:00+00:00');
        INSERT INTO orchestration_v2_projection_turn_items VALUES('child','command_execution','completed','2026-10-25T10:02:00+00:00');
        INSERT INTO orchestration_v2_projection_turn_items VALUES('child','assistant_message','completed','2026-10-25T10:05:00+00:00');
        INSERT INTO orchestration_v2_projection_turn_items VALUES('child','assistant_message','running','2026-10-25T10:06:00+00:00');
        INSERT INTO orchestration_v2_projection_turn_items VALUES('child','assistant_message','failed','2026-10-25T10:07:00+00:00');
        """)
    rows = collect_t3_subagents(str(tmp_path))
    assert rows == [
        {"parent_t3_thread_id":"parent", "child_t3_thread_id":"child", "status":"running",
         "title":"Child title", "parent_title":"Parent title", "parent_project_root":"/Users/example/Projects/Test",
         "last_activity_at":"2026-10-25T10:05:00+00:00", "last_child_output_at":"2026-10-25T10:05:00+00:00"},
        {"parent_t3_thread_id":"parent", "child_t3_thread_id":"waiting-child", "status":"waiting",
         "title":"Waiting title", "parent_title":"Parent title", "parent_project_root":"/Users/example/Projects/Test",
             "last_activity_at":"2026-10-25T10:02:00+00:00", "last_child_output_at":None},
    ]
    assert not list((tmp_path / ".t3/userdata").glob("statev2.sqlite-*"))


def test_t3_metadata_keeps_chat_identity_without_a_useful_title(tmp_path):
    import sqlite3
    from collector.collector import collect_t3_metadata

    db = tmp_path / ".t3/userdata/statev2.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT,payload_json TEXT);
        CREATE TABLE orchestration_v2_projection_provider_threads(thread_id TEXT,provider TEXT,provider_session_id TEXT,provider_thread_id TEXT,payload_json TEXT);
        INSERT INTO orchestration_v2_projection_threads VALUES('named','Useful name',NULL,'{}');
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('named','codex','native-named','provider-named','{"nativeThreadRef":{"nativeId":"native-named"}}');
        INSERT INTO orchestration_v2_projection_threads VALUES('generic','New thread',NULL,'{}');
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('generic','codex','native-generic','provider-generic','{"nativeThreadRef":{"nativeId":"native-generic"}}');
        INSERT INTO orchestration_v2_projection_threads VALUES('missing',NULL,NULL,'{}');
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('missing','claudeAgent','native-missing','provider-missing','{"nativeThreadRef":{"nativeId":"native-missing"}}');
        INSERT INTO orchestration_v2_projection_threads VALUES('child','New thread',NULL,'{"lineage":{"relationshipToParent":"subagent"}}');
        INSERT INTO orchestration_v2_projection_provider_threads VALUES('child','codex','native-child','provider-child','{"nativeThreadRef":{"nativeId":"native-child"}}');
        """)

    roots, metadata = collect_t3_metadata(str(tmp_path))

    assert roots == []
    assert metadata == [
        {"external_thread_id":"native-named","source":"codex","title":"Useful name","title_origin":"t3","t3_thread_id":"named","t3_subagent":False},
        {"external_thread_id":"native-generic","source":"codex","title":None,"title_origin":"t3","t3_thread_id":"generic","t3_subagent":False},
        {"external_thread_id":"native-missing","source":"claude_code","title":None,"title_origin":"t3","t3_thread_id":"missing","t3_subagent":False},
        {"external_thread_id":"native-child","source":"codex","title":None,"title_origin":"t3","t3_thread_id":"child","t3_subagent":True},
    ]


def test_t3_environment_and_thread_ids_use_conservative_charset(tmp_path):
    from collector.collector import read_t3_environment_id

    path = tmp_path / ".t3/userdata/environment-id"
    path.parent.mkdir(parents=True)
    path.write_text("  env:prod-01.%  \n")
    assert read_t3_environment_id(str(tmp_path)) == "env:prod-01.%"
    path.write_text("env id/invalid")
    assert read_t3_environment_id(str(tmp_path)) is None
    path.write_text("x" * 201)
    assert read_t3_environment_id(str(tmp_path)) is None


def test_send_discovery_sends_environment_id_once_with_roots(monkeypatch):
    calls = []
    monkeypatch.setattr(collector, "request_json", lambda url, token, body: calls.append(body))
    collector.send_discovery("http://unused", "token", [{"path":"/p","name":"P","origin":"folder"}], [
        {"external_thread_id":"native-1","source":"codex","title":"Chat","title_origin":"t3","t3_thread_id":"t3-1","t3_subagent":False}
    ], "env-1")
    assert calls == [
        {"roots":[{"path":"/p","name":"P","origin":"folder"}],"titles":[],"environment_id":"env-1"},
        {"roots":[],"titles":[{"external_thread_id":"native-1","source":"codex","title":"Chat","title_origin":"t3","t3_thread_id":"t3-1","t3_subagent":False}]},
    ]


def test_discover_roots_includes_active_t3_and_codex_worktrees(tmp_path, monkeypatch):
    from collector import collector

    monkeypatch.setattr(collector, "discovery_excluded", lambda path, home=None: False)

    home = tmp_path / "home"
    (home / "Projects" / "T3Only").mkdir(parents=True)
    (home / "projects" / "CodexOnly").mkdir(parents=True)
    (home / "Projects" / "Quiet").mkdir(parents=True)
    session_dir = home / ".codex" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "t3-active.jsonl").write_text(json.dumps({"type":"session_meta","payload":{"session_id":"t3-active","cwd":str(home / ".t3/worktrees/t3only/feature")}}) + "\n")
    (session_dir / "codex-active.jsonl").write_text(json.dumps({"type":"session_meta","payload":{"session_id":"codex-active","cwd":str(home / ".codex/worktrees/123/CODEXONLY/src")}}) + "\n")

    roots, _ = collector.discover_roots({}, str(home))

    found = {item["path"].casefold() for item in roots}
    assert str(home / "Projects" / "T3Only").casefold() in found
    assert str(home / "projects" / "CodexOnly").casefold() in found
    assert str(home / "Projects" / "Quiet").casefold() not in found


def test_t3_wal_metadata_read_does_not_change_provider_files(tmp_path):
    from collector.collector import collect_t3_metadata

    provider_dir = tmp_path / ".t3" / "userdata"
    provider_dir.mkdir(parents=True)
    database = provider_dir / "statev2.sqlite"
    subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
database = sys.argv[1]
conn = sqlite3.connect(database)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA wal_autocheckpoint=0')
conn.executescript('''
CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT);
CREATE TABLE orchestration_v2_projection_provider_threads(thread_id TEXT,provider TEXT,provider_session_id TEXT,provider_thread_id TEXT,payload_json TEXT);
INSERT INTO orchestration_v2_projection_threads VALUES('wal-thread','WAL only title',NULL);
INSERT INTO orchestration_v2_projection_provider_threads VALUES('wal-thread','codex','provider-session','provider-thread','{\"nativeThreadRef\":{\"nativeId\":\"wal-native-id\"}}');
''')
conn.commit()
os._exit(0)
""", str(database)], check=True)
    wal = provider_dir / "statev2.sqlite-wal"
    shm = provider_dir / "statev2.sqlite-shm"
    assert wal.is_file() and wal.stat().st_size > 0
    if shm.exists():
        shm.unlink()
    before = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in provider_dir.iterdir()}
    assert "statev2.sqlite-shm" not in before

    import pytest
    import sqlite3
    with pytest.raises(sqlite3.OperationalError, match="sidecars.*copying is disabled"):
        collect_t3_metadata(str(tmp_path))

    after = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in provider_dir.iterdir()}
    assert after == before
    assert not (tmp_path / ".cache/lighthouse/t3-snapshot").exists()


def test_claude_and_codex_native_title_fixtures_ignore_message_content(tmp_path):
    from collector.collector import thread_title_record

    claude_path = tmp_path / "claude.jsonl"
    claude_path.write_text(json.dumps({"type":"summary","summary":"Claude task","sessionId":"claude-session","message":{"content":"private"}})+"\n")
    codex_path = tmp_path / "session_index.jsonl"
    codex_path.write_text(json.dumps({"id":"codex-session","thread_name":"Codex task"})+"\n")
    claude = json.loads(claude_path.read_text())
    codex = json.loads(codex_path.read_text())
    assert thread_title_record(claude,"claude_code") == {"external_thread_id":"claude-session","source":"claude_code","title":"Claude task","title_origin":"native"}
    assert thread_title_record(codex,"codex") == {"external_thread_id":"codex-session","source":"codex","title":"Codex task","title_origin":"native"}
    assert "private" not in json.dumps(thread_title_record(claude,"claude_code"))
    assert thread_title_record({"id":"generic","thread_name":"New thread"},"codex") is None


def test_collector_skips_codex_session_from_briefs_work_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = home / ".config/lighthouse/briefs-work"
    work.mkdir(parents=True)
    session = home / ".codex/sessions/brief-run.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"type":"session_meta","payload":{"session_id":"brief-run","cwd":str(work)}})+"\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(collector.glob,"glob",lambda pattern,recursive,include_hidden:[str(session)] if ".codex" in pattern else [])
    monkeypatch.setattr(collector,"send_event",lambda *args: (_ for _ in ()).throw(AssertionError("briefs session was collected")))
    state={}
    collector.scan_jsonl("http://unused","token",state)
    assert state.get("offsets",{}) == {}


def test_collector_skips_claude_session_from_briefs_work_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = home / ".config/lighthouse/briefs-work"
    work.mkdir(parents=True)
    session = home / ".claude/projects/brief-run.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"type": "user", "cwd": str(work), "sessionId": "brief-run",
                                   "message": {"content": "must not become project activity"}}) + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(collector.glob, "glob", lambda pattern, recursive, include_hidden: [str(session)] if ".claude" in pattern else [])
    monkeypatch.setattr(collector, "send_event", lambda *args: (_ for _ in ()).throw(AssertionError("briefs session was collected")))
    state = {}
    collector.scan_jsonl("http://unused", "token", state)
    assert state.get("offsets", {}) == {}
