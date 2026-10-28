import secrets
import uuid
import hashlib
import os
from pathlib import Path
from urllib.parse import quote

import pytest
import psycopg
from psycopg.conninfo import conninfo_to_dict
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from lighthouse.api import app
from lighthouse.db import connection


@pytest.fixture(scope="session", autouse=True)
def disposable_test_database():
    """Create a clean DB for this pytest process; never use the deployed DB."""
    password_path = Path(os.environ.get("LIGHTHOUSE_TEST_DB_PASSWORD_FILE", "/run/secrets/db-password"))
    if not password_path.is_file():
        pytest.fail("test database password file is required")
    password = password_path.read_text().strip()
    database_name = os.environ.get("LIGHTHOUSE_TEST_DATABASE_NAME", "lighthouse_test")
    live_name = os.environ.get("LIGHTHOUSE_LIVE_DATABASE_NAME", "lighthouse")
    if database_name == live_name or database_name != "lighthouse_test":
        pytest.fail("test database name must be lighthouse_test and differ from the live database")

    admin_params = {"host": "db", "port": 5432, "user": "lighthouse_root", "password": password, "dbname": "postgres"}
    with psycopg.connect(**admin_params, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database_name,)).fetchone()
        if exists:
            pytest.fail("lighthouse_test already exists; refusing to touch a database not created by this run")
        admin.execute(f'CREATE DATABASE "{database_name}"')

    created = True
    try:
        test_admin = {**admin_params, "dbname": database_name}
        with psycopg.connect(**test_admin) as conn:
            conn.execute((Path(__file__).parents[1] / "db/migrations/001_initial.sql").read_text(), prepare=False)
            for migration in ("003_auth_index.sql", "004_cr1.sql", "005_round2_guards.sql", "006_cr3.sql", "007_cr3_project_threads_index.sql", "008_thread_title_origin.sql", "009_cr4_briefs.sql", "010_cr6_t3_links.sql", "011_cr6_nullable_t3_titles.sql", "012_cr7_project_events_cover.sql", "013_cr7_project_event_summary.sql", "014_cr7_project_summary_repair.sql", "015_cr7_project_summary_insert_lock.sql", "016_cr9_derived_tasks.sql", "017_cr9_t3_mcp.sql", "018_cr11_typed_brief_items.sql"):
                conn.execute((Path(__file__).parents[1] / "db/migrations" / migration).read_text(), prepare=False)
            conn.execute("""
                GRANT CONNECT ON DATABASE lighthouse_test TO lighthouse_api;
                GRANT USAGE ON SCHEMA public TO lighthouse_api;
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lighthouse_api;
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lighthouse_api;
                REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON events FROM lighthouse_api;
                REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON project_brief_history FROM lighthouse_api;
            """, prepare=False)
        live_url = f"postgresql://lighthouse_api:{quote(password)}@db:5432/{live_name}"
        test_url = f"postgresql://lighthouse_api:{quote(password)}@db:5432/{database_name}"
        os.environ["LIGHTHOUSE_TEST_LIVE_DATABASE_URL"] = live_url
        os.environ["DATABASE_URL"] = test_url
        yield
    finally:
        if created:
            with psycopg.connect(**admin_params, autocommit=True) as admin:
                admin.execute(f'DROP DATABASE "{database_name}"')


def assert_test_database_is_isolated():
    test_url = os.environ.get("DATABASE_URL", "")
    live_url = os.environ.get("LIGHTHOUSE_TEST_LIVE_DATABASE_URL", "")
    if not test_url or not live_url or test_url == live_url:
        pytest.fail("test suite database URL must differ from the live database URL", pytrace=False)
    if conninfo_to_dict(test_url).get("dbname") != "lighthouse_test":
        pytest.fail("test suite database must use lighthouse_test", pytrace=False)


class SecretString(str):
    def __repr__(self):
        return "[REDACTED_TEST_TOKEN]"


class SafeHeaders(dict):
    def __repr__(self):
        return "{'Authorization': '[REDACTED_TEST_TOKEN]'}"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def principals():
    agent_token = "lh_" + secrets.token_urlsafe(24)
    user_token = "lh_" + secrets.token_urlsafe(24)
    suffix = uuid.uuid4().hex
    hasher = PasswordHasher()
    with connection() as conn:
        agent = conn.execute("INSERT INTO principals(name,kind,permission_scopes,token_hash,token_lookup_hash) VALUES(%s,'agent',%s,%s,%s) RETURNING id", ("test-agent-"+suffix, ["project:read","project:write","project:discover","project:brief","task:write","event:write","thread:write","controller:write","incident:read","incident:ack","mac_alert:deliver"], hasher.hash(agent_token), hashlib.sha256(agent_token.encode()).hexdigest())).fetchone()
        user = conn.execute("INSERT INTO principals(name,kind,permission_scopes,token_hash,token_lookup_hash) VALUES(%s,'user',%s,%s,%s) RETURNING id", ("test-user-"+suffix, ["project:read","project:write","source:bind","task:write","event:write","task:review","incident:read","incident:ack"], hasher.hash(user_token), hashlib.sha256(user_token.encode()).hexdigest())).fetchone()
    return {"agent": (SecretString(agent_token), str(agent["id"])), "user": (SecretString(user_token), str(user["id"])), "prefix": suffix}


@pytest.fixture
def headers(principals):
    return SafeHeaders({"Authorization": f"Bearer {principals['agent'][0]}"})


def make_project(client, headers, name="test project"):
    result = client.post("/api/v1/projects", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"name":name+"-"+uuid.uuid4().hex[:8],"kind":"research","next_action":"Inspect test result"})
    assert result.status_code == 200, result.text
    return result.json()["project"]
