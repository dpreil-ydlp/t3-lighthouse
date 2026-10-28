#!/usr/bin/env python3
"""Incrementally create owner-readable project briefs without changing source projects."""
import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lighthouse.project_discovery import match_project_root

HOST = os.environ.get("LIGHTHOUSE_API_URL", "http://127.0.0.1:51821")
CONFIG_DIR = Path(os.environ.get("LIGHTHOUSE_CONFIG_DIR", "~/.config/lighthouse")).expanduser()
TOKEN_FILE = Path(os.environ.get("LIGHTHOUSE_AGENT_TOKEN_FILE", CONFIG_DIR / "secrets/agent-token")).expanduser()
STATE_FILE = CONFIG_DIR / "briefs-state.json"
CONTEXT_DIR = CONFIG_DIR / "context"
WORK_DIR = CONFIG_DIR / "briefs-work"
CLAUDE = os.path.expanduser(os.environ.get("LIGHTHOUSE_CLAUDE_BIN", "~/.local/bin/claude"))
MODEL = "claude-haiku-5-5"
MAX_INPUT_CHARS = 40_000
MAX_CALLS = 12
REWRITE_MAX_CALLS = 60
STE_MAX_RETRIES = 2
CALL_TIMEOUT = 180
INITIAL_TAIL_BYTES = 200_000
VAGUE_NOW = re.compile(r"\b(?:remaining(?:\s+product)?\s+work|finish\s+the\s+work|continue\s+work|make\s+progress)\b", re.I)
DISALLOWED_ACTOR = re.compile(r"\bthe\s+(?:owner|team)\b", re.I)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.I)
SECRET_PATTERNS = [
    PRIVATE_KEY_PATTERN,
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"),
    re.compile(r"(?i)([?&](?:token|access_token|api_key|key|secret|signature)=)[^&#\s]+"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\blh_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"),
]

SECRET_FIELD_PREFIX = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?P<prefix>(?:\\?[\"'])?"
    r"(?:token|access_token|refresh_token|api_key|apikey|secret|client_secret|password|passwd|authorization|cookie|session)"
    r"(?:\\?[\"'])?\s*[:=]\s*)"
)

BRIEF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["about", "now", "done", "left", "eta_basis"],
    "properties": {
        "about": {"type": "string", "minLength": 1, "maxLength": 600},
        "now": {"type": "string", "minLength": 1, "maxLength": 240},
        "done": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        "left": {"type": "array", "maxItems": 32, "items": {"oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["text", "who"],
             "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 200},
                            "who": {"type": "string", "enum": ["you"]}}},
            {"type": "object", "additionalProperties": False, "required": ["text", "who"],
             "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 200},
                            "who": {"type": "string", "enum": ["outside"]}}},
            {"type": "object", "additionalProperties": False, "required": ["text", "who", "hours"],
             "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 200},
                            "who": {"type": "string", "enum": ["agents"]},
                            "hours": {"type": ["number", "null"], "minimum": 0}}},
        ]}},
        "eta_basis": {"type": "string", "minLength": 1, "maxLength": 300},
    },
}


def redact_text(value: str) -> str:
    cleaned = PRIVATE_KEY_PATTERN.sub("[REDACTED]", value)
    parts = []
    cursor = 0
    for match in SECRET_FIELD_PREFIX.finditer(cleaned):
        if match.start() < cursor:
            continue
        boundaries = [cleaned.find(boundary, match.end()) for boundary in ("\n", "\r", r"\n", r"\r")]
        end = min((boundary for boundary in boundaries if boundary >= 0), default=len(cleaned))
        parts.extend((cleaned[cursor:match.end()], "[REDACTED]"))
        cursor = end
    parts.append(cleaned[cursor:])
    cleaned = "".join(parts)
    for pattern in SECRET_PATTERNS[1:]:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def clean_now(value: str) -> str:
    month = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    date = rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|{month}\s+\d{{1,2}}(?:,?\s+\d{{4}})?|\d{{1,2}}\s+{month}(?:\s+\d{{4}})?|today|yesterday|this morning)"
    match = re.match(rf"(?i)^As of\s+{date},\s*", value)
    if not match:
        return value
    cleaned = value[match.end():]
    if cleaned.startswith("iOS"):
        return cleaned
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


def is_briefs_work_session(context: dict, home: Path | None = None) -> bool:
    home = home or Path.home()
    cwd = context.get("cwd")
    if not cwd:
        return False
    work = os.path.normcase(os.path.realpath(home / ".config/lighthouse/briefs-work"))
    candidate = os.path.normcase(os.path.realpath(cwd))
    return candidate == work or candidate.startswith(work + os.sep)


def _content_text(content) -> list[str]:
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "")).lower()
        if kind in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
            if block["text"].strip():
                texts.append(block["text"])
    return texts


def extract_text(record: dict, source: str) -> list[str]:
    if source == "claude_code":
        if record.get("type") not in {"user", "assistant"}:
            return []
        message = record.get("message")
        if isinstance(message, dict):
            return _content_text(message.get("content"))
        return _content_text(record.get("content"))

    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if record.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message", "assistant_message"}:
        return _content_text(payload.get("message"))
    if record.get("type") == "response_item":
        item = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        if item.get("type") == "message" and item.get("role") in {"user", "assistant"}:
            return _content_text(item.get("content"))
    return []


def _record_context(record: dict, source: str, context: dict) -> None:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    cwd = (payload.get("cwd") or payload.get("working_directory") or record.get("cwd"))
    if cwd:
        context["cwd"] = cwd
    thread = payload.get("session_id") or record.get("sessionId") or record.get("session_id")
    if thread:
        context["thread"] = str(thread)


def _record_time(record: dict) -> str:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    stamp = payload.get("timestamp") or record.get("timestamp")
    if not stamp:
        return ""
    return str(stamp)


def read_session(path: Path, source: str, cursor: int | None, context: dict) -> tuple[list[tuple[str, str]], int]:
    size = path.stat().st_size
    start = 0 if cursor is None else cursor
    if size < start:
        start = 0
    if cursor is None:
        start = max(0, size - INITIAL_TAIL_BYTES)
    lines: list[tuple[str, str]] = []
    with path.open("rb") as stream:
        stream.seek(start)
        if cursor is None and start:
            stream.readline()
        while True:
            line_start = stream.tell()
            raw = stream.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                stream.seek(line_start)
                break
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            _record_context(record, source, context)
            timestamp = _record_time(record)
            for text in extract_text(record, source):
                safe = redact_text(text).strip()
                if safe and safe != "[REDACTED]":
                    lines.append((timestamp, safe))
        committed_cursor = stream.tell()
    return lines, committed_cursor


def api_request(path: str, token: str, payload: dict | None = None, method: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        HOST.rstrip("/") + path, data=data, method=method or ("PUT" if data is not None else "GET"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {"files": {}, "projects": {}}
    except (OSError, ValueError):
        return {"files": {}, "projects": {}}


def save_state(path: Path, state: dict) -> None:
    ensure_config_write_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(state, output, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temp, 0o600)
    temp.replace(path)


def project_slug(project: dict) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", str(project.get("name", "project")).lower()).strip("-") or "project"
    return name[:80]


_HEAD_NOT_CAPTURED = object()


def git_activity(root: Path, previous: dict | None, covered_commit: str | None = None, head_commit: str | None | object = _HEAD_NOT_CAPTURED) -> list[str]:
    if not (root / ".git").exists():
        return []
    if head_commit is _HEAD_NOT_CAPTURED:
        head_commit = git_head(root)
    if not head_commit:
        return []
    if previous is None:
        args = ["git", "log", "--format=%cI %s", "-40", head_commit]
    elif covered_commit:
        args = ["git", "log", "--format=%cI %s", "-40", f"{covered_commit}..{head_commit}"]
    else:
        since = previous.get("activity_through") or previous.get("updated_at")
        args = ["git", "log", "--format=%cI %s", "-40", head_commit]
        if since:
            args.append("--since=" + str(since))
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=20)
    commits = [redact_text(line.strip()) for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []
    return commits


def git_head(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=20)
    return result.stdout.strip() if result.returncode == 0 else None


def first_build_context(root: Path) -> list[str]:
    chunks = []
    for pattern in ("README*", "CLAUDE.md", "AGENTS.md", "memory.md"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                try:
                    body = redact_text(path.read_text(encoding="utf-8", errors="replace")[:8000]).strip()
                except OSError:
                    continue
                if body:
                    chunks.append(f"{path.name}:\n{body}")
    return chunks


def _previous_brief(brief: dict | None) -> dict | None:
    if not brief:
        return None
    clean = {}
    for key, value in brief.items():
        if key not in {"about", "now", "done", "left", "eta_basis", "activity_through", "updated_at"}:
            continue
        if isinstance(value, str):
            clean[key] = redact_text(value)
        elif isinstance(value, list):
            clean[key] = [({**item, "text": redact_text(item["text"])} if isinstance(item, dict) and isinstance(item.get("text"), str)
                           else redact_text(item) if isinstance(item, str) else item) for item in value]
        else:
            clean[key] = value
    return clean


def _limit_new_text(items: list[tuple[str, str]], limit: int = MAX_INPUT_CHARS) -> str:
    selected = []
    remaining = limit
    for timestamp, text in sorted(items, key=lambda item: item[0], reverse=True):
        piece = (f"[{timestamp}] " if timestamp else "") + text
        if len(piece) > remaining:
            piece = piece[:remaining]
        if piece:
            selected.append(piece)
            remaining -= len(piece) + 1
        if remaining <= 0:
            break
    return "\n".join(selected)


def prioritize_left_items(items: list[dict], limit: int = 8) -> list[dict]:
    owners = [item for item in items if item.get("who") == "you"]
    others = [item for item in items if item.get("who") != "you"]
    return owners + others[:max(0, limit - len(owners))]


def make_prompt(project: dict, previous: dict | None, docs: list[str], commits: list[str], new_text: str) -> str:
    packet = {
        "project_name": redact_text(str(project.get("name", "Project"))),
        "previous_brief": previous,
        "first_build_documents": docs,
        "new_git_commits_newest_first": commits,
        "new_user_and_assistant_text_newest_first": new_text,
    }
    return (
        "Write an accurate brief for the owner. Follow these writing rules in every text field.\n"
        "Keep about, done, and eta_basis to sentences with 20 words or fewer. Give each sentence one idea. Use active voice and name the actor.\n"
        "Address the owner as 'you'. Call AI workers 'the agents'. Keep the app or product as actor when it acts.\n"
        "Never write 'the owner' or 'the team'. Avoid generic phrases such as 'remaining work' or 'make progress'.\n"
        "Use simple present or will. Use plain, common words. Use one term for each meaning.\n"
        "Do not use jargon, paths, commit hashes, model names, or thread or session details.\n"
        "now: 1–2 full, natural sentences (≤240 characters) a busy owner reads on a dashboard card. Say what is happening right now, what just finished, and what it is waiting on or blocked by. Be specific: name the feature, decision, number or blocker. Plain words, no jargon, no file paths, no dates.\n"
        "Style examples: 'The recipe search is live and tested, but your hosting login and two photo checks are still needed before it is finished.' / 'The shopping list export is updated and checked: a typical week lists 42 items, and a few unmatched ingredients are still marked unknown.' / 'The agents rebuilt the welcome tour and are waiting for your choice of color theme before release.'\n"
        "Do not use hype or filler. Do not start now with an 'As of' prefix.\n"
        "Never use successfully, robust, seamless, leverage, comprehensive, various, or em-dash asides.\n"
        "Write each done item as one short sentence. Write each left item with text and who.\n"
        "Use who=you for owner actions, agents for agent work, and outside for external work.\n"
        "Write each owner action as a direct command that starts with its action verb. Do not start with 'You', 'You must', or a person as the subject. Use ordinary English verbs, including decide, mark, pay, or read.\n"
        "Keep agent items starting with 'The agents'.\n"
        "Agent items must include hours as a number or null. Owner and outside items must omit hours.\n"
        "Split sentences that mix owners, agents, or outside people into separate items.\n"
        "Write about in one or two sentences. Explain the agent estimate basis in one sentence.\n"
        "Keep prior done and left items unless new evidence changes them. Put recent done items first.\n"
        "List no more than eight done items. Keep every owner action if left items exceed eight.\n"
        "Weak: 'Tests were run.' Clear: 'The agent ran the tests.'\n"
        "Weak: 'A robust flow is ready.' Clear: 'The app saves the recipe.'\n"
        "Weak: 'The team finishes the remaining work.' Clear owner action: 'Choose the color theme for the recipe pages.'\n"
        "Clear: 'The agents fix the broken photo upload on the recipe form.'\n"
        "Return only the required JSON object.\n\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    )


def run_model(prompt: str) -> dict:
    WORK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(WORK_DIR, 0o700)
    command = [CLAUDE, "-p", "--model", MODEL, "--effort", "medium", "--output-format", "json",
               "--json-schema", json.dumps(BRIEF_SCHEMA, separators=(",", ":")), "--no-session-persistence",
               "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "project,local", "--disable-slash-commands"]
    allowed = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["PATH"] = f"{Path(CLAUDE).parent}:/usr/bin:/bin"
    environment["TERM"] = "dumb"
    result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=CALL_TIMEOUT, cwd=WORK_DIR, env=environment)
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed status={result.returncode}")
    try:
        response = json.loads(result.stdout)
        if isinstance(response.get("structured_output"), dict):
            brief = response["structured_output"]
        else:
            brief = json.loads(response["result"]) if isinstance(response.get("result"), str) else response.get("result", response)
    except (TypeError, KeyError, ValueError) as error:
        raise RuntimeError("claude -p returned invalid JSON") from error
    return brief


def _sentences(text: str) -> list[str]:
    """Split brief text without treating common abbreviations or decimals as sentence ends."""
    protected = re.sub(
        r"\b(?:e\.g|i\.e|Mr|Mrs|Ms|Dr|Prof|vs|etc)\.",
        lambda match: match.group(0)[:-1] + "\ue000",
        text,
        flags=re.I,
    )
    protected = re.sub(r"\b(?:[A-Z]\.){2,}", lambda match: match.group(0).replace(".", "\ue000"), protected)
    protected = re.sub(r"(?<=\d)\.(?=\d)", "\ue000", protected)
    protected = re.sub(r"[\r\n]+", " ", protected)
    parts = re.split(r"(?<=[.!?])(?:[\"'’”)]*)\s+", protected)
    return [part.replace("\ue000", ".").strip() for part in parts if part.strip()]


def _word_count(sentence: str) -> int:
    return len(re.findall(r"(?:[$€£¥₪])?\d[\d,]*(?:\.\d+)?|[^\W\d_]+(?:['’\-][^\W\d_]+)*|\d+", sentence, flags=re.UNICODE))


def ste_check(brief: dict) -> list[str]:
    issues = []
    for field in ("about", "done", "left", "eta_basis"):
        value = brief.get(field, [])
        texts = value if isinstance(value, list) else [value]
        for text in texts:
            if isinstance(text, dict):
                text = text.get("text", "")
            if isinstance(text, str):
                for sentence in _sentences(text):
                    if (_word_count(sentence) > 20 or DISALLOWED_ACTOR.search(sentence)
                            or VAGUE_NOW.search(sentence)):
                        issues.append(sentence)
    now = brief.get("now")
    if isinstance(now, str):
        if len(now) > 240:
            issues.append("now exceeds 240 characters")
        for sentence in _sentences(now):
            if DISALLOWED_ACTOR.search(sentence) or VAGUE_NOW.search(sentence):
                issues.append(sentence)
    return issues


def validate_brief(brief: dict) -> None:
    if not isinstance(brief, dict) or set(brief) != set(BRIEF_SCHEMA["required"]):
        raise ValueError("brief fields do not match schema")
    for key, maximum in (("about", 600), ("now", 240), ("eta_basis", 300)):
        value = brief[key]
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"invalid brief field: {key}")
    for key in ("done", "left"):
        items = brief[key]
        if not isinstance(items, list):
            raise ValueError(f"invalid brief field: {key}")
        if key == "done" and (len(items) > 8 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in items)):
            raise ValueError(f"invalid brief field: {key}")
    left = brief["left"]
    if len(left) > 32:
        raise ValueError("invalid brief field: left")
    for item in left:
        if not isinstance(item, dict) or not {"text", "who"} <= item.keys() or set(item) - {"text", "who", "hours"}:
            raise ValueError("invalid left item")
        text, who = item["text"], item["who"]
        if not isinstance(text, str) or not text.strip() or len(text) > 200 or who not in {"you", "agents", "outside"}:
            raise ValueError("invalid left item")
        first_word = re.match(r"([A-Za-z'-]+)", text)
        if who == "you" and (not first_word or re.match(r"(?i)^(?:you\b|the owner\b|owner\b)", text.strip())):
            raise ValueError("owner_item_imperative")
        if who == "agents":
            hours = item.get("hours")
            if "hours" not in item or (hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 0)):
                raise ValueError("invalid agent item hours")
        elif "hours" in item:
            raise ValueError("non_agent_hours")
    complete = re.compile(r"\b(?:the (?:whole )?project|everything|all work|it)\s+(?:is|was|has been)\s+(?:(?:now|fully|completely|already)\s+)*(?:finished|complete|completed|done)\b|\ball\s+(?:(?:now|fully|completely|already)\s+)*done\b", re.I)
    if left and (complete.search(brief["now"]) or any(complete.search(item) for item in brief["done"])):
        raise ValueError("finished_claim_open_items")


def markdown(brief: dict, updated: dt.datetime) -> str:
    done = "\n".join(f"- {item}" for item in brief["done"]) or "- Nothing recorded yet."
    left = "\n".join(f"- {item['text']} ({item['who']})" for item in brief["left"]) or "- Nothing recorded yet."
    return (f"# Project brief\n\nUpdated: {updated.date().isoformat()}\n\n## About\n{brief['about']}\n\n## Now\n{brief['now']}\n\n"
            f"## Done\n{done}\n\n## Left\n{left}\n\nAgent estimate basis: {brief['eta_basis']}\n")


def write_context(path: Path, content: str) -> None:
    ensure_config_write_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temp, 0o600)
    temp.replace(path)


def ensure_config_write_path(path: Path) -> None:
    home = Path.home().absolute()
    allowed = CONFIG_DIR.absolute()
    candidate = path.absolute()
    if not candidate.is_relative_to(allowed):
        raise PermissionError("brief job writes are limited to the Lighthouse config directory")
    # Reject symlinks in every directory from home (or the config directory's parent) down to the file.
    base = home if allowed.is_relative_to(home) and allowed != home else allowed.parent
    current = base
    for part in candidate.relative_to(base).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise PermissionError("brief storage path must not follow symlinks")


def run_once(project_filter: list[str] | None = None, dry_run: bool = False, rewrite_all: bool = False) -> dict:
    if not TOKEN_FILE.is_file():
        raise FileNotFoundError("Lighthouse collector token file is missing")
    if TOKEN_FILE.stat().st_mode & 0o777 != 0o600:
        raise PermissionError("Lighthouse collector token file must have mode 0600")
    token = TOKEN_FILE.read_text().strip()
    projects = api_request("/api/v1/projects?view=active", token).get("items", [])
    state = load_state(STATE_FILE)
    files_state = state.setdefault("files", {})
    project_state = state.setdefault("projects", {})
    if project_filter:
        filters = {value.lower() for value in project_filter}
        projects = [p for p in projects if project_slug(p) in filters or str(p.get("id", "")).lower() in filters]

    patterns = ((str(Path.home() / ".claude/projects/**/*.jsonl"), "claude_code"),
                (str(Path.home() / ".codex/sessions/**/*.jsonl"), "codex"))
    extracted: dict[str, list[tuple[str, str]]] = {str(p["id"]): [] for p in projects}
    pending_offsets: dict[str, tuple[int, str, dict]] = {}
    for pattern, source in patterns:
        for raw_path in glob.glob(pattern, recursive=True, include_hidden=True):
            path = Path(raw_path)
            try:
                cursor_info = files_state.get(str(path))
                cursor = cursor_info.get("offset") if cursor_info else None
                context = dict(cursor_info.get("context", {})) if cursor_info else {}
                # Discover cwd from leading session metadata, without retaining message bodies.
                if not context.get("cwd"):
                    from collector.collector import session_context
                    context.update(session_context(str(path), source))
                if context.get("cwd") and is_briefs_work_session(context):
                    continue
                matched = match_project_root(context.get("cwd"), [root for p in projects for root in
                    ({"path": root, "project_id": str(p["id"]), "project_name": p.get("name")} for root in p.get("roots", []))])
                if not matched:
                    continue
                project_id = matched.get("project_id")
                if project_id not in extracted:
                    continue
                lines, next_cursor = read_session(path, source, cursor, context)
                if lines:
                    extracted[project_id].extend(lines)
                pending_offsets[str(path)] = (next_cursor, project_id, context)
            except (OSError, ValueError, TypeError):
                continue

    prepared = []
    for project in projects:
        pid = str(project["id"])
        brief_prev = project.get("brief")
        # The projects list carries a compact brief. Fetch detail only when needed for prior content.
        detail = None
        if brief_prev:
            detail = api_request(f"/api/v1/projects/{pid}", token).get("project", {})
            brief_prev = detail.get("brief")
        previous = _previous_brief(brief_prev)
        roots = project.get("roots") or []
        root_path = next((Path(item) for item in roots if Path(item).exists()), None)
        if root_path is None:
            continue
        pstate = project_state.setdefault(pid, {})
        head_commit = git_head(root_path)
        commits = git_activity(root_path, previous, pstate.get("activity_commit"), head_commit)
        new_items = extracted.get(pid, [])
        initial = previous is None
        has_activity = bool(commits or new_items)
        activity_fingerprint = hashlib.sha256(json.dumps([commits, new_items], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if not rewrite_all and pstate.get("consecutive_rejects", 0) >= 2 and pstate.get("rejected_activity") == activity_fingerprint:
            print(f"ste_skip project={project_slug(project)} reason=two_rejects_same_activity")
            continue
        if rewrite_all:
            if previous is None and not has_activity:
                continue
        elif not initial and not has_activity:
            continue
        docs = first_build_context(root_path) if initial else []
        text_limit = max(0, MAX_INPUT_CHARS - sum(len(item) for item in docs) - sum(len(item) for item in commits))
        text = _limit_new_text(new_items, text_limit)
        activity_at = None
        last_activity = project.get("last_activity_at")
        if last_activity:
            try:
                activity_at = dt.datetime.fromisoformat(str(last_activity).replace("Z", "+00:00"))
                if activity_at.tzinfo is None:
                    activity_at = activity_at.replace(tzinfo=dt.timezone.utc)
                activity_at = activity_at.astimezone(dt.timezone.utc)
            except ValueError:
                activity_at = None
        for commit in commits:
            try:
                parsed = dt.datetime.fromisoformat(commit[:25].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                parsed = parsed.astimezone(dt.timezone.utc)
                activity_at = max(activity_at, parsed) if activity_at else parsed
            except ValueError:
                pass
        for stamp, _ in new_items:
            try:
                parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                parsed = parsed.astimezone(dt.timezone.utc)
                activity_at = max(activity_at, parsed) if activity_at else parsed
            except ValueError:
                pass
        prepared.append((project, root_path, previous, docs, commits, text, activity_at, new_items, pstate, activity_fingerprint, head_commit))

    prepared.sort(key=lambda item: item[0].get("last_activity_at") or "", reverse=True)
    call_limit = REWRITE_MAX_CALLS if rewrite_all else MAX_CALLS
    if dry_run:
        for project, *_ in prepared:
            print(f"would_update={project_slug(project)}")
        suffix = " rewrite_all=true" if rewrite_all else ""
        print(f"projects_checked={len(projects)} updated=0 model_calls=0{suffix}")
        return {"projects_checked": len(projects), "updated": 0, "model_calls": 0, "would_update": len(prepared)}

    ensure_config_write_path(STATE_FILE)
    ensure_config_write_path(CONTEXT_DIR / "brief.md")
    ensure_config_write_path(WORK_DIR / "brief-schema.json")
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    calls = updated = 0
    for project, root_path, previous, docs, commits, text, activity_at, new_items, pstate, activity_fingerprint, head_commit in prepared:
        if calls >= call_limit:
            break
        try:
            prompt = make_prompt(project, previous, docs, commits, text)
            generated = None
            rejected_rules = set()
            for attempt in range(STE_MAX_RETRIES + 1):
                calls += 1
                generated = run_model(prompt)
                style_issues = ste_check(generated)
                try:
                    validate_brief(generated)
                except (TypeError, ValueError) as exc:
                    style_issues.append(str(exc))
                    if str(exc) in {"owner_item_imperative", "non_agent_hours", "finished_claim_open_items"}:
                        rejected_rules.add(str(exc))
                if style_issues:
                    rejected_rules.update("ste_text_style" for issue in style_issues if issue not in {"owner_item_imperative", "non_agent_hours", "finished_claim_open_items"})
                if not style_issues:
                    break
                if attempt == STE_MAX_RETRIES or calls >= call_limit:
                    print(f"ste_reject project={project_slug(project)} rules={','.join(sorted(rejected_rules)) or 'unknown'}")
                    pstate["consecutive_rejects"] = int(pstate.get("consecutive_rejects", 0)) + 1
                    pstate["rejected_activity"] = activity_fingerprint
                    save_state(STATE_FILE, state)
                    generated = None
                    break
                prompt = (make_prompt(project, previous, docs, commits, text) +
                          "\n\nRewrite the brief. Fix each listed problem. Keep about, done, and the agent estimate basis to 20 words or fewer per sentence. Keep now to 1–2 full sentences within 240 characters. Use a named actor and a concrete item. Do not use 'the owner' or 'the team'. Problems:\n" +
                          "\n".join(f"- {sentence}" for sentence in style_issues))
            if generated is None:
                continue
            selected = prioritize_left_items(generated["left"])
            clean = {key: redact_text(value) if isinstance(value, str) else value
                     for key, value in generated.items() if key != "left"}
            clean["left"] = [{**item, "text": redact_text(item["text"])} for item in selected]
            clean["now"] = clean_now(clean["now"])
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
            payload = {**clean, "activity_through": activity_at.isoformat() if activity_at else None}
            api_request(f"/api/v1/projects/{project['id']}/brief", token, payload)
            destination = CONTEXT_DIR / f"{project_slug(project)}.md"
            write_context(destination, markdown(clean, dt.datetime.fromisoformat(timestamp)))
            for path, (offset, assigned_project_id, context) in pending_offsets.items():
                if assigned_project_id == str(project["id"]):
                    files_state[path] = {"offset": offset, "context": context}
            pstate["updated_at"] = timestamp
            if head_commit:
                pstate["activity_commit"] = head_commit
            pstate.pop("consecutive_rejects", None)
            pstate.pop("rejected_activity", None)
            save_state(STATE_FILE, state)
            updated += 1
        except (OSError, ValueError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError, subprocess.SubprocessError):
            continue
    suffix = " rewrite_all=true" if rewrite_all else ""
    print(f"projects_checked={len(projects)} updated={updated} model_calls={calls}{suffix}")
    result = {"projects_checked": len(projects), "updated": updated, "model_calls": calls}
    if rewrite_all:
        result["rewrite_all"] = True
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rewrite-all", action="store_true")
    parser.add_argument("--project", action="append", help="limit to a project slug or ID; repeat for several projects")
    args = parser.parse_args(argv)
    if not args.once and not args.dry_run and not args.rewrite_all:
        parser.error("use --once, --dry-run, or --rewrite-all")
    if args.rewrite_all and args.dry_run:
        parser.error("--rewrite-all cannot be combined with --dry-run")
    run_once(args.project, args.dry_run, args.rewrite_all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
