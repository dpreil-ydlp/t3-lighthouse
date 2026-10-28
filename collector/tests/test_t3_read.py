"""Small, isolated SQLite regressions; no provider database or service access."""
import ast
import json
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from collector import collector, t3_read


SCHEMA = """
CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT);
CREATE TABLE orchestration_v2_projection_provider_threads(thread_id TEXT,provider TEXT,provider_session_id TEXT,payload_json TEXT);
INSERT INTO orchestration_v2_projection_threads VALUES('fixture','Committed title',NULL);
INSERT INTO orchestration_v2_projection_provider_threads VALUES('fixture','codex','session','{"nativeThreadRef":{"nativeId":"native-fixture"}}');
"""


class T3ReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lighthouse-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.database = self.home / ".t3/userdata/statev2.sqlite"
        self.database.parent.mkdir(parents=True)

    def orphan_wal(self):
        subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.executescript(sys.argv[2])
connection.commit()
os._exit(0)
""", str(self.database), SCHEMA], check=True)
        Path(str(self.database) + "-shm").unlink(missing_ok=True)

    def provider_state(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.database.parent.iterdir()}

    @property
    def snapshot(self):
        return self.home / ".cache/lighthouse/t3-snapshot"

    def test_orphan_wal_fails_closed_without_copying_or_provider_writes(self):
        self.orphan_wal()
        before = self.provider_state()
        with patch("shutil.copy2", side_effect=AssertionError("provider database copied")), \
             patch("subprocess.run", side_effect=AssertionError("copy process started")), \
             patch("tempfile.mkdtemp", side_effect=AssertionError("copy directory created")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "sidecars.*copying is disabled"):
                collector.collect_t3_metadata(str(self.home))
            with self.assertRaisesRegex(sqlite3.OperationalError, "sidecars.*copying is disabled"):
                collector.collect_t3_subagents(str(self.home))
        self.assertEqual(self.provider_state(), before)
        self.assertFalse(self.snapshot.exists())

    def test_read_transaction_keeps_one_consistent_wal_snapshot(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript(SCHEMA)
        with t3_read.read_t3_database(self.home) as reader:
            before = reader.execute("SELECT title FROM orchestration_v2_projection_threads").fetchone()
            writer.execute("UPDATE orchestration_v2_projection_threads SET title='Later commit'")
            writer.commit()
            after = reader.execute("SELECT title FROM orchestration_v2_projection_threads").fetchone()
            self.assertEqual(before, ("Committed title",))
            self.assertEqual(after, before)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM orchestration_v2_projection_threads")
        _, titles = collector.collect_t3_metadata(str(self.home))
        self.assertEqual(titles[0]["title"], "Later commit")
        self.assertFalse(self.snapshot.exists())

    def test_concurrent_process_cannot_remove_legacy_snapshot(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript(SCHEMA)
        with t3_read.read_t3_database(self.home) as reader:
            self.snapshot.mkdir()
            private = self.snapshot / "statev2.sqlite"
            private.write_bytes(b"small stale fixture")
            attempt = subprocess.run([sys.executable, "-c", """
import sys
from collector.collector import collect_t3_metadata
try:
    collect_t3_metadata(sys.argv[1])
except BlockingIOError:
    sys.exit(7)
sys.exit(9)
""", str(self.home)], cwd=Path(collector.__file__).parents[1], capture_output=True, text=True, timeout=5)
            self.assertEqual(attempt.returncode, 7, attempt.stderr)
            self.assertTrue(private.exists())
            self.assertEqual(reader.execute("SELECT title FROM orchestration_v2_projection_threads").fetchone(), ("Committed title",))
        self.assertFalse(self.snapshot.exists())

    def test_error_and_interrupt_close_read_without_creating_snapshot(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript(SCHEMA)
        for exception in (RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exception.__name__):
                with self.assertRaises(exception):
                    with t3_read.read_t3_database(self.home) as reader:
                        self.assertFalse(self.snapshot.exists())
                        raise exception("fixture interruption")
                with self.assertRaises(sqlite3.ProgrammingError):
                    reader.execute("SELECT 1")
                self.assertFalse(self.snapshot.exists())

    def test_next_reader_cleans_legacy_snapshot_without_replacing_it(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript(SCHEMA)
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "statev2.sqlite").write_bytes(b"small stale fixture")
        _, titles = collector.collect_t3_metadata(str(self.home))
        self.assertEqual(titles[0]["title"], "Committed title")
        self.assertFalse(self.snapshot.exists())

    def test_collector_scripts_and_server_have_no_file_copy_operations(self):
        root = Path(collector.__file__).parents[1]
        forbidden = {"copyfile", "copyfileobj", "copy2", "copytree", "clonefile", "backup"}
        for directory in (root / "collector", root / "scripts", root / "lighthouse"):
            for path in directory.rglob("*.py"):
                if "tests" in path.parts:
                    continue
                with self.subTest(path=str(path.relative_to(root))):
                    tree = ast.parse(path.read_text())
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.Call):
                            continue
                        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                        self.assertNotIn(name, forbidden, "Do not copy provider databases or workspaces")
                        if name in {"run", "Popen", "call", "check_call", "check_output", "system"} and node.args:
                            command = node.args[0]
                            if isinstance(command, (ast.List, ast.Tuple)) and command.elts:
                                command = command.elts[0]
                            if isinstance(command, ast.Constant) and isinstance(command.value, str):
                                self.assertNotIn(Path(command.value.split()[0]).name,
                                                 {"cp", "ditto", "rsync"})

    def test_expired_read_deadline_does_not_start_a_transaction(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript(SCHEMA)
        with patch.object(t3_read, "READ_SECONDS", 0):
            with self.assertRaisesRegex(sqlite3.OperationalError, "deadline"):
                collector.collect_t3_metadata(str(self.home))

    def test_sqlite_progress_handler_aborts_expired_query(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript(SCHEMA)
        with patch.object(t3_read.time, "monotonic", return_value=10) as clock:
            with t3_read.read_t3_database(self.home) as reader:
                clock.return_value = 100
                with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                    reader.execute("WITH RECURSIVE counter(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM counter WHERE n<100000) SELECT SUM(n) FROM counter").fetchone()
        self.assertFalse(self.snapshot.exists())

    def test_active_wal_metadata_uses_no_database_copy(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript(SCHEMA)
        writer.commit()
        before = self.database.read_bytes()
        with patch("tempfile.mkdtemp", side_effect=AssertionError("database copy directory created")), \
             patch("shutil.copy2", side_effect=AssertionError("database copied")), \
             patch("subprocess.run", side_effect=AssertionError("copy process started")):
            roots, titles = collector.collect_t3_metadata(str(self.home))
        self.assertEqual(roots, [])
        self.assertEqual(titles[0]["title"], "Committed title")
        self.assertEqual(titles[0]["external_thread_id"], "native-fixture")
        self.assertEqual(self.database.read_bytes(), before)

    def test_active_wal_subagent_discovery_uses_no_database_copy(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript("""
        CREATE TABLE orchestration_v2_projection_subagents(thread_id TEXT,child_thread_id TEXT,status TEXT,origin TEXT,updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,title TEXT,deleted_at TEXT,updated_at TEXT,project_id TEXT);
        CREATE TABLE projection_projects(project_id TEXT,workspace_root TEXT);
        CREATE TABLE orchestration_v2_projection_messages(thread_id TEXT,updated_at TEXT);
        CREATE INDEX messages_thread_idx ON orchestration_v2_projection_messages(thread_id);
        CREATE TABLE orchestration_v2_projection_turn_items(thread_id TEXT,type TEXT,status TEXT,updated_at TEXT);
        CREATE INDEX turn_items_thread_idx ON orchestration_v2_projection_turn_items(thread_id);
        CREATE TABLE projection_threads(thread_id TEXT,pending_approval_count INTEGER,pending_user_input_count INTEGER,updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_runtime_requests(thread_id TEXT,kind TEXT,status TEXT);
        INSERT INTO orchestration_v2_projection_subagents VALUES('parent','child','running','app_owned','2026-10-25T10:04:00+00:00');
        INSERT INTO orchestration_v2_projection_threads VALUES('parent','Parent',NULL,'2026-10-25T10:00:00+00:00','project');
        INSERT INTO orchestration_v2_projection_threads VALUES('child','Child',NULL,'2026-10-25T10:01:00+00:00',NULL);
        INSERT INTO projection_projects VALUES('project','/Users/example/Projects/Test');
        INSERT INTO orchestration_v2_projection_messages VALUES('child','2026-10-25T10:03:00+00:00');
        INSERT INTO orchestration_v2_projection_turn_items VALUES('child','command_execution','completed','2026-10-25T10:02:00+00:00');
        """)
        writer.commit()
        with patch("tempfile.mkdtemp", side_effect=AssertionError("database copy directory created")), \
             patch("shutil.copy2", side_effect=AssertionError("database copied")), \
             patch("subprocess.run", side_effect=AssertionError("copy process started")):
            rows = collector.collect_t3_subagents(str(self.home))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_child_output_at"], "2026-10-25T10:02:00+00:00")
        self.assertEqual(rows[0]["last_activity_at"], "2026-10-25T10:04:00+00:00")
        self.assertFalse(self.snapshot.exists())

    def test_child_activity_uses_indexed_lookups_not_whole_table_aggregates(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.executescript("""
            CREATE TABLE orchestration_v2_projection_messages(thread_id TEXT,updated_at TEXT);
            CREATE INDEX messages_thread_idx ON orchestration_v2_projection_messages(thread_id);
            CREATE TABLE orchestration_v2_projection_turn_items(thread_id TEXT,type TEXT,status TEXT,updated_at TEXT);
            CREATE INDEX turn_items_thread_idx ON orchestration_v2_projection_turn_items(thread_id);
            """)
            old_output_plan = list(writer.execute("EXPLAIN QUERY PLAN SELECT thread_id,MAX(updated_at) FROM orchestration_v2_projection_turn_items WHERE type IN ('command_execution','dynamic_tool') AND status IN ('completed','failed') GROUP BY thread_id"))
            new_output_plan = list(writer.execute("EXPLAIN QUERY PLAN SELECT MAX(updated_at) FROM orchestration_v2_projection_turn_items WHERE thread_id=? AND type IN ('command_execution','dynamic_tool') AND status IN ('completed','failed')", ("child",)))
            old_message_plan = list(writer.execute("EXPLAIN QUERY PLAN SELECT thread_id,MAX(updated_at) FROM orchestration_v2_projection_messages GROUP BY thread_id"))
            new_message_plan = list(writer.execute("EXPLAIN QUERY PLAN SELECT MAX(updated_at) FROM orchestration_v2_projection_messages WHERE thread_id=?", ("child",)))
        self.assertTrue(any("SCAN orchestration_v2_projection_turn_items" in row[3] for row in old_output_plan))
        self.assertTrue(any("SEARCH orchestration_v2_projection_turn_items" in row[3] for row in new_output_plan))
        self.assertTrue(any("SCAN orchestration_v2_projection_messages" in row[3] for row in old_message_plan))
        self.assertTrue(any("SEARCH orchestration_v2_projection_messages" in row[3] for row in new_message_plan))

    def test_subagent_upload_log_includes_sqlite_error_text(self):
        error = sqlite3.OperationalError("no such column: output.updated_at")
        with patch.object(collector, "send_subagents", side_effect=error), \
             patch.object(collector, "collect_t3_subagents", return_value=[]), \
             patch.object(collector, "scan_jsonl"), patch.object(collector, "scan_liveness"):
            now = collector.time.monotonic()
            state = {"last_subagent_discovery": now - collector.SUBAGENT_DISCOVERY_INTERVAL}
            with self.assertLogs("lighthouse.collector", level="WARNING") as captured:
                collector.run_cycle("http://fixture", "fixture-token", state, last_discovery=now)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("error=OperationalError", captured.output[0])
        self.assertIn("message=no such column: output.updated_at", captured.output[0])
        self.assertIn("stage=upload", captured.output[0])
        self.assertRegex(captured.output[0], r"elapsed_seconds=\d+\.\d{3}")
        self.assertNotIn("fixture-token", captured.output[0])

    def test_successful_subagent_upload_has_a_data_free_log(self):
        with patch.object(collector, "send_subagents"), \
             patch.object(collector, "collect_t3_subagents", return_value=[{"child_t3_thread_id": "private-child"}]), \
             patch.object(collector, "scan_jsonl"), patch.object(collector, "scan_liveness"):
            now = collector.time.monotonic()
            state = {"last_subagent_discovery": now - collector.SUBAGENT_DISCOVERY_INTERVAL}
            with self.assertLogs("lighthouse.collector", level="INFO") as captured:
                collector.run_cycle("http://fixture", "fixture-token", state, last_discovery=now)
        self.assertRegex(captured.output[0],
                         r"INFO:lighthouse\.collector:subagent discovery upload succeeded children=1 read_seconds=\d+\.\d{3} upload_seconds=\d+\.\d{3}")
        self.assertNotIn("private-child", captured.output[0])
        self.assertNotIn("fixture-token", captured.output[0])


if __name__ == "__main__":
    unittest.main()
