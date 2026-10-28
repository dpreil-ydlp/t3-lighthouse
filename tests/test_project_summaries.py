from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
import pytest

from conftest import make_project
from lighthouse.db import connection


def _summary_rows(project_id):
    with connection() as conn:
        expected = conn.execute("""
          SELECT to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900) AS bucket_at,
            source, parent_thread_id, max(observed_at) AS last_observed_at
          FROM events WHERE project_id=%s
          GROUP BY to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900), source, parent_thread_id
          ORDER BY bucket_at, source, parent_thread_id NULLS FIRST
        """, (project_id,)).fetchall()
        actual = conn.execute("""
          SELECT bucket_at, source, parent_thread_id, last_observed_at
          FROM project_event_summary WHERE project_id=%s
          ORDER BY bucket_at, source, parent_thread_id NULLS FIRST
        """, (project_id,)).fetchall()
    return expected, actual


def test_database_connections_disable_jit():
    with connection() as conn:
        assert conn.execute("SHOW jit").fetchone()["jit"] == "off"


def test_project_event_index_covers_list_aggregates():
    with connection() as conn:
        definition = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname='events_project_time_idx'"
        ).fetchone()["indexdef"]
        assert "include (source, parent_thread_id)" in definition.lower()


def test_event_insert_updates_project_event_summary(client, headers):
    project = make_project(client, headers, "event summary")
    observed_at = datetime.now(timezone.utc)
    response = client.post(
        "/api/v1/events",
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "project_id": project["id"],
            "source": "mcp",
            "confidence": "high",
            "status": "running",
            "summary": "event summary event",
            "meaningful": True,
            "source_event_id": uuid.uuid4().hex,
            "observed_at": observed_at.isoformat(),
        },
    )
    assert response.status_code == 200
    expected_bucket = math.floor(observed_at.timestamp() / 900) * 900
    with connection() as conn:
        row = conn.execute(
            "SELECT source, parent_thread_id, last_observed_at, bucket_at "
            "FROM project_event_summary WHERE project_id=%s",
            (project["id"],),
        ).fetchone()
    assert row["source"] == "mcp"
    assert row["parent_thread_id"] is None
    assert row["last_observed_at"] == observed_at
    assert int(row["bucket_at"].timestamp()) == expected_bucket


def test_project_merge_rebuilds_source_and_target_summaries(client, headers, principals):
    source = make_project(client, headers, "summary merge source")
    target = make_project(client, headers, "summary merge target")
    controller = client.post(f"/api/v1/projects/{source['id']}/controllers", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={})
    assert controller.status_code == 200, controller.text
    thread = client.post(f"/api/v1/projects/{source['id']}/threads", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={
        "source": "mcp", "external_thread_id": "merge-" + uuid.uuid4().hex, "state": "running"
    })
    assert thread.status_code == 200, thread.text
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    with connection() as conn:
        for index in range(60):
            observed = now - timedelta(minutes=15 * index)
            conn.execute("""INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at)
              VALUES(%s,%s,'progress','mcp','high',%s,%s)""", (source["id"], thread.json()["parent_thread_id"], f"merge-source-{uuid.uuid4().hex}", observed))
        conn.execute("""INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at)
          VALUES(%s,'collector_observation','mcp','high',%s,%s)""", (target["id"], f"merge-target-{uuid.uuid4().hex}", now - timedelta(minutes=15 * 59)))
        conn.execute("""INSERT INTO project_briefs(project_id,about,now,done,"left",remaining_hours,eta_confidence,eta_basis,version)
          VALUES(%s,'Review','Check','[]','[]',10,'low','test',1),(%s,'Review','Check','[]','[]',10,'low','test',1)""", (source["id"], target["id"]))

    response = client.post(f"/api/v1/projects/{source['id']}/merge", headers={
        "Authorization": f"Bearer {principals['user'][0]}", "Idempotency-Key": uuid.uuid4().hex
    }, json={"into": target["id"]})
    assert response.status_code == 200, response.text
    source_expected, source_actual = _summary_rows(source["id"])
    target_expected, target_actual = _summary_rows(target["id"])
    assert source_actual == source_expected == []
    assert target_actual == target_expected
    assert {row["source"] for row in target_actual} == {"dashboard", "mcp"}
    assert len({row["parent_thread_id"] for row in target_actual if row["parent_thread_id"]}) == 1
    listed = client.get("/api/v1/projects?view=all", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["items"]
    merged = next(item for item in listed if item["id"] == target["id"])
    assert merged["tools"] == ["dashboard", "mcp"]
    assert merged["active_threads"] == 1
    details = client.get(f"/api/v1/projects/{target['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()
    assert details["project"]["brief"]["left"] == []
    assert details["project"]["brief"]["state"] == "agents_working"
    assert not ({"pace_hours_per_day", "eta_date", "eta_confidence", "remaining_hours"} & set(details["project"]["brief"]))


def test_summary_migration_locks_events_and_installs_trigger_before_backfill():
    migration = (Path(__file__).parents[1] / "db/migrations/013_cr7_project_event_summary.sql").read_text()
    lock = migration.index("LOCK TABLE events IN SHARE ROW EXCLUSIVE MODE")
    trigger = migration.index("CREATE TRIGGER events_maintain_project_event_summary")
    backfill = migration.index("INSERT INTO project_event_summary", trigger)
    commit = migration.rindex("COMMIT;")
    assert migration.index("BEGIN;") < lock < trigger < backfill < commit


def test_summary_migration_blocks_insert_until_backfill_and_trigger_commit():
    password_file = Path(os.environ.get("LIGHTHOUSE_TEST_DB_PASSWORD_FILE", "/run/secrets/db-password"))
    password = password_file.read_text().strip()
    params = conninfo_to_dict(os.environ["DATABASE_URL"])
    params.update(host="db", port=5432, user="lighthouse_root", password=password)
    migration = (Path(__file__).parents[1] / "db/migrations/013_cr7_project_event_summary.sql").read_text()
    script = migration[:migration.rindex("COMMIT;")]
    with psycopg.connect(**params, autocommit=True, row_factory=dict_row) as conn:
        project_id = conn.execute("INSERT INTO projects(name) VALUES(%s) RETURNING id", ("summary migration lock " + uuid.uuid4().hex,)).fetchone()["id"]

    migration_conn = psycopg.connect(**params, autocommit=True)
    migration_conn.execute(script, prepare=False)
    inserted = threading.Event()
    attempting = threading.Event()
    failure = []

    def insert_event():
        try:
            with psycopg.connect(**params) as conn:
                attempting.set()
                conn.execute("""INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at)
                  VALUES(%s,'collector_observation','migration-probe','medium',%s,now())""", (project_id, "migration-race-" + uuid.uuid4().hex))
            inserted.set()
        except Exception as exc:  # captured so the assertion reports a failed writer
            failure.append(exc)
            inserted.set()

    writer = threading.Thread(target=insert_event)
    writer.start()
    assert attempting.wait(timeout=5), "event writer did not start"
    try:
        time.sleep(0.2)
        assert writer.is_alive(), "event insert passed the migration lock before trigger installation committed"
    finally:
        migration_conn.execute("COMMIT")
    writer.join(timeout=5)
    migration_conn.close()
    assert inserted.is_set() and not writer.is_alive(), "event insert did not finish after migration commit"
    assert not failure, f"event insert failed: {failure!r}"
    expected, actual = _summary_rows(str(project_id))
    assert actual == expected and len(actual) == 1


def test_admin_summary_check_counts_drift_and_repair_is_idempotent(client, headers, capsys):
    from lighthouse import admin

    project = make_project(client, headers, "summary admin summaries")
    observed_at = datetime.now(timezone.utc)
    with connection() as conn:
        conn.execute("""INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at)
          VALUES(%s,'collector_observation','admin-probe','medium',%s,%s)""", (project["id"], "admin-summary-" + uuid.uuid4().hex, observed_at))
    admin.main(["check-summaries"])
    assert capsys.readouterr().out.strip() == "summary_mismatches=0"

    with connection() as conn:
        conn.execute("DELETE FROM project_event_summary WHERE project_id=%s", (project["id"],))
    admin.main(["check-summaries"])
    assert capsys.readouterr().out.strip() == "summary_mismatches=1"

    admin.main(["repair-summaries"])
    assert capsys.readouterr().out.strip() == "project summaries refreshed"
    admin.main(["repair-summaries"])
    assert capsys.readouterr().out.strip() == "project summaries refreshed"
    admin.main(["check-summaries"])
    assert capsys.readouterr().out.strip() == "summary_mismatches=0"


def test_upgrade_migration_015_serializes_concurrent_insert_with_repair():
    password_file = Path(os.environ.get("LIGHTHOUSE_TEST_DB_PASSWORD_FILE", "/run/secrets/db-password"))
    password = password_file.read_text().strip()
    params = conninfo_to_dict(os.environ["DATABASE_URL"])
    params.update(host="db", port=5432, user="lighthouse_root", password=password, application_name="summary-015-test")
    root = Path(__file__).parents[1]
    migration_013 = (root / "db/migrations/013_cr7_project_event_summary.sql").read_text()
    migration_014 = (root / "db/migrations/014_cr7_project_summary_repair.sql").read_text()
    migration_015 = (root / "db/migrations/015_cr7_project_summary_insert_lock.sql").read_text()
    function_match = re.search(
        r"CREATE OR REPLACE FUNCTION maintain_project_event_summary\(\) RETURNS trigger.*?\n\$\$;",
        migration_013,
        re.DOTALL,
    )
    assert function_match, "migration 013 must define the fresh-install insert function"

    legacy_function = """CREATE OR REPLACE FUNCTION maintain_project_event_summary() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    BEGIN
      IF NEW.project_id IS NOT NULL THEN
        INSERT INTO public.project_event_summary(project_id,bucket_at,source,parent_thread_id,last_observed_at)
        VALUES (NEW.project_id,to_timestamp(floor(extract(epoch FROM NEW.observed_at)/900)*900),NEW.source,NEW.parent_thread_id,NEW.observed_at)
        ON CONFLICT (project_id,bucket_at,source,parent_thread_id)
        DO UPDATE SET last_observed_at=GREATEST(project_event_summary.last_observed_at,EXCLUDED.last_observed_at);
      END IF;
      RETURN NEW;
    END;
    $$"""

    def function_definition(conn):
        return conn.execute("SELECT pg_get_functiondef('public.maintain_project_event_summary()'::regprocedure)").fetchone()[0]

    def race(project_label):
        with psycopg.connect(**params, autocommit=True, row_factory=dict_row) as conn:
            project_id = conn.execute("INSERT INTO projects(name) VALUES(%s) RETURNING id", (project_label + uuid.uuid4().hex,)).fetchone()["id"]
            for source in ("repair-a", "repair-b"):
                conn.execute("""INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at)
                  VALUES(%s,'collector_observation',%s,'medium',%s,now())""", (project_id, source, uuid.uuid4().hex))
            conn.execute("DELETE FROM project_event_summary WHERE project_id=%s AND source='repair-b'", (project_id,))

        row_lock = psycopg.connect(**params, autocommit=False)
        row_lock.execute("SELECT 1 FROM project_event_summary WHERE project_id=%s AND source='repair-a' FOR UPDATE", (project_id,))
        repair_done = threading.Event()
        writer_attempting = threading.Event()
        writer_done = threading.Event()
        errors = []

        def repair():
            try:
                with psycopg.connect(**{**params, "application_name": "summary-015-repair"}) as conn:
                    conn.execute("SELECT refresh_project_summary(%s)", (project_id,))
            except Exception as exc:
                errors.append(exc)
            finally:
                repair_done.set()

        def insert_during_repair():
            try:
                with psycopg.connect(**{**params, "application_name": "summary-015-writer"}) as conn:
                    writer_attempting.set()
                    conn.execute("""INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at)
                      VALUES(%s,'collector_observation','repair-c','medium',%s,now())""", (project_id, uuid.uuid4().hex))
            except Exception as exc:
                errors.append(exc)
            finally:
                writer_done.set()

        repair_thread = threading.Thread(target=repair)
        writer_thread = threading.Thread(target=insert_during_repair)
        repair_thread.start()
        deadline = time.monotonic() + 5
        waiting = False
        while time.monotonic() < deadline:
            with psycopg.connect(**params, autocommit=True) as conn:
                waiting = bool(conn.execute("""SELECT 1 FROM pg_stat_activity
                  WHERE application_name='summary-015-repair' AND wait_event_type='Lock'""").fetchone())
            if waiting:
                break
            time.sleep(0.02)
        assert waiting, "repair did not block on the locked summary row"

        writer_thread.start()
        assert writer_attempting.wait(5), "concurrent insert did not start"
        writer_waiting = False
        writer_deadline = time.monotonic() + 5
        while time.monotonic() < writer_deadline and not writer_done.is_set():
            with psycopg.connect(**params, autocommit=True) as conn:
                writer_waiting = bool(conn.execute("""SELECT 1 FROM pg_stat_activity
                  WHERE application_name='summary-015-writer' AND wait_event_type='Lock'""").fetchone())
            if writer_waiting:
                break
            time.sleep(0.02)
        writer_finished_before_repair = writer_done.is_set()
        row_lock.commit()
        row_lock.close()
        repair_thread.join(10)
        writer_thread.join(10)
        assert repair_done.is_set() and writer_done.is_set(), "repair and insert did not finish"
        assert writer_waiting or writer_finished_before_repair, "insert neither waited nor finished during repair"

        with psycopg.connect(**params, autocommit=True) as conn:
            differences = conn.execute("""WITH expected AS (
              SELECT project_id,to_timestamp(floor(extract(epoch FROM observed_at)/900)*900) bucket_at,
                source,parent_thread_id,max(observed_at) last_observed_at
              FROM events WHERE project_id=%s
              GROUP BY project_id,to_timestamp(floor(extract(epoch FROM observed_at)/900)*900),source,parent_thread_id
            ), differences AS (
              (SELECT * FROM expected EXCEPT SELECT * FROM project_event_summary WHERE project_id=%s)
              UNION ALL
              (SELECT * FROM project_event_summary WHERE project_id=%s EXCEPT SELECT * FROM expected)
            ) SELECT count(*) FROM differences""", (project_id, project_id, project_id)).fetchone()[0]
        return writer_finished_before_repair, errors, differences

    # The old 013 trigger lets the writer insert while repair holds its delete snapshot.
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(legacy_function, prepare=False)
        conn.execute("DROP TRIGGER IF EXISTS events_maintain_project_event_summary ON events")
        conn.execute("CREATE TRIGGER events_maintain_project_event_summary AFTER INSERT ON events FOR EACH ROW EXECUTE FUNCTION maintain_project_event_summary()")
    legacy_race = race("summary 015 legacy path ")
    assert legacy_race[0], "old 013 must reproduce the concurrent writer race"
    assert legacy_race[1] and "project_event_summary_bucket_key" in str(legacy_race[1][0])
    assert legacy_race[2] == 1, "old 013 race must leave the original summary drift"

    # Fresh installation must retain the 013 function body after applying 014 and 015.
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(function_match.group(0), prepare=False)
        conn.execute(migration_014, prepare=False)
        conn.execute(migration_015, prepare=False)
        fresh_definition = function_definition(conn)
        conn.execute("SELECT refresh_all_project_summaries()")

    fresh_race = race("summary 015 fresh path ")
    assert not fresh_race[0], "the fresh insert trigger must wait for summary repair"
    assert not fresh_race[1], f"fresh repair and insert failed: {fresh_race[1]!r}"
    assert fresh_race[2] == 0, "fresh repair and concurrent insert must leave zero summary differences"

    # An already-upgraded database must converge to the same installed definition.
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(legacy_function, prepare=False)
        conn.execute(migration_014, prepare=False)
        conn.execute(migration_015, prepare=False)
        upgraded_definition = function_definition(conn)
        conn.execute("SELECT refresh_all_project_summaries()")
    assert upgraded_definition == fresh_definition

    upgraded_race = race("summary 015 upgraded path ")
    assert not upgraded_race[0], "the upgraded insert trigger must wait for summary repair"
    assert not upgraded_race[1], f"upgraded repair and insert failed: {upgraded_race[1]!r}"
    assert upgraded_race[2] == 0, "upgraded repair and concurrent insert must leave zero summary differences"
