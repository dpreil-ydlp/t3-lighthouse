"""Apply database migrations that the PostgreSQL image does not run on first start.

The database image runs 001-003 when it creates the volume. This runner applies 004 onward,
records each file in schema_migrations, and skips files already recorded. It runs as the
database owner because the migrations create tables, functions, and grants.
"""
import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"
INITDB_MIGRATIONS = ("001_initial.sql", "002_roles.sql", "003_auth_index.sql")
NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")
LOCK_ID = 51821


def root_url():
    explicit = os.environ.get("LIGHTHOUSE_ROOT_DATABASE_URL")
    if explicit:
        return explicit
    secret = Path(os.environ.get("LIGHTHOUSE_DB_PASSWORD_FILE", "/run/secrets/db-password"))
    password = secret.read_text().strip()
    host = os.environ.get("LIGHTHOUSE_DB_HOST", "db")
    name = os.environ.get("LIGHTHOUSE_DB_NAME", "lighthouse")
    return f"postgresql://lighthouse_root:{quote(password, safe='')}@{host}:5432/{name}"


def migration_files():
    files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    bad = [name for name in files if not NAME.match(name)]
    if bad:
        raise SystemExit(f"unexpected migration file names: {', '.join(bad)}")
    return files


def column_exists(conn, table, column):
    return conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    ).fetchone() is not None


def baseline(conn, files):
    """Record what an install made before this runner existed already applied."""
    if conn.execute("SELECT to_regclass('public.principals')").fetchone()[0] is None:
        raise SystemExit("the database has no Lighthouse schema; the database container did not initialise it")
    if column_exists(conn, "project_briefs", "schema_version"):
        applied = files
    elif not column_exists(conn, "unmapped_observations", "resolved_project_id"):
        applied = [name for name in files if name in INITDB_MIGRATIONS]
    else:
        raise SystemExit(
            "this database has some migrations from 004 onward but not all of them. "
            "Apply the missing files by hand, then run: migrate --mark-applied-through <last applied file>"
        )
    for name in applied:
        conn.execute("INSERT INTO schema_migrations(name) VALUES(%s) ON CONFLICT DO NOTHING", (name,))
    return applied


def migrate(url=None, mark_through=None, out=sys.stdout):
    files = migration_files()
    with psycopg.connect(url or root_url(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            fresh = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0] is None
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            if mark_through:
                if mark_through not in files:
                    raise SystemExit(f"unknown migration {mark_through}")
                for name in files[: files.index(mark_through) + 1]:
                    conn.execute("INSERT INTO schema_migrations(name) VALUES(%s) ON CONFLICT DO NOTHING", (name,))
                print(f"marked migrations through {mark_through} as applied", file=out)
            elif fresh:
                baseline(conn, files)
            done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
            pending = [name for name in files if name not in done]
            for name in pending:
                if name in INITDB_MIGRATIONS:
                    raise SystemExit(f"{name} is applied by the database image on first start, not by this runner")
                sql = (MIGRATIONS / name).read_text()
                # Files without their own BEGIN/COMMIT run with the record insert in one implicit transaction.
                conn.execute(sql + f"\n;INSERT INTO schema_migrations(name) VALUES('{name}');", prepare=False)
                print(f"applied {name}", file=out)
            print(f"database schema is current ({len(files)} migrations)", file=out)
            return pending
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="migrate")
    parser.add_argument("--mark-applied-through", metavar="FILE", help="record FILE and every earlier migration as applied without running them")
    args = parser.parse_args(argv)
    migrate(mark_through=args.mark_applied_through)


if __name__ == "__main__":
    main()
