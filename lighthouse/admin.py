import argparse
import hashlib
import secrets
import sys
from pathlib import Path

from argon2 import PasswordHasher

from lighthouse.db import connection
from lighthouse.security import create_token_file

DEFAULT_SCOPES = "project:read,project:write,project:discover,project:brief,task:write,event:write,thread:write,controller:write,controller:report,subagent:report,incident:read,incident:ack,mac_alert:deliver"


def issue(name, kind, scopes, path):
    token = "lh_" + secrets.token_urlsafe(40)
    token_hash = PasswordHasher().hash(token)
    token_lookup_hash = hashlib.sha256(token.encode()).hexdigest()
    with connection() as conn:
        conn.execute("INSERT INTO principals(name,kind,permission_scopes,token_hash,token_lookup_hash) VALUES(%s,%s,%s,%s,%s)", (name, kind, scopes, token_hash, token_lookup_hash))
    create_token_file(path, token)
    print(f"created principal {name}; token written to protected file")


def index_legacy(name, path):
    token = Path(path).read_text().strip()
    with connection() as conn:
        row = conn.execute("SELECT id,token_hash FROM principals WHERE name=%s AND enabled=true", (name,)).fetchone()
        if not row or not PasswordHasher().verify(row["token_hash"], token):
            raise SystemExit("token does not match the named enabled principal")
        digest = hashlib.sha256(token.encode()).hexdigest()
        conn.execute("UPDATE principals SET token_lookup_hash=%s WHERE id=%s", (digest, row["id"]))
    print(f"indexed token lookup for {name}")


def check_summaries():
    with connection() as conn:
        row = conn.execute("""
          WITH expected AS (
            SELECT project_id,
              to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900) AS bucket_at,
              source, parent_thread_id, max(observed_at) AS last_observed_at
            FROM events
            WHERE project_id IS NOT NULL
            GROUP BY project_id, to_timestamp(floor(extract(epoch FROM observed_at) / 900) * 900), source, parent_thread_id
          ), actual AS (
            SELECT project_id, bucket_at, source, parent_thread_id, last_observed_at
            FROM project_event_summary
          ), differences AS (
            (SELECT * FROM expected EXCEPT SELECT * FROM actual)
            UNION ALL
            (SELECT * FROM actual EXCEPT SELECT * FROM expected)
          )
          SELECT count(DISTINCT project_id) AS mismatches FROM differences
        """).fetchone()
    print(f"summary_mismatches={row['mismatches']}")


def repair_summaries():
    with connection() as conn:
        conn.execute("SELECT refresh_all_project_summaries()")
    print("project summaries refreshed")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lighthouse-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, default_kind in (("bootstrap", "user"), ("issue-principal", "agent")):
        item = sub.add_parser(command)
        item.add_argument("--name", required=True)
        item.add_argument("--kind", default=default_kind, choices=["user", "agent", "service"])
        scopes_default = "project:read,project:write,source:bind,task:write,event:write,task:review,incident:read,incident:ack" if default_kind == "user" else DEFAULT_SCOPES
        item.add_argument("--scopes", default=scopes_default)
        item.add_argument("--token-file", required=True)
    indexer = sub.add_parser("index-legacy-token")
    indexer.add_argument("--name", required=True)
    indexer.add_argument("--token-file", required=True)
    sub.add_parser("check-summaries")
    sub.add_parser("repair-summaries")
    args = parser.parse_args(argv)
    if args.command == "index-legacy-token":
        index_legacy(args.name, args.token_file)
    elif args.command == "check-summaries":
        check_summaries()
    elif args.command == "repair-summaries":
        repair_summaries()
    else:
        issue(args.name, args.kind, [s for s in args.scopes.split(",") if s], args.token_file)


if __name__ == "__main__":
    main()
