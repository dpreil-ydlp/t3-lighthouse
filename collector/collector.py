#!/usr/bin/env python3
"""Observe local agent metadata and deliver alerts: Mac notifications, and T3 notices, which start a turn of the parent agent."""
import argparse
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath

if __package__:
    from .t3_read import read_t3_database
    from .t3_mcp import deliver_pending as deliver_pending_t3_mcp
    from .t3_connect import T3ConnectError, renew_if_due as renew_t3_token
else:
    from t3_read import read_t3_database
    from t3_mcp import deliver_pending as deliver_pending_t3_mcp
    from t3_connect import T3ConnectError, renew_if_due as renew_t3_token

logger = logging.getLogger("lighthouse.collector")
DISCOVERY_INTERVAL = 600
SUBAGENT_DISCOVERY_INTERVAL = 120
TITLE_LIMIT = 120
T3_ID_PATTERN = re.compile(r"[A-Za-z0-9:._%-]{1,200}\Z")


def parse_record(record: dict, source: str, path: str, context: dict | None = None, record_offset: int | None = None):
    context = context if context is not None else {}
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    if source == "claude_code":
        cwd = payload.get("cwd") or record.get("cwd") or context.get("cwd")
        thread_id = payload.get("sessionId") or record.get("sessionId") or context.get("thread")
        event_type = record.get("type", "unknown")
        fallback_id = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:24]
        event_id = payload.get("uuid") or record.get("uuid") or f"{record.get('timestamp','')}:{event_type}:{record_offset if record_offset is not None else fallback_id}"
        observed = payload.get("timestamp") or record.get("timestamp")
    else:
        cwd = payload.get("cwd") or payload.get("working_directory") or context.get("cwd")
        thread_id = payload.get("session_id") or record.get("session_id") or (payload.get("id") if record.get("type") == "session_meta" else None) or context.get("thread")
        event_type = record.get("type", "unknown")
        fallback_id = record.get("ordinal") if record.get("ordinal") is not None else record_offset
        if fallback_id is None:
            fallback_id = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:24]
        event_id = payload.get("uuid") or record.get("id") or f"{fallback_id}:{event_type}"
        observed = record.get("timestamp") or payload.get("timestamp")
    if not cwd and not thread_id:
        return None
    if cwd:
        context["cwd"] = cwd
    if thread_id:
        context["thread"] = str(thread_id)
    try:
        parsed = datetime.fromisoformat(str(observed).replace("Z", "+00:00")) if observed else datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    file_id = hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16]
    return {"source": source, "confidence": "high", "event_type": "collector_observation", "source_event_id": f"{file_id}:{event_id}", "observed_at": parsed.astimezone(timezone.utc).isoformat(), "working_directory": cwd, "external_thread_id": str(thread_id) if thread_id else None, "summary": f"Observed {source} {event_type} record", "meaningful": False, "details": {"record_type": str(event_type)[:40]}}


def redact(event: dict) -> dict:
    allowed = {"project_id", "task_id", "parent_thread_id", "source", "confidence", "event_type", "source_event_id", "observed_at", "working_directory", "external_thread_id", "summary", "meaningful", "details"}
    return {key: value for key, value in event.items() if key in allowed}


def thread_title_record(record: dict, source: str, thread_id: str | None = None):
    """Return title metadata only. Never inspect or copy message content."""
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    if source == "claude_code":
        candidate = payload.get("summary") or payload.get("title") or record.get("summary") or record.get("title")
        thread_id = thread_id or payload.get("sessionId") or record.get("sessionId")
    else:
        candidate = record.get("thread_name") or record.get("title")
        thread_id = thread_id or record.get("id") or record.get("session_id")
    if not isinstance(candidate, str) or not candidate.strip() or not thread_id:
        return None
    candidate = candidate.strip()
    if candidate.casefold() in {"new thread", "untitled", "untitled session"}:
        return None
    return {"external_thread_id": str(thread_id), "source": source, "title": candidate[:TITLE_LIMIT], "title_origin": "native"}


def collect_t3_metadata(home: str | None = None):
    home = home or os.path.expanduser("~")
    database = os.path.join(home, ".t3", "userdata", "statev2.sqlite")
    roots, titles = [], []
    if not os.path.isfile(database):
        return roots, titles
    with read_t3_database(home) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "projection_projects" in tables:
            for title, root in conn.execute("SELECT title,workspace_root FROM projection_projects WHERE deleted_at IS NULL"):
                if root and not discovery_excluded(root, home):
                    roots.append({"path": root, "name": title.strip() or PurePosixPath(root).name, "origin": "t3"})
        if {"orchestration_v2_projection_threads", "orchestration_v2_projection_provider_threads"} <= tables:
            thread_columns = {row[1] for row in conn.execute("PRAGMA table_info(orchestration_v2_projection_threads)")}
            thread_payload = "t.payload_json" if "payload_json" in thread_columns else "'{}'"
            rows = conn.execute(f"""SELECT p.provider,p.payload_json,t.title,t.thread_id,{thread_payload}
                FROM orchestration_v2_projection_threads t
                JOIN orchestration_v2_projection_provider_threads p USING(thread_id)
                WHERE t.deleted_at IS NULL AND p.provider_session_id IS NOT NULL""")
            for provider, payload_json, title, t3_thread_id, thread_payload_json in rows:
                source = {"codex": "codex", "claudeAgent": "claude_code"}.get(provider)
                if source:
                    try:
                        payload = json.loads(payload_json or "{}")
                        thread_payload = json.loads(thread_payload_json or "{}")
                    except (TypeError, ValueError):
                        continue
                    native_ref = payload.get("nativeThreadRef")
                    native_id = native_ref.get("nativeId") if isinstance(native_ref, dict) else None
                    if not isinstance(native_id, str) or not native_id or not isinstance(t3_thread_id, str) or not T3_ID_PATTERN.fullmatch(t3_thread_id):
                        continue
                    item = thread_title_record({"title": title}, source, native_id)
                    lineage = thread_payload.get("lineage") if isinstance(thread_payload, dict) else None
                    relationship = lineage.get("relationshipToParent") if isinstance(lineage, dict) else None
                    titles.append({
                        "external_thread_id": str(native_id),
                        "source": source,
                        "title": item["title"] if item else None,
                        "title_origin": "t3",
                        "t3_thread_id": t3_thread_id,
                        "t3_subagent": relationship == "subagent" or t3_thread_id.startswith("thread:delegated-task:"),
                    })
    return roots, titles


def collect_t3_subagents(home: str | None = None):
    """Read delegated child state and activity from one SQLite read transaction."""
    home = home or os.path.expanduser("~")
    database = os.path.join(home, ".t3", "userdata", "statev2.sqlite")
    if not os.path.isfile(database):
        return None
    with read_t3_database(home) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        needed = {"orchestration_v2_projection_subagents", "orchestration_v2_projection_threads", "orchestration_v2_projection_turn_items", "orchestration_v2_projection_runtime_requests", "projection_threads", "projection_projects"}
        if not needed <= tables:
            return None
        query = """SELECT s.thread_id parent_t3_thread_id,s.child_thread_id,
            CASE WHEN s.status='running' AND
              (COALESCE(pt.pending_approval_count,0)>0 OR COALESCE(pt.pending_user_input_count,0)>0
              OR COALESCE((SELECT MAX(CASE WHEN request.status='pending' AND request.kind IN ('user_input','approval') THEN 1 ELSE 0 END)
                FROM orchestration_v2_projection_runtime_requests request WHERE request.thread_id=s.child_thread_id),0)>0)
              THEN 'waiting' ELSE s.status END status,
            s.updated_at state_updated_at,c.title,parent.title parent_title,project.workspace_root parent_project_root,
            CASE WHEN s.status='running' THEN
              MAX(COALESCE(s.updated_at,''),COALESCE(c.updated_at,''),COALESCE(pt.updated_at,''),
                COALESCE((SELECT MAX(output.updated_at) FROM orchestration_v2_projection_turn_items output
                  WHERE output.thread_id=s.child_thread_id
                    AND ((output.type IN ('command_execution','dynamic_tool') AND output.status IN ('completed','failed'))
                      OR (output.type='assistant_message' AND output.status='completed'))),''))
              ELSE MAX(COALESCE(s.updated_at,''),COALESCE(c.updated_at,''),COALESCE(pt.updated_at,'')) END activity_at,
            CASE WHEN s.status='running' THEN
              (SELECT MAX(output.updated_at) FROM orchestration_v2_projection_turn_items output
                WHERE output.thread_id=s.child_thread_id
                  AND ((output.type IN ('command_execution','dynamic_tool') AND output.status IN ('completed','failed'))
                    OR (output.type='assistant_message' AND output.status='completed')))
              ELSE NULL END last_child_output_at
          FROM orchestration_v2_projection_subagents s
          JOIN orchestration_v2_projection_threads c ON c.thread_id=s.child_thread_id
          JOIN orchestration_v2_projection_threads parent ON parent.thread_id=s.thread_id
          LEFT JOIN projection_projects project ON project.project_id=parent.project_id
          LEFT JOIN projection_threads pt ON pt.thread_id=s.child_thread_id
          WHERE s.child_thread_id IS NOT NULL
            AND (s.origin='app_owned' OR s.origin='provider_native')
          ORDER BY s.child_thread_id"""
        result = []
        for row in conn.execute(query):
            result.append({"parent_t3_thread_id": row["parent_t3_thread_id"], "child_t3_thread_id": row["child_thread_id"],
                           "status": row["status"], "title": row["title"] or "Untitled subagent",
                           "parent_title": row["parent_title"] or "T3 parent chat",
                           "parent_project_root": row["parent_project_root"],
                           "last_activity_at": row["activity_at"] or row["state_updated_at"],
                           "last_child_output_at": row["last_child_output_at"]})
        return result


def discovery_excluded(path: str, home: str | None = None):
    normalized = os.path.normpath(path).casefold().rstrip(os.sep)
    home = home or os.path.expanduser("~")
    scratch = os.path.normpath(os.path.join(home, ".t3", "scratch")).casefold()
    if normalized == scratch or normalized.startswith(scratch + os.sep):
        return True
    if any(normalized == prefix or normalized.startswith(prefix + os.sep) for prefix in ("/private/var/folders", "/tmp", "/private/tmp")):
        return True
    documents_codex = os.path.normpath(os.path.join(home, "Documents", "Codex")).casefold()
    if normalized.startswith(documents_codex + os.sep):
        remainder = normalized[len(documents_codex) + 1:].split(os.sep)
        if remainder and len(remainder[0]) == 10 and remainder[0][4:5] == "-" and remainder[0][7:8] == "-":
            return True
    return False


def read_t3_environment_id(home: str | None = None) -> str | None:
    path = Path(home or os.path.expanduser("~")) / ".t3" / "userdata" / "environment-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if T3_ID_PATTERN.fullmatch(value) else None


def discover_roots(state: dict, home: str | None = None):
    home = home or os.path.expanduser("~")
    roots, titles = collect_t3_metadata(home)
    active_dirs = set()
    for item in state.get("offsets", {}).values():
        cwd = item.get("cwd")
        if cwd:
            active_dirs.add(os.path.normpath(cwd))
    for pattern, source in ((os.path.join(home, ".claude/projects/**/*.jsonl"), "claude_code"), (os.path.join(home, ".codex/sessions/**/*.jsonl"), "codex")):
        for path in glob.glob(pattern, recursive=True, include_hidden=True):
            try:
                context = session_context(path, source)
            except OSError:
                continue
            if context.get("cwd"):
                active_dirs.add(os.path.normpath(context["cwd"]))
            if source == "claude_code":
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as file:
                        for _, line in zip(range(200), file):
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            title = thread_title_record(record, source, context.get("thread"))
                            if title:
                                titles.append(title)
                    size = os.path.getsize(path)
                    with open(path, "r", encoding="utf-8", errors="replace") as file:
                        file.seek(max(0, size - 1048576))
                        if file.tell():
                            file.readline()
                        for line in file:
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            title = thread_title_record(record, source, context.get("thread"))
                            if title:
                                titles.append(title)
                except OSError:
                    pass
    for base in (os.path.join(home, "Projects"), os.path.join(home, "projects")):
        if not os.path.isdir(base):
            continue
        for child in Path(base).iterdir():
            if not child.is_dir() or discovery_excluded(str(child), home):
                continue
            project_path = os.path.normpath(str(child)).casefold()
            prefix = project_path + os.sep
            t3_worktrees = os.path.normpath(os.path.join(home, ".t3", "worktrees")).casefold() + os.sep
            codex_worktrees = os.path.normpath(os.path.join(home, ".codex", "worktrees")).casefold() + os.sep
            active = False
            for directory in active_dirs:
                normalized = os.path.normpath(directory).casefold()
                if normalized == project_path or normalized.startswith(prefix):
                    active = True
                    break
                if normalized.startswith(t3_worktrees):
                    worktree_parts = normalized[len(t3_worktrees):].split(os.sep)
                    if worktree_parts and worktree_parts[0] == child.name.casefold():
                        active = True
                        break
                if normalized.startswith(codex_worktrees):
                    worktree_parts = normalized[len(codex_worktrees):].split(os.sep)
                    if len(worktree_parts) >= 2 and worktree_parts[1] == child.name.casefold():
                        active = True
                        break
            if active:
                roots.append({"path": str(child), "name": child.name, "origin": "folder"})
    index_path = os.path.join(home, ".codex", "session_index.jsonl")
    try:
        with open(index_path, "r", encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                item = thread_title_record(record, "codex")
                if item:
                    titles.append(item)
    except OSError:
        pass
    dedup_roots = {}
    for item in roots:
        key = os.path.normpath(item["path"]).casefold()
        dedup_roots.setdefault(key, item)
    dedup_titles = {}
    for item in titles:
        key = (item["source"], item["external_thread_id"])
        current = dedup_titles.get(key)
        if current is None:
            dedup_titles[key] = item
            continue
        # Choose display titles independently from T3 identity and lineage.
        # A useful T3 title wins; otherwise retain or use a useful native title.
        incoming_title = item.get("title")
        current_is_t3 = bool(current.get("title")) and current.get("title_origin") == "t3"
        incoming_is_t3 = item.get("title_origin") == "t3"
        if incoming_title and not (current_is_t3 and not incoming_is_t3):
            current["title"] = incoming_title
            current["title_origin"] = item.get("title_origin")
        # T3 identity fields remain attached even when the native title wins.
        if incoming_is_t3:
            current.update({field: item[field] for field in ("t3_thread_id", "t3_subagent") if field in item})
    return list(dedup_roots.values()), list(dedup_titles.values())


def send_discovery(host: str, token: str, roots: list[dict], titles: list[dict], environment_id: str | None = None):
    base = host.rstrip("/") + "/api/v1/discovery/roots"
    request_json(base, token, {"roots": roots, "titles": [], "environment_id": environment_id})
    for start in range(0, len(titles), 500):
        request_json(base, token, {"roots": [], "titles": titles[start:start + 500]})


def request_json(url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if body is not None:
        headers["Idempotency-Key"] = hashlib.sha256(data).hexdigest()
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def save_state(path: Path, state: dict):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, separators=(",", ":")))
    os.chmod(temp, 0o600)
    temp.replace(path)


def session_context(path: str, source: str) -> dict:
    context = {}
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        for _ in range(200):
            offset = file.tell()
            line = file.readline()
            if not line:
                break
            try:
                parse_record(json.loads(line), source, path, context, offset)
            except (ValueError, TypeError):
                continue
            if context.get("cwd") and context.get("thread"):
                break
    return context


def is_briefs_work_session(context: dict, home: str | None = None) -> bool:
    home = home or os.path.expanduser("~")
    cwd = context.get("cwd")
    if not cwd:
        return False
    work = os.path.normcase(os.path.realpath(os.path.join(home, ".config", "lighthouse", "briefs-work")))
    candidate = os.path.normcase(os.path.realpath(cwd))
    return candidate == work or candidate.startswith(work + os.sep)


def initial_event(path: str, source: str):
    context = session_context(path, source)
    latest = None
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        size = os.fstat(file.fileno()).st_size
        file.seek(max(0, size - 262144))
        if file.tell():
            file.readline()
        while True:
            offset = file.tell()
            line = file.readline()
            if not line:
                break
            try:
                event = parse_record(json.loads(line), source, path, context, offset)
            except (ValueError, TypeError):
                continue
            if event:
                latest = event
    return latest, context


def send_event(host: str, token: str, event: dict):
    event = redact(event)
    body = json.dumps(event).encode()
    request = urllib.request.Request(host.rstrip("/") + "/api/v1/events", data=body, method="POST", headers={"Authorization": f"Bearer {token}", "Idempotency-Key": hashlib.sha256(body).hexdigest(), "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15):
        pass


def scan_jsonl(host: str, token: str, state: dict, progress=None):
    patterns = ((os.path.expanduser("~/.claude/projects/**/*.jsonl"), "claude_code"), (os.path.expanduser("~/.codex/sessions/**/*.jsonl"), "codex"))
    offsets = state.setdefault("offsets", {})
    scanned = 0
    for pattern, source in patterns:
        for path in glob.glob(pattern, recursive=True, include_hidden=True):
            scanned += 1
            if progress and scanned % 25 == 0:
                progress()
            try:
                size = os.path.getsize(path)
                prior = offsets.get(path)
                context = session_context(path, source) if prior is None else {key: prior.get(key) for key in ("cwd", "thread") if prior.get(key)}
                if is_briefs_work_session(context):
                    continue
                if prior is None:
                    event, context = initial_event(path, source)
                    if event:
                        send_event(host, token, event)
                    offsets[path] = {**context, "offset": size}
                    continue
                cursor = prior.get("offset", 0)
                if size < cursor:
                    cursor = 0
                with open(path, "r", encoding="utf-8", errors="replace") as file:
                    file.seek(cursor)
                    while True:
                        offset = file.tell()
                        line = file.readline()
                        if not line:
                            break
                        try:
                            event = parse_record(json.loads(line), source, path, context, offset)
                        except (ValueError, TypeError):
                            continue
                        if event:
                            send_event(host, token, event)
                    prior.update(context)
                    prior["offset"] = file.tell()
                offsets[path] = prior
            except urllib.error.HTTPError as error:
                if error.code == 409:
                    logger.error("collector event rejected status=409 source=%s", source)
                continue
            except (OSError, ValueError, urllib.error.URLError):
                continue


def scan_liveness(host: str, token: str, state: dict, progress=None):
    roots = ((os.path.expanduser("~/Library/Application Support/com.openai.chat"), "chatgpt_desktop"), (os.path.expanduser("~/Library/Application Support/com.openai.codex"), "codex_desktop"), (os.path.expanduser("~/Library/Application Support/Claude"), "claude_desktop"))
    seen = state.setdefault("mtimes", {})
    scanned = 0
    for root, source in roots:
        if not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root):
            depth = os.path.relpath(current, root).count(os.sep)
            dirs[:] = [] if depth >= 5 else [name for name in dirs if name not in {"Cache", "GPUCache", "Code Cache", "Service Worker"}]
            for name in files:
                scanned += 1
                if progress and scanned % 100 == 0:
                    progress()
                if source == "claude_desktop" and not name.endswith(".jsonl"):
                    continue
                path = os.path.join(current, name)
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                key = source + ":" + hashlib.sha256(os.path.relpath(path, root).encode()).hexdigest()
                if key not in seen:
                    seen[key] = mtime
                    continue
                if mtime <= seen[key]:
                    continue
                seen[key] = mtime
                event = {"source": source, "confidence": "low", "event_type": "desktop_file_activity", "source_event_id": f"{key}:{mtime}", "observed_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(), "external_thread_id": key, "summary": f"Observed low-confidence {source} file activity", "meaningful": False, "details": {"signal": "file_mtime_only"}}
                try:
                    send_event(host, token, event)
                except urllib.error.HTTPError as error:
                    if error.code == 409:
                        logger.error("collector liveness event rejected status=409 source=%s", source)
                    seen.pop(key, None)
                except (OSError, urllib.error.URLError):
                    seen.pop(key, None)


def escape_applescript(value: str):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def show_notification(item: dict):
    payload = item.get("payload") or {}
    title = "Lighthouse: incident escalated" if item.get("incident_state") == "escalated" else "Lighthouse: task stalled"
    message = str(payload.get("message") or payload.get("reason") or "A supervised task needs attention")[:240]
    incident = str(item.get("incident_id", ""))
    notifier = shutil.which("terminal-notifier")
    if notifier:
        subprocess.run([notifier, "-title", title, "-message", message, "-group", incident], check=True, timeout=15)
        return "terminal-notifier"
    osa = shutil.which("osascript")
    if not osa:
        raise RuntimeError("osascript is not available")
    script = f'display notification "{escape_applescript(message)}" with title "{escape_applescript(title)}"'
    subprocess.run([osa, "-e", script], check=True, timeout=15)
    return "osascript"


def show_t3_notice(message: str, outbox_id: str):
    """Show a stall notice that T3 would not take as a Mac notification.

    Each notice gets its own group, so terminal-notifier does not replace one stall's notice with another's.
    """
    return show_notification({"incident_id": f"lighthouse-t3-{outbox_id}", "payload": {"message": message}})


def deliver_mac_alerts(host: str, token: str, state: dict):
    try:
        data = request_json(host.rstrip("/") + "/api/v1/mac-alerts/pending", token)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return
    done = state.setdefault("alert_notified", {})
    for item in data.get("items", [])[:1]:
        item_id = item["id"]
        try:
            if item_id not in done:
                method = show_notification(item)
                done[item_id] = {"method": method, "incident_id": item["incident_id"]}
                logger.info("mac_alert shown incident=%s via=%s", item['incident_id'], method)
            request_json(host.rstrip("/") + f"/api/v1/mac-alerts/{item_id}/delivered", token, {"incident_id": item["incident_id"]})
            logger.info("mac_alert delivered incident=%s", item['incident_id'])
            done.pop(item_id, None)
        except (OSError, subprocess.SubprocessError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
            logger.warning("mac_alert delivery pending incident=%s error=%s", item['incident_id'], type(error).__name__)


def run_cycle(host: str, token: str, state: dict, progress=None, last_discovery=0.0):
    if os.getenv("LIGHTHOUSE_DERIVE_SUBAGENT_TASKS", "1").strip().lower() not in {"0", "false", "no", "off"} and time.monotonic() - state.get("last_subagent_discovery", 0.0) >= SUBAGENT_DISCOVERY_INTERVAL:
        discovery_started = time.monotonic()
        stage = "read"
        try:
            subagents = collect_t3_subagents()
            read_seconds = time.monotonic() - discovery_started
            stage = "upload"
            upload_started = time.monotonic()
            send_subagents(host, token, subagents)
            if subagents is not None:
                logger.info("subagent discovery upload succeeded children=%d read_seconds=%.3f upload_seconds=%.3f",
                            len(subagents), read_seconds, time.monotonic() - upload_started)
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, sqlite3.Error) as error:
            logger.warning("subagent discovery upload failed stage=%s elapsed_seconds=%.3f error=%s message=%s",
                           stage, time.monotonic() - discovery_started, type(error).__name__, str(error))
        finally:
            state["last_subagent_discovery"] = time.monotonic()
    if time.monotonic() - last_discovery >= DISCOVERY_INTERVAL:
        try:
            roots, titles = discover_roots(state)
            send_discovery(host, token, roots, titles, read_t3_environment_id())
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as error:
            logger.warning("project discovery upload failed error=%s", type(error).__name__)
        except sqlite3.Error as error:
            logger.warning("project discovery metadata read failed error=%s", type(error).__name__)
        finally:
            last_discovery = time.monotonic()
    scan_jsonl(host, token, state, progress)
    scan_liveness(host, token, state, progress)
    return last_discovery


def send_subagents(host: str, token: str, subagents: list[dict]):
    if subagents is not None:
        request_json(host.rstrip("/") + "/api/v1/discovery/subagents", token, {"subagents": subagents})


def renew_t3_connection(state: dict):
    try:
        expires_at = renew_t3_token(state)
    except (T3ConnectError, OSError, subprocess.SubprocessError) as error:
        logger.warning("T3 token renewal failed error=%s", error)
        return
    if expires_at:
        logger.info("T3 token renewed expires_at=%s", datetime.fromtimestamp(expires_at, timezone.utc).isoformat())


def run(host: str, token_file: str, interval: float):
    if os.stat(token_file).st_mode & 0o777 != 0o600:
        raise PermissionError("collector token file must have mode 0600")
    token = Path(token_file).read_text().strip()
    state_path = Path(os.path.expanduser("~/.config/lighthouse/collector-state.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {}
    # Older versions wrote collector-state-cr1.json; read it so cursors carry over.
    for candidate in (state_path, state_path.with_name("collector-state-cr1.json")):
        try:
            state = json.loads(candidate.read_text())
            break
        except (OSError, ValueError):
            continue
    last_discovery = 0.0
    while True:
        deliver_mac_alerts(host, token, state)
        def checkpoint():
            deliver_mac_alerts(host, token, state)
            save_state(state_path, state)

        renew_t3_connection(state)
        deliver_pending_t3_mcp(host, token, state, checkpoint, show_t3_notice)

        last_discovery = run_cycle(host, token, state, checkpoint, last_discovery)
        checkpoint()
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(args.host, args.token_file, args.interval)


if __name__ == "__main__":
    main()
