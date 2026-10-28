import io
import os
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

from conftest import make_project
from lighthouse import demo, migrate
from lighthouse.worker import evaluate_stalls


def root_test_url():
    password = Path(os.environ.get("LIGHTHOUSE_TEST_DB_PASSWORD_FILE", "/run/secrets/db-password")).read_text().strip()
    return f"postgresql://lighthouse_root:{quote(password, safe='')}@db:5432/lighthouse_test"


def test_migrate_baselines_a_fully_migrated_database_and_is_idempotent():
    out = io.StringIO()
    assert migrate.migrate(url=root_test_url(), out=out) == []
    assert migrate.migrate(url=root_test_url(), out=out) == []
    with psycopg.connect(root_test_url()) as conn:
        recorded = [row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    assert recorded == migrate.migration_files()
    assert "database schema is current" in out.getvalue()


def test_demo_load_shows_projects_and_remove_leaves_real_data(client, headers, principals):
    owner = {"Authorization": f"Bearer {principals['user'][0]}"}
    real = make_project(client, headers, "real project")
    with psycopg.connect(root_test_url(), row_factory=dict_row) as conn:
        assert demo.load(conn) == len(demo.PROJECTS)
    try:
        evaluate_stalls()
        items = client.get("/api/v1/projects", headers=owner).json()["items"]
        names = {item["name"]: item for item in items}
        assert {spec["name"] for spec in demo.PROJECTS} <= set(names)
        assert names["Pantry Notes"]["activity_state"] == "working_now"
        assert names["Pantry Notes"]["brief"]["needs_you"]["action"] == "Approve the new recipe card layout"
        assert names["Weather Kiosk"]["activity_state"] == "quiet"
        incidents = client.get("/api/v1/incidents", headers=owner).json()["items"]
        streak = [i for i in incidents if i["project_name"] == "Streak"]
        assert [i["state"] for i in streak] == ["open"]
        assert streak[0]["task_title"] == "Offline sync for check-ins"
        detail = client.get(f"/api/v1/projects/{names['Streak']['id']}", headers=owner).json()
        assert len(detail["threads"]) == 2
    finally:
        with psycopg.connect(root_test_url(), row_factory=dict_row) as conn:
            assert demo.remove(conn) == len(demo.PROJECTS)
            assert demo.remove(conn) == 0
            assert conn.execute("SELECT count(*) n FROM thread_titles WHERE external_thread_id LIKE 'demo-%'").fetchone()["n"] == 0
    ids = {item["id"] for item in client.get("/api/v1/projects", headers=owner).json()["items"]}
    assert real["id"] in ids


def test_demo_data_is_fictional():
    text = repr(demo.PROJECTS).lower()
    for private in ("/home/", "t3code://", "@"):
        assert private not in text
    assert all(spec["folder"] and " " not in spec["folder"] for spec in demo.PROJECTS)
