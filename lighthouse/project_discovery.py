from pathlib import PurePosixPath


EXCLUDED_PREFIXES = ("/private/var/folders", "/tmp", "/private/tmp")


def normalize_root(path: str) -> str:
    value = str(PurePosixPath(path)).rstrip("/") or "/"
    return value.lower()


def excluded_root(path: str, home: str | None = None) -> bool:
    normalized = normalize_root(path)
    if any(part == ".t3" and index + 1 < len(normalized.split("/")) and normalized.split("/")[index + 1] == "scratch" for index, part in enumerate(normalized.split("/"))):
        return True
    if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in EXCLUDED_PREFIXES):
        return True
    parts = normalized.split("/")
    if len(parts) >= 6 and parts[3:5] == ["documents", "codex"]:
        date_part = parts[5]
        if len(date_part) == 10 and date_part[4:5] == "-" and date_part[7:8] == "-":
            return True
    if home:
        scratch = normalize_root(home.rstrip("/") + "/.t3/scratch")
        if normalized == scratch or normalized.startswith(scratch + "/"):
            return True
    return False


def _worktree_project_name(path: str) -> str | None:
    parts = normalize_root(path).split("/")
    for index in range(len(parts) - 1):
        if parts[index:index + 2] == [".t3", "worktrees"] and index + 2 < len(parts):
            return parts[index + 2]
        if parts[index:index + 2] == [".codex", "worktrees"] and index + 3 < len(parts):
            return parts[index + 3]
    return None


def match_project_root(path: str | None, roots: list[dict]):
    if not path:
        return None
    candidate = normalize_root(path)
    matches = []
    for root in roots:
        root_path = normalize_root(root["path"])
        if candidate == root_path or candidate.startswith(root_path.rstrip("/") + "/"):
            matches.append((len(root_path), root))
    if matches:
        return max(matches, key=lambda pair: pair[0])[1]

    worktree_name = _worktree_project_name(path)
    if worktree_name:
        by_name = [root for root in roots if normalize_root(root["path"]).rsplit("/", 1)[-1] == worktree_name]
        if len(by_name) == 1:
            return by_name[0]
    return None


def ensure_auto_thread(conn, project_id, source: str, external_thread_id: str | None):
    from psycopg.types.json import Jsonb
    if not external_thread_id:
        return None
    existing = conn.execute("SELECT id,project_id FROM parent_threads WHERE source=%s AND external_thread_id=%s", (source, external_thread_id)).fetchone()
    if existing:
        return existing["id"] if existing["project_id"] == project_id else None
    controller = conn.execute("SELECT id FROM controllers WHERE project_id=%s", (project_id,)).fetchone()
    if not controller:
        controller = conn.execute("INSERT INTO controllers(project_id,policy) VALUES(%s,%s) RETURNING id", (project_id, Jsonb({}))).fetchone()
    row = conn.execute("INSERT INTO parent_threads(project_id,controller_id,source,external_thread_id,state) VALUES(%s,%s,%s,%s,'unknown') ON CONFLICT(source,external_thread_id) DO NOTHING RETURNING id", (project_id, controller["id"], source, external_thread_id)).fetchone()
    if row:
        return row["id"]
    existing = conn.execute("SELECT id,project_id FROM parent_threads WHERE source=%s AND external_thread_id=%s", (source, external_thread_id)).fetchone()
    return existing["id"] if existing and existing["project_id"] == project_id else None
