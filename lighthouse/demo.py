"""Load or remove a fictional demo dataset.

Every project, folder, session, and person here is made up. Demo projects carry
origin='demo' and demo sessions use thread IDs that start with 'demo-', so `remove`
deletes exactly what `load` created and leaves real data alone. Sessions listed under
"t3_threads" get a made-up T3 Code thread, so the dashboard shows its "Open in T3" links;
the demo T3 environment is set only when no real one is known.

Run inside the API container:  python -m lighthouse.demo load | remove
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lighthouse.migrate import root_url

DEMO_T3_ENVIRONMENT = "demo-environment"
M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)

# Each session: (source, id, title, minutes ago for each recorded turn).
PROJECTS = [
    {
        "name": "Pantry Notes",
        "folder": "pantry-notes",
        "kind": "web",
        "description": "Recipe site that turns what is in your cupboard into a weekly meal plan.",
        "sessions": [
            ("claude_code", "demo-pn-1", "Add ingredient substitutions to recipe pages", [2, 5, 9, 14, 21, 33, 48]),
            ("codex", "demo-pn-2", "Fix shopping list totals for metric units", [6, 18, 41, 95]),
            ("claude_code", "demo-pn-3", "Write tests for the meal planner", [26 * 60, 27 * 60]),
        ],
        "progress": [
            (4, "Substitution table renders on 212 of 214 recipes"),
            (40, "Shopping list now sums grams and millilitres separately"),
            (26 * 60, "Meal planner tests pass on CI"),
        ],
        "tasks": [("Ingredient substitutions", "running", 4, 1800)],
        "brief": {
            "about": "A recipe site that plans a week of meals from what is already in your pantry.",
            "now": "Adding ingredient substitutions to every recipe page.",
            "done": [
                "Weekly planner picks meals from pantry stock",
                "Shopping list groups items by store aisle",
                "Metric and imperial units convert both ways",
            ],
            "left": [
                {"text": "Finish substitutions for the last two recipes", "who": "agents", "hours": 1.5},
                {"text": "Approve the new recipe card layout", "who": "you"},
                {"text": "Add print styles for recipe cards", "who": "agents", "hours": 2},
            ],
            "eta_basis": "Two small agent tasks and one review from you.",
        },
    },
    {
        "name": "Streak",
        "folder": "streak-app",
        "kind": "mobile",
        "description": "Habit tracker for iOS and Android with offline sync.",
        "sessions": [
            ("codex", "demo-st-1", "Offline sync for habit check-ins", [52, 58, 66, 80, 110]),
            ("claude_code", "demo-st-2", "Reminder notifications on Android 15", [3 * 60, 4 * 60]),
        ],
        "progress": [
            (52, "Conflict rules written for check-ins made on two devices"),
            (4 * 60, "Reminders fire on time on the Android emulator"),
        ],
        "t3_threads": {"demo-st-1", "demo-st-2"},
        # This task stops reporting progress, so the worker opens a stall incident for it.
        "tasks": [("Offline sync for check-ins", "running", 52, 1800), ("Reminder notifications", "running", 4 * 60, 6 * 3600)],
        "brief": {
            "about": "A habit tracker for phones that keeps working without a connection.",
            "now": "Offline sync stopped moving after the conflict rules were written.",
            "done": [
                "Daily, weekly, and custom habit schedules",
                "Streak history chart",
                "Conflict rules for check-ins made on two devices",
            ],
            "left": [
                {"text": "Decide whether a check-in on a second phone overrides the first", "who": "you"},
                {"text": "Finish the sync queue and retry logic", "who": "agents", "hours": 4},
                {"text": "TestFlight review", "who": "outside"},
            ],
            "eta_basis": "Sync work waits on your decision about conflicts.",
        },
    },
    {
        "name": "Fieldguide Docs",
        "folder": "fieldguide-docs",
        "kind": "docs",
        "description": "Documentation site for a fictional open-source mapping library.",
        "sessions": [
            ("codex", "demo-fg-1", "Rewrite the getting started guide", [3 * 60, 3 * 60 + 20, 5 * 60]),
            ("claude_code", "demo-fg-2", "Generate API reference from type comments", [7 * 60, 9 * 60]),
        ],
        "progress": [
            (3 * 60, "Getting started guide rewritten and checked against v4.2"),
            (9 * 60, "API reference builds from source comments"),
        ],
        "tasks": [],
        "brief": {
            "about": "Docs for Fieldguide, an open-source mapping library.",
            "now": "Getting started guide is rewritten; the API reference is next.",
            "done": ["Getting started guide for v4.2", "Search across all pages", "Dark mode"],
            "left": [
                {"text": "Fill in examples for the 18 undocumented functions", "who": "agents", "hours": 3},
                {"text": "Review the new landing page copy", "who": "you"},
            ],
            "eta_basis": "Examples are mechanical; the review is yours.",
        },
    },
    {
        "name": "tidyx",
        "folder": "tidyx",
        "kind": "cli",
        "description": "Command-line tool that renames and sorts downloaded files by rules.",
        "sessions": [
            ("claude_code", "demo-tx-1", "Add a dry-run flag", [2 * 24 * 60, 2 * 24 * 60 + 30]),
            ("codex", "demo-tx-2", "Package for Homebrew", [4 * 24 * 60]),
        ],
        "progress": [(2 * 24 * 60, "Dry run prints every rename without touching files")],
        "tasks": [],
        "brief": {
            "about": "A command-line tool that sorts and renames downloaded files with simple rules.",
            "now": "Dry-run mode is done; packaging is next.",
            "done": ["Rule file format", "Dry-run flag", "Undo log"],
            "left": [{"text": "Publish the Homebrew formula", "who": "agents", "hours": 1}],
            "eta_basis": "One packaging task left.",
        },
        "resolved_incident": True,
    },
    {
        "name": "Weather Kiosk",
        "folder": "weather-kiosk",
        "kind": "hardware",
        "description": "Raspberry Pi display that shows the local forecast.",
        "sessions": [("claude_code", "demo-wk-1", "Night mode for the e-ink display", [12 * 24 * 60])],
        "progress": [(12 * 24 * 60, "Night mode dims the display after 10 pm")],
        "tasks": [],
        "brief": None,
    },
]


def project_ids(conn):
    return [row["id"] for row in conn.execute("SELECT id FROM projects WHERE origin='demo'")]


def remove(conn):
    ids = project_ids(conn)
    if not ids:
        return 0
    incidents = "SELECT id FROM incidents WHERE project_id = ANY(%(ids)s)"
    tasks = "SELECT id FROM tasks WHERE project_id = ANY(%(ids)s)"
    for sql in (
        f"DELETE FROM notification_outbox WHERE incident_id IN ({incidents})",
        f"DELETE FROM action_approvals WHERE incident_id IN ({incidents})",
        "DELETE FROM incidents WHERE project_id = ANY(%(ids)s)",
        f"DELETE FROM task_verifications WHERE task_id IN ({tasks})",
        "DELETE FROM events WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM subagent_registrations WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM milestones WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM tasks WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM thread_titles WHERE external_thread_id LIKE 'demo-%%'",
        "DELETE FROM parent_threads WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM controllers WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM project_sources WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM project_roots WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM project_brief_history WHERE project_id = ANY(%(ids)s)",
        "DELETE FROM project_briefs WHERE project_id = ANY(%(ids)s)",
        "UPDATE unmapped_observations SET resolved_project_id=NULL, bound_at=NULL WHERE resolved_project_id = ANY(%(ids)s)",
        "DELETE FROM projects WHERE id = ANY(%(ids)s)",
    ):
        conn.execute(sql, {"ids": ids})
    conn.execute("DELETE FROM t3_discovery_state WHERE environment_id=%s", (DEMO_T3_ENVIRONMENT,))
    return len(ids)


def event(conn, project_id, thread_id, task_id, event_type, source, confidence, source_event_id, at, details):
    conn.execute(
        """INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (project_id, task_id, thread_id, event_type, source, confidence, source_event_id, at, Jsonb(details)),
    )


def load(conn, now=None):
    now = now or datetime.now(timezone.utc)
    conn.execute("INSERT INTO t3_discovery_state(singleton,environment_id) VALUES(true,%s) ON CONFLICT(singleton) DO NOTHING",
                 (DEMO_T3_ENVIRONMENT,))
    for spec in PROJECTS:
        path = f"/Users/demo/Projects/{spec['folder']}"
        sessions = spec["sessions"]
        latest = now - min(m for s in sessions for m in s[3]) * M
        latest_progress = now - min(p[0] for p in spec["progress"]) * M
        project = conn.execute(
            """INSERT INTO projects(name,kind,description,origin,last_meaningful_progress_at,created_at,updated_at)
               VALUES(%s,%s,%s,'demo',%s,%s,%s) RETURNING id""",
            (spec["name"], spec["kind"], spec["description"], latest_progress, now - 30 * D, latest),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO project_roots(project_id,path,display_path,display_name,origin) VALUES(%s,%s,%s,%s,'folder')",
            (project, path, f"~/Projects/{spec['folder']}", spec["folder"]),
        )
        controller = conn.execute("INSERT INTO controllers(project_id) VALUES(%s) RETURNING id", (project,)).fetchone()["id"]
        threads = {}
        for source, external_id, title, turns in sessions:
            last = now - min(turns) * M
            thread = conn.execute(
                """INSERT INTO parent_threads(project_id,controller_id,source,external_thread_id,state,first_seen_at,
                     last_agent_event_at,last_meaningful_progress_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (project, controller, source, external_id, "running" if min(turns) < 15 else "idle",
                 now - max(turns) * M, last, last),
            ).fetchone()["id"]
            threads[external_id] = thread
            t3_thread = f"demo-t3-{external_id}" if external_id in spec.get("t3_threads", ()) else None
            conn.execute(
                "INSERT INTO thread_titles(source,external_thread_id,title,title_origin,t3_thread_id) VALUES(%s,%s,%s,%s,%s)",
                (source, external_id, title, "t3" if t3_thread else "native", t3_thread),
            )
            for n, minutes in enumerate(turns):
                event(conn, project, thread, None, "collector_observation", source, "medium", f"{external_id}:{n}",
                      now - minutes * M, {"summary": f"Observed {source} session activity", "record_type": "assistant", "working_directory": path})
        first_thread = threads[sessions[0][1]]
        for title, status, minutes, threshold in spec["tasks"]:
            at = now - minutes * M
            task = conn.execute(
                """INSERT INTO tasks(project_id,parent_thread_id,title,status,started_at,thresholds,
                     last_agent_event_at,last_meaningful_progress_at,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (project, first_thread, title, status, at - 3 * H, Jsonb({"meaningful_progress_seconds": threshold}),
                 at, at, at - 3 * H, at),
            ).fetchone()["id"]
            event(conn, project, first_thread, task, "progress", "mcp", "high", f"demo-task:{task}", at,
                  {"summary": f"Started: {title}", "status": status})
        for n, (minutes, summary) in enumerate(spec["progress"]):
            event(conn, project, first_thread, None, "progress", "mcp", "high", f"demo-progress:{project}:{n}",
                  now - minutes * M, {"summary": summary})
        if spec.get("resolved_incident"):
            opened = now - 3 * D
            conn.execute(
                """INSERT INTO incidents(project_id,parent_thread_id,kind,state,reason,opened_at,acknowledged_at,
                     resumed_progress_at,resolved_at,details)
                   VALUES(%s,%s,'stall','resolved','No meaningful progress within the task threshold',%s,%s,%s,%s,'{}')""",
                (project, first_thread, opened, opened + 6 * M, opened + 40 * M, opened + 40 * M),
            )
        brief = spec["brief"]
        if brief:
            body = {**brief, "version": 1, "schema_version": 2}
            conn.execute(
                """INSERT INTO project_briefs(project_id,about,now,done,"left",eta_basis,activity_through,updated_at,version,schema_version)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,2)""",
                (project, brief["about"], brief["now"], Jsonb(brief["done"]), Jsonb(brief["left"]),
                 brief["eta_basis"], latest, now - 6 * M),
            )
            conn.execute("INSERT INTO project_brief_history(project_id,version,body) VALUES(%s,1,%s)", (project, Jsonb(body)))
    return len(PROJECTS)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lighthouse.demo", description="Load or remove the fictional demo projects.")
    parser.add_argument("action", choices=["load", "remove"])
    args = parser.parse_args(argv)
    with psycopg.connect(root_url(), row_factory=dict_row) as conn:
        removed = remove(conn)
        if args.action == "load":
            count = load(conn)
            print(f"loaded {count} demo projects" + (f" (replaced {removed} older demo projects)" if removed else ""))
            print("the stall on Streak opens as an incident within a few seconds")
        else:
            print(f"removed {removed} demo projects" if removed else "no demo projects to remove")


if __name__ == "__main__":
    sys.exit(main())
