import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def database_url():
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        url = explicit
    else:
        secret_path = os.environ.get("LIGHTHOUSE_DB_PASSWORD_FILE", "/run/secrets/db-password")
        if os.path.exists(secret_path):
            password = open(secret_path).read().strip()
            url = f"postgresql://lighthouse_api:{password}@db:5432/lighthouse"
        else:
            raise RuntimeError("DATABASE_URL or LIGHTHOUSE_DB_PASSWORD_FILE must be configured")
    if os.environ.get("PYTEST_CURRENT_TEST") and conninfo_to_dict(url).get("dbname") != "lighthouse_test":
        raise RuntimeError("pytest database connections must use lighthouse_test")
    return url


@contextmanager
def connection():
    with psycopg.connect(database_url(), row_factory=dict_row, options="-c jit=off") as conn:
        yield conn


def json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def row_json(row):
    return {key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in dict(row).items()}


def init_db():
    with connection() as conn:
        conn.execute("SELECT 1")
