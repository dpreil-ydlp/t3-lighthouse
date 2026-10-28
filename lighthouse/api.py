import os
from datetime import datetime, timezone
import re
from typing import Any, Literal
from uuid import UUID
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, field_validator, model_validator
from psycopg.types.json import Jsonb
from lighthouse.project_discovery import ensure_auto_thread, excluded_root, match_project_root, normalize_root

from lighthouse.db import connection, row_json
from lighthouse.security import Principal, auth, claim_idempotency, require, save_idempotent

app = FastAPI(title="Lighthouse API", version="0.2.0")


class ProjectIn(BaseModel):
    name: str
    kind: str = "other"
    description: str = ""
    next_action: str | None = None


class BriefLeftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(min_length=1, max_length=200)
    who: Literal["you", "agents", "outside"]
    hours: StrictFloat | StrictInt | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_hours_owner(self):
        if self.who == "agents":
            if "hours" not in self.model_fields_set:
                raise ValueError("agent items require hours")
        elif "hours" in self.model_fields_set:
            raise ValueError("owner and outside items must not have hours")
        return self


class ProjectBriefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    about: StrictStr = Field(min_length=1, max_length=600)
    now: StrictStr = Field(min_length=1, max_length=240)
    done: list[StrictStr] = Field(max_length=8)
    left: list[BriefLeftItem] = Field(max_length=32)
    eta_basis: StrictStr | None = Field(default=None, max_length=300)
    activity_through: datetime | None = None

    @field_validator("done")
    @classmethod
    def brief_items_limited(cls, values):
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("brief items must contain 1 to 200 characters")
        return values

    @field_validator("about", "now")
    @classmethod
    def brief_text_not_blank(cls, value):
        if not value.strip():
            raise ValueError("brief text must not be blank")
        return value

    @field_validator("eta_basis")
    @classmethod
    def brief_basis_not_blank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("brief text must not be blank")
        return value

    @model_validator(mode="after")
    def no_finished_claim_with_open_items(self):
        finished = re.compile(r"\b(?:the (?:whole )?project|everything|all work|it)\s+(?:is|was|has been)\s+(?:(?:now|fully|completely|already)\s+)*(?:finished|complete|completed|done)\b|\ball\s+(?:(?:now|fully|completely|already)\s+)*done\b", re.I)
        if self.left and (finished.search(self.now) or any(finished.search(item) for item in self.done)):
            raise ValueError("brief claims all work is finished while open items remain")
        return self


class TaskIn(BaseModel):
    title: str
    description: str = ""
    acceptance: dict = Field(default_factory=dict)
    thresholds: dict = Field(default_factory=dict)

    @field_validator("thresholds")
    @classmethod
    def positive_integer_thresholds(cls, value):
        if any(type(seconds) is not int or seconds <= 0 for seconds in value.values()):
            raise ValueError("threshold values must be positive integers")
        return value


class ProgressIn(BaseModel):
    project_id: UUID | None = None
    task_id: UUID | None = None
    parent_thread_id: UUID | None = None
    source: str = "mcp"
    confidence: str = "high"
    event_type: str = "progress"
    status: Literal["running", "blocked", "needs_review", "complete"] | None = None
    summary: str = ""
    meaningful: bool = False
    source_event_id: str
    observed_at: datetime
    working_directory: str | None = None
    external_thread_id: str | None = None
    details: dict = Field(default_factory=dict)


class ChannelIn(BaseModel):
    channel: Literal["t3_webhook", "mac_alert", "test_sink"]
    t3_webhook_url: str | None = None


class UnassignedProjectIn(BaseModel):
    name: str
    kind: str = "other"


class UnassignedBindIn(BaseModel):
    project_id: UUID


class UnassignedIgnoreIn(BaseModel):
    source: str
    working_directory: str | None = None
    external_thread_id: str | None = None


class DiscoveredRoot(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    name: str = Field(min_length=1, max_length=200)
    origin: Literal["t3", "folder"]


class ThreadTitleIn(BaseModel):
    external_thread_id: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=2000)
    title_origin: Literal["t3", "native"] = "native"
    t3_thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9:._%-]{1,200}$")
    t3_subagent: bool = False


class DiscoveryIn(BaseModel):
    roots: list[DiscoveredRoot] = Field(default_factory=list, max_length=5000)
    titles: list[ThreadTitleIn] = Field(default_factory=list, max_length=500)
    environment_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9:._%-]{1,200}$")


class DiscoveredSubagent(BaseModel):
    parent_t3_thread_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9:._%-]+$")
    parent_title: str = Field(default="T3 parent chat", min_length=1, max_length=500)
    parent_project_root: str | None = Field(default=None, max_length=2000)
    child_t3_thread_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9:._%-]+$")
    title: str = Field(min_length=1, max_length=500)
    status: str = Field(min_length=1, max_length=80)
    last_activity_at: datetime
    last_child_output_at: datetime | None = None


class SubagentDiscoveryIn(BaseModel):
    subagents: list[DiscoveredSubagent] = Field(default_factory=list, max_length=5000)


class T3McpStatusIn(BaseModel):
    expires_at: datetime


class T3McpFailureIn(BaseModel):
    retryable: bool
    reason: Literal["send_failed", "malformed_thread_id", "child_not_working"]


def required_key(key: str | None):
    if not key:
        raise HTTPException(400, "Idempotency-Key header required")
    return key


def check_scope(principal: Principal, scope: str):
    if scope not in principal.scopes:
        raise HTTPException(403, "Required scope is missing")


def valid_webhook_url(value: str | None):
    try:
        parsed = urlsplit(value or "")
        return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ready"}


@app.get("/api/v1/me")
def current_principal(principal: Principal = Depends(auth)):
    return {"id": principal.id, "name": principal.name, "kind": principal.kind}


def attention(project: dict):
    if project.get("incident_state") in {"open", "notifying", "awaiting_ack", "escalated"}:
        return "incident", "An incident is open"
    if project.get("stalled"):
        return "stalled", "No meaningful progress within the task threshold"
    if project.get("milestone_overdue"):
        return "milestone_overdue", "A milestone is overdue"
    if project.get("inactive"):
        return "inactive", "No recent signal was observed"
    return "healthy", "No attention needed"


def t3_url(environment_id: str | None, thread_id: str | None):
    if not environment_id or not thread_id:
        return None
    encode = lambda value: quote(value, safe="-_.!~*'()")
    return f"t3code://threads/{encode(environment_id)}/{encode(thread_id)}"


def add_t3_urls(project: dict):
    project["t3_url"] = t3_url(project.pop("t3_environment_id", None), project.pop("latest_t3_thread_id", None))
    recent = project.get("recent_threads")
    if isinstance(recent, list):
        for thread in recent:
            thread["t3_url"] = t3_url(thread.pop("t3_environment_id", None), thread.pop("t3_thread_id", None))
            thread.pop("t3_subagent", None)
    return project


def add_thread_t3_url(thread: dict):
    thread["t3_url"] = t3_url(thread.pop("t3_environment_id", None), thread.pop("t3_thread_id", None))
    thread.pop("t3_subagent", None)
    return thread


def derive_brief(brief: dict | None, activity_state: str, last_activity_at: datetime | None) -> dict | None:
    if not brief:
        return None
    raw_left = brief.get("left") or []
    left = []
    for item in raw_left:
        if isinstance(item, dict) and item.get("who") in {"you", "agents", "outside"}:
            left.append({"text": item.get("text", ""), "who": item["who"], **({"hours": item.get("hours")} if item["who"] == "agents" else {})})
        elif isinstance(item, str):
            left.append({"text": item, "who": "unknown"})
    for item in left:
        if item["who"] == "you":
            item["text"] = re.sub(r"(?i)^You (?:must|need to)\s+", "", item["text"]).strip()
            item["text"] = re.sub(r"^([a-z])", lambda match: match.group(1).upper(), item["text"])
    owner_items = [item for item in left if item["who"] == "you"]
    agent_items = [item for item in left if item["who"] == "agents"]
    agent_hours = None if any(item["who"] == "unknown" for item in left) or any(item.get("hours") is None for item in agent_items) else sum(item["hours"] for item in agent_items)
    age_days = None
    if last_activity_at:
        try:
            stamp = last_activity_at if isinstance(last_activity_at, datetime) else datetime.fromisoformat(str(last_activity_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 86400
        except (TypeError, ValueError):
            pass
    if activity_state == "working_now":
        state = "agents_working"
    elif owner_items:
        state = "waiting_on_you"
    elif age_days is not None and age_days >= 7:
        state = "paused"
    elif not left:
        state = "nothing_open"
    else:
        state = "idle"
    paused_since = last_activity_at.isoformat() if hasattr(last_activity_at, "isoformat") else last_activity_at
    result = {
        "about": brief.get("about"),
        "now": brief.get("now"),
        "done": brief.get("done") or [],
        "left": left,
        "eta_basis": brief.get("eta_basis"),
        "updated_at": brief.get("updated_at"),
        "version": brief.get("version"),
        "schema_version": brief.get("schema_version", 1 if any(item["who"] == "unknown" for item in left) else 2),
        "activity_through": brief.get("activity_through"),
        "checked_at": brief.get("updated_at"),
        "last_activity_at": paused_since,
        "needs_you": {"action": owner_items[0]["text"], "count": len(owner_items)} if owner_items else None,
        "agent_hours": agent_hours,
        "state": state,
        "paused_since": paused_since if state == "paused" and paused_since else None,
    }
    return result


PROJECT_SUMMARY_SELECT = """
  p.*,es.last_signal_at,es.last_signal_source,es.last_signal_confidence,es.last_activity_at,
  COALESCE(ea.tools,ARRAY[]::text[]) AS tools,COALESCE(ea.active_threads,0) AS active_threads,
  COALESCE(ins.open_incidents,0) AS open_incidents,COALESCE(rt.recent_threads,'[]'::jsonb) AS recent_threads,
  ts.environment_id AS t3_environment_id,top_t3.thread_id AS latest_t3_thread_id,
  COALESCE(rs.roots,ARRAY[]::text[]) AS roots,ins.incident_state,
  EXISTS(SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.status='running' AND t.last_meaningful_progress_at < now()-COALESCE((t.thresholds->>'meaningful_progress_seconds')::int,1800)*interval '1 second') AS stalled,
  EXISTS(SELECT 1 FROM milestones m WHERE m.project_id=p.id AND m.status='open' AND m.due_at < now()) AS milestone_overdue,
  p.last_meaningful_progress_at < now()-p.inactivity_threshold AS inactive,
  COALESCE(brief.summary, 'null'::jsonb) AS brief,
  CASE WHEN es.last_activity_at >= now()-interval '15 minutes' THEN 'working_now'
       WHEN es.last_activity_at >= now()-interval '24 hours' THEN 'today'
       WHEN es.last_activity_at >= now()-interval '7 days' THEN 'this_week' ELSE 'quiet' END AS activity_state
  FROM projects p
  LEFT JOIN LATERAL (
    SELECT observed_at AS last_activity_at,observed_at AS last_signal_at,
      source AS last_signal_source,confidence AS last_signal_confidence
    FROM events WHERE project_id=p.id ORDER BY observed_at DESC,id DESC LIMIT 1
  ) es ON true
  LEFT JOIN LATERAL (
    SELECT array_agg(DISTINCT source ORDER BY source) FILTER (WHERE last_observed_at>=now()-interval '7 days') AS tools,
      count(DISTINCT parent_thread_id) FILTER (WHERE parent_thread_id IS NOT NULL AND last_observed_at>=now()-interval '2 hours') AS active_threads
    FROM project_event_summary WHERE project_id=p.id AND last_observed_at>=now()-interval '14 days'
  ) ea ON true
  LEFT JOIN t3_discovery_state ts ON ts.singleton=true
  LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object('title',recent.title,'source',recent.source,'last_activity_at',recent.last_activity_at,
      't3_thread_id',recent.t3_thread_id,'t3_environment_id',ts.environment_id,'t3_subagent',recent.t3_subagent)
      ORDER BY recent.last_activity_at DESC) AS recent_threads
    FROM (SELECT pt.source,pt.external_thread_id,tt.title,tt.t3_thread_id,tt.t3_subagent,latest.last_activity_at
      FROM parent_threads pt
      JOIN LATERAL (SELECT observed_at AS last_activity_at FROM events
        WHERE parent_thread_id=pt.id ORDER BY observed_at DESC LIMIT 1) latest ON true
      LEFT JOIN thread_titles tt ON tt.source=pt.source AND tt.external_thread_id=pt.external_thread_id
      WHERE pt.project_id=p.id ORDER BY latest.last_activity_at DESC LIMIT 3) recent
  ) rt ON true
  LEFT JOIN LATERAL (
    SELECT tt.t3_thread_id AS thread_id FROM parent_threads pt
      JOIN thread_titles tt ON tt.source=pt.source AND tt.external_thread_id=pt.external_thread_id
      WHERE pt.project_id=p.id AND tt.t3_thread_id IS NOT NULL AND tt.t3_subagent=false
      ORDER BY (SELECT max(e.observed_at) FROM events e WHERE e.parent_thread_id=pt.id) DESC NULLS LAST
      LIMIT 1
  ) top_t3 ON true
  LEFT JOIN LATERAL (
    SELECT array_agg(path ORDER BY path) AS roots FROM project_roots WHERE project_id=p.id
  ) rs ON true
  LEFT JOIN LATERAL (
    SELECT count(*) AS open_incidents,(array_agg(state ORDER BY opened_at DESC))[1] AS incident_state
    FROM incidents WHERE project_id=p.id AND state NOT IN ('resolved','closed')
  ) ins ON true
  LEFT JOIN LATERAL (
    SELECT jsonb_build_object(
      'about', b.about, 'now', b.now, 'done', b.done, 'left', b."left",
      'eta_basis', b.eta_basis, 'activity_through', b.activity_through,
      'updated_at', b.updated_at, 'version', b.version, 'schema_version', b.schema_version
    ) AS summary
    FROM project_briefs b WHERE b.project_id=p.id
  ) brief ON true
"""


@app.get("/api/v1/projects")
def list_projects(view: Literal["active", "archived", "hidden", "all"] = "active", principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        filter_clause = {"active": "p.status<>'archived' AND p.hidden=false", "archived": "p.status='archived'", "hidden": "p.hidden=true", "all": "true"}[view]
        rows = conn.execute(f"SELECT {PROJECT_SUMMARY_SELECT} WHERE {filter_clause} ORDER BY CASE WHEN es.last_activity_at>=now()-interval '15 minutes' THEN 0 ELSE 1 END,es.last_activity_at DESC NULLS LAST,p.name").fetchall()
    result = []
    for row in rows:
        item = row_json(row)
        add_t3_urls(item)
        if item.get("brief"):
            item["brief"] = derive_brief(item["brief"], item["activity_state"], item.get("last_activity_at"))
        item["attention_state"], item["attention_reason"] = attention(item)
        result.append(item)
    return {"items": result, "next_cursor": None}


@app.post("/api/v1/projects")
def create_project(body: ProjectIn, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("project:write"))):
    key = required_key(idempotency_key)
    payload = body.model_dump()
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, payload)
        if replay:
            return prior
        project = conn.execute("INSERT INTO projects(name,kind,description,next_action) VALUES(%s,%s,%s,%s) RETURNING *", (body.name, body.kind, body.description, body.next_action)).fetchone()
        result = {"project": row_json(project)}
        save_idempotent(conn, principal, key, result)
        return result


@app.get("/api/v1/projects/{project_id}")
def get_project(project_id: UUID, principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        project = conn.execute(f"SELECT {PROJECT_SUMMARY_SELECT} WHERE p.id=%s", (project_id,)).fetchone()
        if not project:
            raise HTTPException(404, "Project not found")
        controller = conn.execute("SELECT * FROM controllers WHERE project_id=%s", (project_id,)).fetchone()
        threads = conn.execute("SELECT * FROM parent_threads WHERE project_id=%s ORDER BY first_seen_at DESC", (project_id,)).fetchall()
        thread_activity = conn.execute("""SELECT pt.external_thread_id,pt.source,tt.title,max(e.observed_at) last_activity_at,
          tt.t3_thread_id,tt.t3_subagent,ts.environment_id AS t3_environment_id,
          (array_agg(e.details->>'working_directory' ORDER BY e.observed_at DESC) FILTER (WHERE e.details ? 'working_directory'))[1] working_directory
          FROM parent_threads pt JOIN events e ON e.parent_thread_id=pt.id
          LEFT JOIN thread_titles tt ON tt.source=pt.source AND tt.external_thread_id=pt.external_thread_id
          LEFT JOIN t3_discovery_state ts ON ts.singleton=true
          WHERE pt.project_id=%s GROUP BY pt.external_thread_id,pt.source,tt.title,tt.t3_thread_id,tt.t3_subagent,ts.environment_id
          ORDER BY max(e.observed_at) DESC LIMIT 50""", (project_id,)).fetchall()
        tasks = conn.execute("""SELECT t.*,(SELECT i.state FROM incidents i WHERE i.task_id=t.id ORDER BY i.opened_at DESC LIMIT 1) AS latest_incident_state
          FROM tasks t WHERE t.project_id=%s
            AND ((t.derived_source='t3_subagent' AND t.status='running')
              OR (t.derived_source IS NULL AND t.status NOT IN ('complete','verified','cancelled','abandoned')))
          ORDER BY t.created_at DESC""", (project_id,)).fetchall()
        sources = conn.execute("SELECT id,source,match_kind,match_value,confidence,created_at FROM project_sources WHERE project_id=%s ORDER BY created_at", (project_id,)).fetchall()
    controller_data = row_json(controller) if controller else None
    if controller_data:
        controller_data["policy"] = dict(controller_data["policy"] or {})
        controller_data["policy"].pop("t3_webhook_url", None)
    project_data = add_t3_urls(row_json(project))
    project_data["brief"] = derive_brief(project_data.get("brief"), project_data.get("activity_state", "quiet"), project_data.get("last_activity_at"))
    thread_items = [add_thread_t3_url(row_json(x)) for x in thread_activity]
    return {"project": project_data, "controller": controller_data, "parent_threads": [row_json(x) for x in threads], "threads": thread_items, "open_tasks": [row_json(x) for x in tasks], "sources": [row_json(x) for x in sources]}


@app.put("/api/v1/projects/{project_id}/brief")
def put_project_brief(project_id: UUID, body: ProjectBriefIn, principal: Principal = Depends(auth)):
    if principal.kind != "user":
        check_scope(principal, "project:brief")
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE id=%s FOR UPDATE", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
        current = conn.execute("SELECT version FROM project_briefs WHERE project_id=%s FOR UPDATE", (project_id,)).fetchone()
        version = (current["version"] if current else 0) + 1
        latest_activity = conn.execute("SELECT max(observed_at) AS last_activity_at FROM events WHERE project_id=%s", (project_id,)).fetchone()["last_activity_at"]
        now = datetime.now(timezone.utc)
        age = now - latest_activity.astimezone(timezone.utc) if latest_activity else None
        activity_state = ("working_now" if age is not None and age.total_seconds() <= 15 * 60 else
                          "today" if age is not None and age.total_seconds() <= 24 * 60 * 60 else
                          "this_week" if age is not None and age.total_seconds() <= 7 * 24 * 60 * 60 else "quiet")
        brief = conn.execute("""INSERT INTO project_briefs(project_id,about,now,done,"left",remaining_hours,eta_confidence,eta_basis,activity_through,version,schema_version)
          VALUES(%s,%s,%s,%s,%s,NULL,NULL,%s,%s,%s,2)
          ON CONFLICT(project_id) DO UPDATE SET about=EXCLUDED.about,now=EXCLUDED.now,done=EXCLUDED.done,"left"=EXCLUDED."left",
            remaining_hours=NULL,eta_confidence=NULL,eta_basis=EXCLUDED.eta_basis,
            activity_through=EXCLUDED.activity_through,updated_at=now(),version=EXCLUDED.version,schema_version=2
          RETURNING *""", (project_id, body.about, body.now, Jsonb(body.done),
                              Jsonb([item.model_dump(mode="json", exclude_unset=True) for item in body.left]),
                              body.eta_basis, body.activity_through, version)).fetchone()
        saved = jsonable_encoder(derive_brief(row_json(brief), activity_state, latest_activity))
        conn.execute("INSERT INTO project_brief_history(project_id,version,body) VALUES(%s,%s,%s)",
                     (project_id, version, Jsonb(saved)))
        return {"brief": saved}


@app.post("/api/v1/discovery/roots")
def discover_projects(body: DiscoveryIn, principal: Principal = Depends(require("project:discover"))):
    with connection() as conn:
        if body.environment_id:
            conn.execute("""INSERT INTO t3_discovery_state(singleton,environment_id) VALUES(true,%s)
              ON CONFLICT(singleton) DO UPDATE SET environment_id=EXCLUDED.environment_id,updated_at=now()""", (body.environment_id,))
        projects = []
        for root in body.roots:
            normalized = normalize_root(root.path)
            if excluded_root(root.path):
                continue
            existing = conn.execute("SELECT p.id,p.name,p.hidden,p.status FROM project_roots r JOIN projects p ON p.id=r.project_id WHERE r.path=%s", (normalized,)).fetchone()
            if existing:
                project_id = existing["id"]
            else:
                row = conn.execute("INSERT INTO projects(name,kind,origin) VALUES(%s,'other','auto') RETURNING id", (root.name.strip(),)).fetchone()
                project_id = row["id"]
                conn.execute("INSERT INTO project_roots(project_id,path,display_path,display_name,origin) VALUES(%s,%s,%s,%s,%s)", (project_id, normalized, root.path, root.name.strip(), root.origin))
                existing = {"id": project_id, "name": root.name.strip(), "hidden": False, "status": "active"}
            projects.append({"id": str(project_id), "name": existing["name"], "hidden": existing["hidden"], "status": existing["status"]})
        for item in body.titles:
            title = item.title.strip()[:120] if item.title else None
            if title and title.casefold() in {"new thread", "untitled", "untitled session"}:
                title = None
            if title is None and item.t3_thread_id is None:
                continue
            conn.execute("""INSERT INTO thread_titles(source,external_thread_id,title,title_origin,t3_thread_id,t3_subagent) VALUES(%s,%s,%s,%s,%s,%s)
              ON CONFLICT(source,external_thread_id) DO UPDATE SET
                title=CASE
                  WHEN EXCLUDED.title IS NOT NULL AND NOT (thread_titles.title IS NOT NULL AND thread_titles.title_origin='t3' AND EXCLUDED.title_origin='native') THEN EXCLUDED.title
                  ELSE thread_titles.title END,
                title_origin=CASE
                  WHEN EXCLUDED.title IS NOT NULL AND NOT (thread_titles.title IS NOT NULL AND thread_titles.title_origin='t3' AND EXCLUDED.title_origin='native') THEN EXCLUDED.title_origin
                  ELSE thread_titles.title_origin END,
                t3_thread_id=COALESCE(EXCLUDED.t3_thread_id,thread_titles.t3_thread_id),
                t3_subagent=CASE WHEN EXCLUDED.t3_thread_id IS NULL THEN thread_titles.t3_subagent ELSE EXCLUDED.t3_subagent END,
                updated_at=now()""",
              (item.source, item.external_thread_id, title, item.title_origin, item.t3_thread_id, item.t3_subagent))
    return {"projects": projects, "titles_stored": len(body.titles)}


@app.post("/api/v1/discovery/subagents")
def discover_subagents(body: SubagentDiscoveryIn, principal: Principal = Depends(require("project:discover"))):
    if os.getenv("LIGHTHOUSE_DERIVE_SUBAGENT_TASKS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {"tasks_created_or_updated": 0, "disabled": True}
    count = 0
    seen_child_ids = [child.child_t3_thread_id for child in body.subagents]
    with connection() as conn:
        env = conn.execute("SELECT environment_id FROM t3_discovery_state WHERE singleton=true").fetchone()
        environment_id = env["environment_id"] if env else None
        for child in body.subagents:
            parent = conn.execute("""SELECT pt.id parent_thread_id,pt.project_id,pt.source
              FROM thread_titles tt JOIN parent_threads pt ON pt.source=tt.source AND pt.external_thread_id=tt.external_thread_id
              WHERE tt.t3_thread_id=%s""", (child.parent_t3_thread_id,)).fetchone()
            if not parent:
                root = normalize_root(child.parent_project_root) if child.parent_project_root else None
                project = conn.execute("SELECT project_id FROM project_roots WHERE path=%s", (root,)).fetchone() if root else None
                if not project and root and not excluded_root(root):
                    project_name = root.rstrip("/").rsplit("/", 1)[-1] or "T3 project"
                    project = conn.execute("INSERT INTO projects(name,kind,origin) VALUES(%s,'other','auto') RETURNING id AS project_id", (project_name,)).fetchone()
                    conn.execute("INSERT INTO project_roots(project_id,path,display_path,display_name,origin) VALUES(%s,%s,%s,%s,'t3')",
                                 (project["project_id"],root,child.parent_project_root,project_name))
                if not project:
                    continue
                parent_thread_id = ensure_auto_thread(conn, project["project_id"], "t3", child.parent_t3_thread_id)
                conn.execute("""INSERT INTO thread_titles(source,external_thread_id,title,title_origin,t3_thread_id,t3_subagent)
                  VALUES('t3',%s,%s,'t3',%s,false) ON CONFLICT(source,external_thread_id) DO UPDATE SET
                  title=EXCLUDED.title,title_origin='t3',t3_thread_id=EXCLUDED.t3_thread_id,t3_subagent=false,updated_at=now()""",
                  (child.parent_t3_thread_id, child.parent_title[:120], child.parent_t3_thread_id))
                parent = {"parent_thread_id": parent_thread_id, "project_id": project["project_id"], "source": "t3"}
            status = "running" if child.status == "running" else ("complete" if child.status == "completed" else ("cancelled" if child.status == "cancelled" else "blocked"))
            child_link = f"t3code://threads/{environment_id}/{child.child_t3_thread_id}" if environment_id else f"t3code://threads/{child.child_t3_thread_id}"
            title = child.title.strip()[:500]
            task = conn.execute("""INSERT INTO tasks(project_id,parent_thread_id,title,description,status,started_at,
              last_meaningful_progress_at,derived_source,derived_external_id)
              VALUES(%s,%s,%s,%s,%s,%s,COALESCE(%s,%s),'t3_subagent',%s)
              ON CONFLICT(derived_source,derived_external_id) WHERE derived_source IS NOT NULL AND derived_external_id IS NOT NULL
              DO UPDATE SET project_id=EXCLUDED.project_id,parent_thread_id=EXCLUDED.parent_thread_id,
                title=EXCLUDED.title,description=EXCLUDED.description,status=EXCLUDED.status,
                last_meaningful_progress_at=CASE WHEN %s::timestamptz IS NULL
                  THEN tasks.last_meaningful_progress_at
                  ELSE GREATEST(COALESCE(tasks.last_meaningful_progress_at,%s),%s) END,
                updated_at=now()
              RETURNING id""", (parent["project_id"], parent["parent_thread_id"], f"Subagent: {title}", child_link, status,
               child.last_activity_at, child.last_child_output_at, child.last_activity_at, child.child_t3_thread_id,
               child.last_child_output_at, child.last_child_output_at, child.last_child_output_at)).fetchone()
            if status == "running":
                resolved = conn.execute("""UPDATE incidents i SET state='resolved',resumed_progress_at=%s,resolved_at=now()
                  WHERE i.task_id=%s AND i.state IN ('open','notifying','awaiting_ack','escalated')
                    AND %s::timestamptz IS NOT NULL AND (i.details->>'stall_evidence_at')::timestamptz<%s
                  RETURNING i.id,i.project_id,i.task_id,i.parent_thread_id""",
                  (child.last_child_output_at, task["id"], child.last_child_output_at, child.last_child_output_at)).fetchall()
                for incident in resolved:
                    conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,'incident_resolved','api','high',%s,now(),%s) ON CONFLICT(source,source_event_id) DO NOTHING",
                                 (incident["project_id"],incident["task_id"],incident["parent_thread_id"],f"incident-resolved:{incident['id']}",Jsonb({"incident_id":str(incident["id"]),"reason":"subagent progress resumed"})))
                conn.execute("UPDATE notification_outbox SET state='failed',last_error='Subagent progress resumed before notification delivery' WHERE incident_id IN (SELECT id FROM incidents WHERE task_id=%s AND state='resolved') AND state='pending' AND delivered_at IS NULL", (task["id"],))
            else:
                conn.execute("UPDATE incidents SET state='resolved',resolved_at=COALESCE(resolved_at,now()) WHERE task_id=%s AND state IN ('open','notifying','awaiting_ack','escalated')", (task["id"],))
                conn.execute("UPDATE notification_outbox SET state='failed',last_error='Subagent stopped working before notification delivery' WHERE incident_id IN (SELECT id FROM incidents WHERE task_id=%s AND state='resolved') AND state='pending' AND delivered_at IS NULL", (task["id"],))
            count += 1
        stopped = conn.execute("""UPDATE tasks SET status='blocked',updated_at=now()
          WHERE derived_source='t3_subagent' AND status='running' AND NOT (derived_external_id=ANY(%s))
          RETURNING id""", (seen_child_ids,)).fetchall()
        stopped_ids = [row["id"] for row in stopped]
        if stopped_ids:
            conn.execute("UPDATE incidents SET state='resolved',resolved_at=COALESCE(resolved_at,now()) WHERE task_id=ANY(%s) AND state IN ('open','notifying','awaiting_ack','escalated')", (stopped_ids,))
            conn.execute("UPDATE notification_outbox SET state='failed',last_error='Subagent absent from the current source projection' WHERE incident_id IN (SELECT id FROM incidents WHERE task_id=ANY(%s) AND state='resolved') AND state='pending' AND delivered_at IS NULL", (stopped_ids,))
    return {"tasks_created_or_updated": count, "disabled": False}


@app.get("/api/v1/other-activity")
def other_activity(principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        rows = conn.execute("""SELECT u.working_directory, u.source, max(u.observed_at) AS last_activity_at,
          count(DISTINCT u.external_thread_id) AS sessions, max(u.external_thread_id) AS external_thread_id
          FROM unmapped_observations u WHERE u.resolved_project_id IS NULL
          GROUP BY u.working_directory,u.source ORDER BY max(u.observed_at) DESC LIMIT 500""").fetchall()
    items = []
    for row in rows:
        directory = row["working_directory"]
        label = directory.rstrip("/").rsplit("/", 1)[-1] if directory else (row["external_thread_id"] or "Unknown session")
        items.append({"label": label, "working_directory": directory, "source": row["source"], "last_activity_at": row["last_activity_at"].isoformat(), "sessions": row["sessions"]})
    return {"items": items}


@app.put("/api/v1/controllers/{controller_id}/channel")
def set_controller_channel(controller_id: UUID, body: ChannelIn, principal: Principal = Depends(require("project:write", user_only=True))):
    if body.channel == "t3_webhook" and not valid_webhook_url(body.t3_webhook_url):
        raise HTTPException(422, "T3 channel requires an HTTPS webhook URL")
    with connection() as conn:
        row = conn.execute("SELECT policy FROM controllers WHERE id=%s FOR UPDATE", (controller_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Controller not found")
        policy = dict(row["policy"] or {})
        policy["channel"] = body.channel
        if body.channel == "t3_webhook":
            policy["t3_webhook_url"] = body.t3_webhook_url
        else:
            policy.pop("t3_webhook_url", None)
        conn.execute("UPDATE controllers SET policy=%s,updated_at=now() WHERE id=%s", (Jsonb(policy), controller_id))
    return {"controller_id": str(controller_id), "channel": body.channel}


@app.get("/api/v1/unassigned")
def list_unassigned(show_ignored: bool = False, principal: Principal = Depends(require("source:bind", user_only=True))):
    with connection() as conn:
        rows = conn.execute("""
          SELECT u.source,u.working_directory,
            CASE WHEN working_directory IS NULL THEN external_thread_id END AS external_thread_id,
            (array_agg(u.id ORDER BY u.observed_at DESC))[1] AS observation_id,
            max(u.observed_at) AS last_seen,count(*) AS observations,
            (array_agg(u.confidence ORDER BY u.observed_at DESC))[1] AS confidence,
            bool_or(g.source IS NOT NULL) AS ignored
          FROM unmapped_observations u
          LEFT JOIN ignored_observation_groups g ON g.source=u.source
            AND g.match_kind=CASE WHEN u.working_directory IS NULL THEN 'external_thread_id' ELSE 'working_directory' END
            AND g.match_value=COALESCE(u.working_directory,u.external_thread_id)
          WHERE u.resolved_project_id IS NULL
          GROUP BY u.source,u.working_directory,CASE WHEN u.working_directory IS NULL THEN u.external_thread_id END
          HAVING bool_or(g.source IS NOT NULL)=false OR %s
          ORDER BY max(u.observed_at) DESC
        """, (show_ignored,)).fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.post("/api/v1/unassigned/ignore")
def ignore_unassigned(body: UnassignedIgnoreIn, principal: Principal = Depends(require("source:bind", user_only=True))):
    match_kind = "working_directory" if body.working_directory else "external_thread_id"
    match_value = body.working_directory or body.external_thread_id
    if not match_value:
        raise HTTPException(422, "A working directory or external thread ID is required")
    with connection() as conn:
        conn.execute("""INSERT INTO ignored_observation_groups(source,match_kind,match_value,ignored_by)
          VALUES(%s,%s,%s,%s) ON CONFLICT(source,match_kind,match_value)
          DO UPDATE SET ignored_by=EXCLUDED.ignored_by,ignored_at=now()""", (body.source,match_kind,match_value,principal.id))
    return {"ignored": True}


@app.post("/api/v1/unassigned/restore")
def restore_unassigned(body: UnassignedIgnoreIn, principal: Principal = Depends(require("source:bind", user_only=True))):
    match_kind = "working_directory" if body.working_directory else "external_thread_id"
    match_value = body.working_directory or body.external_thread_id
    if not match_value:
        raise HTTPException(422, "A working directory or external thread ID is required")
    with connection() as conn:
        conn.execute("DELETE FROM ignored_observation_groups WHERE source=%s AND match_kind=%s AND match_value=%s", (body.source,match_kind,match_value))
    return {"ignored": False}


def bind_observation_group(conn, observation):
    source = observation["source"]
    if observation["working_directory"]:
        return source, "working_directory", observation["working_directory"], "working_directory IS NOT DISTINCT FROM %s", observation["working_directory"]
    if not observation["external_thread_id"]:
        raise HTTPException(422, "This observation has no bindable session or working directory")
    return source, "external_thread_id", observation["external_thread_id"], "external_thread_id IS NOT DISTINCT FROM %s", observation["external_thread_id"]


def save_observation_binding(conn, observation, project_id):
    source, match_kind, match_value, clause, value = bind_observation_group(conn, observation)
    existing = conn.execute("SELECT project_id FROM project_sources WHERE source=%s AND match_kind=%s AND match_value=%s", (source, match_kind, match_value)).fetchone()
    if existing and str(existing["project_id"]) != str(project_id):
        raise HTTPException(409, "This source is already bound to another project")
    mapped = conn.execute("""
      INSERT INTO project_sources(project_id,source,match_kind,match_value,confidence)
      VALUES(%s,%s,%s,%s,'high')
      ON CONFLICT(source,match_kind,match_value) DO UPDATE SET confidence='high'
        WHERE project_sources.project_id=EXCLUDED.project_id
      RETURNING project_id
    """, (project_id, source, match_kind, match_value)).fetchone()
    if not mapped:
        raise HTTPException(409, "This source is already bound to another project")
    conn.execute(f"UPDATE unmapped_observations SET resolved_project_id=%s,bound_at=now() WHERE source=%s AND {clause} AND resolved_project_id IS NULL", (project_id, source, value))


@app.post("/api/v1/unassigned/{observation_id}/bind")
def bind_unassigned(observation_id: UUID, body: UnassignedBindIn, principal: Principal = Depends(require("source:bind", user_only=True))):
    with connection() as conn:
        observation = conn.execute("SELECT * FROM unmapped_observations WHERE id=%s AND resolved_project_id IS NULL FOR UPDATE", (observation_id,)).fetchone()
        if not observation:
            raise HTTPException(404, "Unassigned observation not found")
        project_id = body.project_id
        if not conn.execute("SELECT 1 FROM projects WHERE id=%s", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
        save_observation_binding(conn, observation, project_id)
    return {"project_id": str(project_id), "bound": True}


@app.post("/api/v1/unassigned/{observation_id}/create-project")
def create_project_from_unassigned(observation_id: UUID, body: UnassignedProjectIn, principal: Principal = Depends(require("source:bind", user_only=True))):
    check_scope(principal, "project:write")
    with connection() as conn:
        observation = conn.execute("SELECT * FROM unmapped_observations WHERE id=%s AND resolved_project_id IS NULL FOR UPDATE", (observation_id,)).fetchone()
        if not observation:
            raise HTTPException(404, "Unassigned observation not found")
        project = conn.execute("INSERT INTO projects(name,kind) VALUES(%s,%s) RETURNING id", (body.name, body.kind)).fetchone()
        save_observation_binding(conn, observation, project["id"])
    return {"project_id": str(project["id"]), "bound": True}


@app.post("/api/v1/projects/{project_id}/sources")
def add_source(project_id: UUID, body: dict[str, Any], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("source:bind", user_only=True))):
    key = required_key(idempotency_key)
    if body.get("match_kind") not in {"working_directory", "external_thread_id"}:
        raise HTTPException(422, "Unsupported match_kind")
    if not all(isinstance(body.get(field), str) and body[field] for field in ("source", "match_value")):
        raise HTTPException(422, "source and match_value are required strings")
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, body)
        if replay:
            return prior
        existing = conn.execute("SELECT id,project_id FROM project_sources WHERE source=%s AND match_kind=%s AND match_value=%s", (body["source"], body["match_kind"], body["match_value"])).fetchone()
        if existing and existing["project_id"] != project_id:
            raise HTTPException(409, "This exact source is already bound to another project")
        source = conn.execute("INSERT INTO project_sources(project_id,source,match_kind,match_value,confidence) VALUES(%s,%s,%s,%s,'high') ON CONFLICT(source,match_kind,match_value) DO UPDATE SET confidence='high' RETURNING id", (project_id, body["source"], body["match_kind"], body["match_value"])).fetchone()
        result = {"source_id": str(source["id"]), "confidence": "high"}
        save_idempotent(conn, principal, key, result)
        return result


@app.post("/api/v1/projects/{project_id}/controllers")
def register_controller(project_id: UUID, body: dict = None, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(auth)):
    if principal.kind != "user":
        check_scope(principal, "controller:write")
    key = required_key(idempotency_key)
    body = body or {}
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, body)
        if replay:
            return prior
        current = conn.execute("SELECT c.agent_identity_id,ai.principal_id,c.policy FROM controllers c LEFT JOIN agent_identities ai ON ai.id=c.agent_identity_id WHERE c.project_id=%s FOR UPDATE OF c", (project_id,)).fetchone()
        # Discovery creates controllers without an agent owner; any agent may claim one of those once.
        if current and principal.kind != "user" and current["agent_identity_id"] is not None and str(current["principal_id"] or "") != principal.id:
            raise HTTPException(403, "Only the current controller or a user can replace the controller")
        identity_id = None
        if principal.kind == "agent":
            controller_policy = dict(body.get("policy", {}))
            controller_policy.pop("channel", None)
            controller_policy.pop("t3_webhook_url", None)
            identity = conn.execute("SELECT id FROM agent_identities WHERE principal_id=%s", (principal.id,)).fetchone()
            if identity:
                identity_id = identity["id"]
            else:
                identity = conn.execute("INSERT INTO agent_identities(principal_id,name,agent_type) VALUES(%s,%s,'mcp_agent') RETURNING id", (principal.id, principal.name)).fetchone()
                identity_id = identity["id"]
            if current:
                controller_policy = {**(current["policy"] or {}), **controller_policy}
        else:
            controller_policy = dict(body.get("policy", {}))
            if controller_policy.get("channel") not in {None, "t3_webhook", "mac_alert", "test_sink"}:
                raise HTTPException(422, "Unsupported notification channel")
            if controller_policy.get("channel") == "t3_webhook" and not valid_webhook_url(controller_policy.get("t3_webhook_url")):
                raise HTTPException(422, "T3 channel requires an HTTPS webhook URL")
        row = conn.execute("INSERT INTO controllers(project_id,agent_identity_id,policy) VALUES(%s,%s,%s) ON CONFLICT(project_id) DO UPDATE SET agent_identity_id=COALESCE(EXCLUDED.agent_identity_id,controllers.agent_identity_id),policy=EXCLUDED.policy,updated_at=now() WHERE EXCLUDED.agent_identity_id IS NULL OR controllers.agent_identity_id IS NULL OR controllers.agent_identity_id=EXCLUDED.agent_identity_id RETURNING id", (project_id, identity_id, Jsonb(controller_policy))).fetchone()
        if row is None:
            # Another agent claimed the controller between the check above and this write.
            raise HTTPException(403, "Only the current controller or a user can replace the controller")
        result = {"controller_id": str(row["id"])}
        save_idempotent(conn, principal, key, result)
        return result


@app.post("/api/v1/projects/{project_id}/threads")
def register_thread(project_id: UUID, body: dict, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("thread:write"))):
    key = required_key(idempotency_key)
    if not all(isinstance(body.get(field), str) and body[field] for field in ("source", "external_thread_id")):
        raise HTTPException(422, "source and external_thread_id are required strings")
    if body.get("state", "unknown") not in {"running", "idle", "blocked", "stopped", "unknown"}:
        raise HTTPException(422, "Unsupported thread state")
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, body)
        if replay:
            return prior
        controller = conn.execute("SELECT id FROM controllers WHERE project_id=%s", (project_id,)).fetchone()
        if not controller:
            raise HTTPException(404, "Controller not found")
        thread = conn.execute("INSERT INTO parent_threads(project_id,controller_id,source,external_thread_id,state) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(source,external_thread_id) DO UPDATE SET state=EXCLUDED.state RETURNING id", (project_id, controller["id"], body["source"], body["external_thread_id"], body.get("state", "unknown"))).fetchone()
        result = {"parent_thread_id": str(thread["id"]), "controller_id": str(controller["id"])}
        save_idempotent(conn, principal, key, result)
        return result


@app.post("/api/v1/projects/{project_id}/tasks")
def create_task(project_id: UUID, body: TaskIn, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("task:write"))):
    key = required_key(idempotency_key)
    payload = body.model_dump()
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, payload)
        if replay:
            return prior
        row = conn.execute("INSERT INTO tasks(project_id,title,description,acceptance,thresholds) VALUES(%s,%s,%s,%s,%s) RETURNING *", (project_id, body.title, body.description, Jsonb(body.acceptance), Jsonb(body.thresholds))).fetchone()
        result = {"task": row_json(row)}
        save_idempotent(conn, principal, key, result)
        return result


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: UUID, principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=%s", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        events = conn.execute("SELECT * FROM events WHERE task_id=%s ORDER BY observed_at", (task_id,)).fetchall()
    return {"task": row_json(task), "events": [row_json(e) for e in events]}


@app.post("/api/v1/events")
def ingest_event(body: ProgressIn, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("event:write"))):
    key = required_key(idempotency_key)
    payload = body.model_dump(mode="json")
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, payload)
        if replay:
            return prior
        project_id = body.project_id
        confidence = "high" if body.source == "mcp" else (body.confidence if body.confidence in {"low", "medium", "unknown"} else "unknown")
        thread_id = body.parent_thread_id
        exact_thread_mapping = False
        owner_thread_binding = False
        if body.task_id:
            linked_task = conn.execute("SELECT project_id,parent_thread_id FROM tasks WHERE id=%s", (body.task_id,)).fetchone()
            if linked_task:
                project_id = project_id or linked_task["project_id"]
                thread_id = thread_id or linked_task["parent_thread_id"]
        if body.source != "mcp" and body.external_thread_id:
            thread = conn.execute("SELECT id,project_id FROM parent_threads WHERE source=%s AND external_thread_id=%s", (body.source, body.external_thread_id)).fetchone()
            if thread:
                if body.project_id and body.project_id != thread["project_id"]:
                    raise HTTPException(422, "Project conflicts with the exact parent thread mapping")
                if project_id and project_id != thread["project_id"]:
                    raise HTTPException(422, "Task conflicts with the exact parent thread mapping")
                thread_id = thread["id"]
                project_id = thread["project_id"]
                exact_thread_mapping = True
                owner_mapping = conn.execute("""SELECT 1 FROM project_sources
                    WHERE source=%s AND project_id=%s AND confidence='high' AND (
                      (match_kind='external_thread_id' AND match_value=%s) OR
                      (match_kind='working_directory' AND lower(match_value)=lower(%s))) LIMIT 1""",
                    (body.source, project_id, body.external_thread_id, body.working_directory or "")).fetchone()
                confidence = "low" if body.confidence == "low" else ("high" if owner_mapping else "medium")
            else:
                mapping = conn.execute("SELECT project_id,confidence FROM project_sources WHERE source=%s AND match_kind='external_thread_id' AND match_value=%s", (body.source, body.external_thread_id)).fetchone()
                if mapping:
                    project_id = mapping["project_id"]
                    confidence = "low" if body.confidence == "low" else mapping["confidence"]
                    owner_thread_binding = mapping["confidence"] == "high"
        if body.source != "mcp" and body.working_directory and not exact_thread_mapping:
            mapping = conn.execute("SELECT project_id,confidence FROM project_sources WHERE source=%s AND match_kind='working_directory' AND lower(match_value)=lower(%s)", (body.source, body.working_directory)).fetchall()
            auto_mapping = None
            if len(mapping) == 1:
                if mapping[0]["confidence"] == "medium":
                    auto_mapping = mapping[0]
                elif not thread_id or not project_id or mapping[0]["project_id"] == project_id:
                    project_id = mapping[0]["project_id"]
                    confidence = "low" if body.confidence == "low" else mapping[0]["confidence"]
            explicit_project = body.project_id is not None or body.task_id is not None
            if project_id is None or (auto_mapping is not None and not explicit_project and not owner_thread_binding):
                roots = conn.execute("SELECT path,display_name AS name,origin FROM project_roots").fetchall()
                root = match_project_root(body.working_directory, roots)
                if root and not excluded_root(body.working_directory):
                    project_id = root["project_id"] if "project_id" in root else conn.execute("SELECT project_id FROM project_roots WHERE path=%s", (root["path"],)).fetchone()["project_id"]
                    confidence = "medium"
                    conn.execute("""INSERT INTO project_sources(project_id,source,match_kind,match_value,confidence)
                        VALUES(%s,%s,'working_directory',%s,'medium')
                        ON CONFLICT(source,match_kind,match_value) DO UPDATE SET project_id=EXCLUDED.project_id
                        WHERE project_sources.confidence='medium'""", (project_id, body.source, normalize_root(body.working_directory)))
                    thread_id = ensure_auto_thread(conn, project_id, body.source, body.external_thread_id)
                elif auto_mapping is not None and project_id is None:
                    project_id = auto_mapping["project_id"]
                    confidence = "low" if body.confidence == "low" else "medium"
        if body.source != "mcp" and body.working_directory and thread_id is None and project_id is not None and confidence == "high":
            source_threads = conn.execute("SELECT id FROM parent_threads WHERE project_id=%s AND source=%s", (project_id, body.source)).fetchall()
            if len(source_threads) == 1:
                thread_id = source_threads[0]["id"]
        if body.source != "mcp" and (project_id is None or confidence == "unknown"):
            conn.execute("INSERT INTO unmapped_observations(source,external_thread_id,working_directory,source_event_id,observed_at,details,confidence) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING", (body.source, body.external_thread_id, body.working_directory, body.source_event_id, body.observed_at, Jsonb(body.details), confidence))
            result = {"event_id": None, "mapped": False, "confidence": "unknown", "received_at": datetime.now(timezone.utc).isoformat()}
        else:
            if project_id is None:
                raise HTTPException(422, "project_id is required for MCP reports")
            if thread_id:
                thread_project = conn.execute("SELECT project_id FROM parent_threads WHERE id=%s", (thread_id,)).fetchone()
                if thread_project and thread_project["project_id"] != project_id:
                    raise HTTPException(422, "Event project conflicts with its parent thread")
            if body.source != "mcp" and body.external_thread_id and thread_id is None and confidence == "medium":
                thread_id = ensure_auto_thread(conn, project_id, body.source, body.external_thread_id)
            event_details = {**body.details, "summary": body.summary}
            if body.working_directory:
                event_details["working_directory"] = body.working_directory
            row = conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,actor_principal_id,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING RETURNING id,received_at", (project_id, body.task_id, thread_id, body.event_type, body.source, confidence, body.source_event_id, body.observed_at, principal.id, Jsonb(event_details))).fetchone()
            if row is None:
                row = conn.execute("SELECT id,received_at FROM events WHERE source=%s AND source_event_id=%s", (body.source, body.source_event_id)).fetchone()
            if row is not None:
                heartbeat = body.source != "mcp"
                if thread_id:
                    conn.execute("""UPDATE parent_threads SET
                        last_agent_event_at=GREATEST(COALESCE(last_agent_event_at,%s),%s),
                        last_process_heartbeat_at=CASE WHEN %s THEN GREATEST(COALESCE(last_process_heartbeat_at,now()),now()) ELSE last_process_heartbeat_at END,
                        last_meaningful_progress_at=CASE WHEN %s THEN GREATEST(COALESCE(last_meaningful_progress_at,%s),%s) ELSE last_meaningful_progress_at END,
                        last_milestone_at=CASE WHEN %s THEN GREATEST(COALESCE(last_milestone_at,%s),%s) ELSE last_milestone_at END,
                        last_user_interaction_at=CASE WHEN %s THEN GREATEST(COALESCE(last_user_interaction_at,%s),%s) ELSE last_user_interaction_at END
                        WHERE id=%s""",
                        (body.observed_at,body.observed_at,heartbeat,body.meaningful,body.observed_at,body.observed_at,
                         body.event_type in {"milestone", "milestone_complete"},body.observed_at,body.observed_at,
                         body.event_type in {"user_interaction", "user_message"},body.observed_at,body.observed_at,thread_id))
                if body.task_id:
                    conn.execute("""UPDATE tasks SET parent_thread_id=COALESCE(parent_thread_id,%s),
                        status=CASE WHEN %s AND %s::text IS NOT NULL THEN %s ELSE status END,
                        last_agent_event_at=GREATEST(COALESCE(last_agent_event_at,%s),%s),
                        last_process_heartbeat_at=CASE WHEN %s THEN GREATEST(COALESCE(last_process_heartbeat_at,now()),now()) ELSE last_process_heartbeat_at END,
                        last_meaningful_progress_at=CASE WHEN %s THEN GREATEST(COALESCE(last_meaningful_progress_at,%s),%s) ELSE last_meaningful_progress_at END,
                        last_milestone_at=CASE WHEN %s THEN GREATEST(COALESCE(last_milestone_at,%s),%s) ELSE last_milestone_at END,
                        last_user_interaction_at=CASE WHEN %s THEN GREATEST(COALESCE(last_user_interaction_at,%s),%s) ELSE last_user_interaction_at END,
                        updated_at=now() WHERE id=%s""",
                        (thread_id,body.meaningful,body.status,body.status,body.observed_at,body.observed_at,heartbeat,
                         body.meaningful,body.observed_at,body.observed_at,body.event_type in {"milestone", "milestone_complete"},body.observed_at,body.observed_at,
                         body.event_type in {"user_interaction", "user_message"},body.observed_at,body.observed_at,body.task_id))
                if body.task_id and body.meaningful:
                    conn.execute("UPDATE projects SET last_meaningful_progress_at=GREATEST(COALESCE(last_meaningful_progress_at,%s),%s),updated_at=now() WHERE id=%s", (body.observed_at,body.observed_at,project_id))
                    pending = conn.execute("""UPDATE incidents i SET state='resolved',resumed_progress_at=%s,resolved_at=now()
                        WHERE i.task_id=%s AND i.state IN ('open','notifying') AND i.acknowledged_at IS NULL
                          AND %s>i.opened_at
                          AND %s>COALESCE((i.details->>'stall_evidence_at')::timestamptz,'-infinity'::timestamptz)
                          AND EXISTS (SELECT 1 FROM notification_outbox n WHERE n.incident_id=i.id AND n.state='pending' AND n.delivered_at IS NULL)
                        RETURNING i.id,i.project_id,i.parent_thread_id""", (body.observed_at,body.task_id,body.observed_at,body.observed_at)).fetchall()
                    for incident in pending:
                        conn.execute("UPDATE notification_outbox SET state='failed',last_error='resolved before notification delivery' WHERE incident_id=%s AND state='pending' AND delivered_at IS NULL", (incident["id"],))
                        conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,'incident_resolved_before_delivery',%s,'high',%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING", (incident["project_id"],body.task_id,incident["parent_thread_id"],body.source,f"resolved-before-delivery:{incident['id']}",body.observed_at,Jsonb({"incident_id":str(incident["id"]),"reason":"meaningful progress arrived before notification delivery"})))
            if conn.execute("SELECT 1 FROM incidents WHERE task_id=%s AND state='recovering' AND acknowledged_at IS NOT NULL", (body.task_id,)).fetchone():
                conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,actor_principal_id,details) VALUES(%s,%s,%s,'progress_resumed',%s,'high',%s,%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING", (project_id, body.task_id, thread_id, body.source, f"resume:{body.source_event_id}", body.observed_at, principal.id, Jsonb({"summary":"Meaningful progress resumed after acknowledgement"})))
            result = {"event_id": str(row["id"]), "mapped": True, "confidence": confidence, "received_at": row["received_at"].isoformat()}
        save_idempotent(conn, principal, key, result)
        return result


@app.post("/api/v1/tasks/{task_id}/review")
def review_task(task_id: UUID, body: dict, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("task:review", user_only=True))):
    key = required_key(idempotency_key)
    if body.get("decision") not in {"verified", "rejected"} or not isinstance(body.get("evidence"), dict):
        raise HTTPException(422, "decision and evidence are required")
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, body)
        if replay:
            return prior
        verification = conn.execute("INSERT INTO task_verifications(task_id,reviewer_principal_id,decision,evidence) VALUES(%s,%s,%s,%s) RETURNING id", (task_id, principal.id, body["decision"], Jsonb(body["evidence"]))).fetchone()
        status = "verified" if body["decision"] == "verified" else "needs_review"
        conn.execute("UPDATE tasks SET status=%s,verification=%s,updated_at=now() WHERE id=%s", (status, Jsonb(body["evidence"]), task_id))
        result = {"verification_id": str(verification["id"]), "task_status": status}
        save_idempotent(conn, principal, key, result)
        return result


@app.get("/api/v1/projects/{project_id}/events")
def project_events(project_id: UUID, limit: int | None = None, principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        if limit is None:
            rows = conn.execute("SELECT * FROM events WHERE project_id=%s ORDER BY observed_at,id", (project_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM (SELECT * FROM events WHERE project_id=%s ORDER BY observed_at DESC,id DESC LIMIT %s) e ORDER BY observed_at DESC,id DESC", (project_id, max(1, min(limit, 500)))).fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.get("/api/v1/projects/{project_id}/milestones")
def list_milestones(project_id: UUID, principal: Principal = Depends(require("project:read"))):
    with connection() as conn:
        rows = conn.execute("SELECT * FROM milestones WHERE project_id=%s ORDER BY due_at NULLS LAST", (project_id,)).fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.get("/api/v1/incidents")
def list_incidents(principal: Principal = Depends(require("incident:read"))):
    with connection() as conn:
        rows = conn.execute("""SELECT i.*,p.name AS project_name,t.title AS task_title,
          (SELECT max(o.delivered_at) FROM notification_outbox o WHERE o.incident_id=i.id) AS delivered_at,
          CASE WHEN EXISTS(SELECT 1 FROM notification_outbox o WHERE o.incident_id=i.id AND o.channel='t3_mcp'
            AND o.state<>'delivered' AND (o.last_error IS NOT NULL OR o.state='failed'))
            THEN 'kick not delivered' END AS kick_delivery_status
          FROM incidents i JOIN projects p ON p.id=i.project_id LEFT JOIN tasks t ON t.id=i.task_id ORDER BY opened_at DESC""").fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.get("/api/v1/t3-mcp/pending")
def pending_t3_mcp(principal: Principal = Depends(require("mac_alert:deliver"))):
    with connection() as conn:
        rows = conn.execute("""SELECT n.id,n.incident_id,n.recipient,n.payload,t.status AS child_status
          FROM notification_outbox n JOIN incidents i ON i.id=n.incident_id JOIN tasks t ON t.id=i.task_id
          WHERE n.channel='t3_mcp' AND n.state='pending' AND COALESCE(n.next_attempt_at,now())<=now()
            AND t.derived_source='t3_subagent' AND t.status='running'
          ORDER BY n.created_at LIMIT 50""").fetchall()
    return {"items": [{"id": str(row["id"]), "incident_id": str(row["incident_id"]),
                        "recipient": row["recipient"], "payload": row["payload"],
                        "child_status": row["child_status"]} for row in rows]}


@app.post("/api/v1/t3-mcp/{outbox_id}/delivered")
def report_t3_mcp_delivered(outbox_id: UUID, principal: Principal = Depends(require("mac_alert:deliver"))):
    with connection() as conn:
        row = conn.execute("""UPDATE notification_outbox SET state='delivered',delivered_at=COALESCE(delivered_at,now()),
            attempt_count=attempt_count+1,last_error=NULL WHERE id=%s AND channel='t3_mcp' AND state='pending'
            RETURNING incident_id,delivered_at,payload""", (outbox_id,)).fetchone()
        if not row:
            row = conn.execute("SELECT incident_id,delivered_at,payload FROM notification_outbox WHERE id=%s AND channel='t3_mcp' AND state='delivered'", (outbox_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Pending T3 MCP kick not found")
        if (row["payload"] or {}).get("kind") == "first":
            conn.execute("UPDATE incidents SET state='awaiting_ack' WHERE id=%s AND state IN ('open','notifying')", (row["incident_id"],))
        conn.execute("""INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details)
          SELECT project_id,task_id,parent_thread_id,'notification_delivered','mac_collector','high',%s,%s,%s
          FROM incidents WHERE id=%s ON CONFLICT(source,source_event_id) DO NOTHING""",
          (f"t3-mcp-delivered:{outbox_id}", row["delivered_at"], Jsonb({"outbox_id": str(outbox_id), "incident_id": str(row["incident_id"])}), row["incident_id"]))
    return {"outbox_id": str(outbox_id), "delivered_at": row["delivered_at"].isoformat()}


@app.post("/api/v1/t3-mcp/{outbox_id}/failed")
def report_t3_mcp_failed(outbox_id: UUID, body: T3McpFailureIn, principal: Principal = Depends(require("mac_alert:deliver"))):
    error = {"send_failed": "T3 MCP send failed", "malformed_thread_id": "Malformed T3 thread ID", "child_not_working": "Child is no longer working"}[body.reason]
    with connection() as conn:
        row = conn.execute("""UPDATE notification_outbox SET state=%s,attempt_count=attempt_count+1,
            next_attempt_at=CASE WHEN %s THEN now()+interval '2 seconds' ELSE NULL END,last_error=%s
            WHERE id=%s AND channel='t3_mcp' AND state='pending' RETURNING id,state""",
            ("pending" if body.retryable else "failed", body.retryable, error, outbox_id)).fetchone()
        if not row:
            existing = conn.execute("SELECT state FROM notification_outbox WHERE id=%s AND channel='t3_mcp'", (outbox_id,)).fetchone()
            if not existing:
                raise HTTPException(404, "T3 MCP kick not found")
            return {"outbox_id": str(outbox_id), "state": existing["state"]}
    return {"outbox_id": str(outbox_id), "state": row["state"]}


@app.get("/api/v1/t3-mcp/status")
def get_t3_mcp_status(principal: Principal = Depends(require("incident:read"))):
    with connection() as conn:
        row = conn.execute("SELECT token_expires_at,updated_at FROM t3_mcp_connection WHERE singleton=true").fetchone()
    return {"expires_at": row["token_expires_at"].isoformat() if row else None,
            "updated_at": row["updated_at"].isoformat() if row else None}


@app.post("/api/v1/t3-mcp/status")
def update_t3_mcp_status(body: T3McpStatusIn, principal: Principal = Depends(require("mac_alert:deliver"))):
    with connection() as conn:
        conn.execute("""INSERT INTO t3_mcp_connection(singleton,token_expires_at) VALUES(true,%s)
          ON CONFLICT(singleton) DO UPDATE SET token_expires_at=EXCLUDED.token_expires_at,updated_at=now()""", (body.expires_at,))
    return {"expires_at": body.expires_at.isoformat()}


@app.get("/api/v1/mac-alerts/pending")
def pending_mac_alerts(principal: Principal = Depends(require("mac_alert:deliver"))):
    with connection() as conn:
        rows = conn.execute("""
          SELECT n.id,n.incident_id,n.payload,i.state AS incident_state
          FROM notification_outbox n JOIN incidents i ON i.id=n.incident_id
          WHERE n.channel='mac_alert' AND n.state='pending'
          ORDER BY n.created_at DESC LIMIT 50
        """).fetchall()
    return {"items": [{"id": str(row["id"]), "incident_id": str(row["incident_id"]), "payload": row["payload"], "incident_state": row["incident_state"]} for row in rows]}


@app.post("/api/v1/mac-alerts/{outbox_id}/delivered")
def report_mac_alert_delivered(outbox_id: UUID, principal: Principal = Depends(require("mac_alert:deliver"))):
    with connection() as conn:
        row = conn.execute("""
          UPDATE notification_outbox SET state='delivered',delivered_at=COALESCE(delivered_at,now()),attempt_count=attempt_count+1
          WHERE id=%s AND channel='mac_alert' AND state='pending'
          RETURNING incident_id,delivered_at
        """, (outbox_id,)).fetchone()
        if not row:
            prior = conn.execute("SELECT incident_id,delivered_at FROM notification_outbox WHERE id=%s AND channel='mac_alert' AND state='delivered'", (outbox_id,)).fetchone()
            if not prior:
                raise HTTPException(404, "Pending Mac alert not found")
            row = prior
        conn.execute("UPDATE incidents SET state='awaiting_ack' WHERE id=%s AND state='open'", (row["incident_id"],))
        conn.execute("""
          INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details)
          SELECT project_id,task_id,parent_thread_id,'notification_delivered','mac_collector','high',%s,%s,%s
          FROM incidents WHERE id=%s ON CONFLICT(source,source_event_id) DO NOTHING
        """, (f"mac-alert-delivered:{outbox_id}", row["delivered_at"], Jsonb({"outbox_id": str(outbox_id), "incident_id": str(row["incident_id"])}), row["incident_id"]))
    return {"outbox_id": str(outbox_id), "delivered_at": row["delivered_at"].isoformat()}


@app.get("/api/v1/incidents/{incident_id}")
def get_incident(incident_id: UUID, principal: Principal = Depends(require("incident:read"))):
    with connection() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=%s", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Incident not found")
        notifications = conn.execute("SELECT * FROM notification_outbox WHERE incident_id=%s ORDER BY created_at", (incident_id,)).fetchall()
        events = conn.execute("SELECT * FROM events WHERE project_id=%s AND task_id IS NOT DISTINCT FROM %s ORDER BY observed_at", (row["project_id"], row["task_id"])).fetchall()
    timeline = {"delivered": [n["delivered_at"].isoformat() for n in notifications if n["delivered_at"]], "acknowledged": row["acknowledged_at"].isoformat() if row["acknowledged_at"] else None, "resumed": row["resumed_progress_at"].isoformat() if row["resumed_progress_at"] else None, "events": [row_json(e) for e in events]}
    return {"incident": row_json(row), "timeline": timeline, "notifications": [row_json(n) for n in notifications]}


@app.post("/api/v1/incidents/{incident_id}/ack")
def acknowledge(incident_id: UUID, body: dict | None = None, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(require("incident:ack"))):
    key = required_key(idempotency_key)
    body = body or {}
    with connection() as conn:
        prior, replay, _ = claim_idempotency(conn, principal, key, body)
        if replay:
            return prior
        incident = conn.execute("SELECT i.parent_thread_id,c.agent_identity_id,ai.principal_id,c.policy,t.thresholds FROM incidents i LEFT JOIN parent_threads pt ON pt.id=i.parent_thread_id LEFT JOIN controllers c ON c.id=pt.controller_id LEFT JOIN agent_identities ai ON ai.id=c.agent_identity_id LEFT JOIN tasks t ON t.id=i.task_id WHERE i.id=%s", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404, "Incident not found")
        if principal.kind != "user" and str(incident["principal_id"] or "") != principal.id:
            raise HTTPException(403, "Only the bound controller or a user can acknowledge")
        policy = incident["policy"] or {}
        thresholds = incident["thresholds"] or {}
        progress_seconds = policy.get("progress_deadline_seconds", thresholds.get("meaningful_progress_seconds", 1800))
        if type(progress_seconds) is not int or progress_seconds <= 0:
            progress_seconds = 1800
        row = conn.execute("UPDATE incidents SET acknowledged_at=COALESCE(acknowledged_at,now()),state='recovering',progress_deadline=COALESCE(progress_deadline,now()+(%s * interval '1 second')) WHERE id=%s AND state IN ('notifying','awaiting_ack','recovering') RETURNING acknowledged_at,progress_deadline", (progress_seconds,incident_id)).fetchone()
        if not row:
            raise HTTPException(409, "Incident cannot be acknowledged in its current state")
        conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,actor_principal_id,details) SELECT project_id,task_id,parent_thread_id,'incident_acknowledged','mcp','high',%s,now(),%s,%s FROM incidents WHERE id=%s ON CONFLICT(source,source_event_id) DO NOTHING", (f"ack:{incident_id}:{principal.id}", principal.id, Jsonb({"note":body.get("note", "")}), incident_id))
        result = {"acknowledged_at": row["acknowledged_at"].isoformat(), "progress_deadline": row["progress_deadline"].isoformat()}
        save_idempotent(conn, principal, key, result)
        return result


class ProjectPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    next_action: str | None = Field(default=None, max_length=500)
    status: Literal["active", "paused", "blocked", "complete", "archived"] | None = None
    hidden: bool | None = None


    @field_validator("name", "status")
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise ValueError("must not be null")
        return value


class ProjectMergeIn(BaseModel):
    into: UUID


class TaskPatchIn(BaseModel):
    status: Literal["complete", "cancelled"]


class IncidentCloseIn(BaseModel):
    note: str = Field(default="", max_length=500)


def owner_event(conn, project_id, event_type: str, principal: Principal, details: dict, task_id=None):
    conn.execute(
        "INSERT INTO events(project_id,task_id,event_type,source,confidence,source_event_id,observed_at,actor_principal_id,details) VALUES(%s,%s,%s,'dashboard','high',%s,now(),%s,%s)",
        (project_id, task_id, event_type, f"dashboard:{event_type}:{datetime.now(timezone.utc).isoformat()}:{principal.id}", principal.id, Jsonb(details)),
    )


@app.patch("/api/v1/projects/{project_id}")
def patch_project(project_id: UUID, body: ProjectPatchIn, principal: Principal = Depends(require("project:write", user_only=True))):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "No changes supplied")
    if "next_action" in changes and changes["next_action"] is not None:
        changes["next_action"] = changes["next_action"].strip() or None
    columns = ",".join(f"{name}=%s" for name in changes)
    with connection() as conn:
        row = conn.execute(f"UPDATE projects SET {columns},updated_at=now() WHERE id=%s RETURNING *", (*changes.values(), project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Project not found")
        owner_event(conn, project_id, "project_updated", principal, {"fields": sorted(changes)})
    return {"project": row_json(row)}


@app.post("/api/v1/projects/{project_id}/merge")
def merge_project(project_id: UUID, body: ProjectMergeIn, principal: Principal = Depends(require("project:write", user_only=True))):
    if project_id == body.into:
        raise HTTPException(422, "A project cannot be merged into itself")
    with connection() as conn:
        source = conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,)).fetchone()
        target = conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (body.into,)).fetchone()
        if not source or not target:
            raise HTTPException(404, "Project not found")
        target_controller = conn.execute("SELECT id FROM controllers WHERE project_id=%s", (body.into,)).fetchone()
        source_controller = conn.execute("SELECT id FROM controllers WHERE project_id=%s", (project_id,)).fetchone()
        if source_controller and target_controller:
            conn.execute("UPDATE parent_threads SET controller_id=%s WHERE controller_id=%s", (target_controller["id"], source_controller["id"]))
            conn.execute("DELETE FROM controllers WHERE id=%s", (source_controller["id"],))
        elif source_controller:
            conn.execute("UPDATE controllers SET project_id=%s,updated_at=now() WHERE id=%s", (body.into, source_controller["id"]))
        conn.execute("UPDATE project_roots SET project_id=%s WHERE project_id=%s", (body.into, project_id))
        for row in conn.execute("SELECT id,source,match_kind,match_value FROM project_sources WHERE project_id=%s", (project_id,)).fetchall():
            exists = conn.execute("SELECT 1 FROM project_sources WHERE project_id=%s AND source=%s AND match_kind=%s AND match_value=%s", (body.into, row["source"], row["match_kind"], row["match_value"])).fetchone()
            if exists:
                conn.execute("DELETE FROM project_sources WHERE id=%s", (row["id"],))
            else:
                conn.execute("UPDATE project_sources SET project_id=%s WHERE id=%s", (body.into, row["id"]))
        for table in ("tasks", "milestones", "subagent_registrations", "incidents", "parent_threads"):
            conn.execute(f"UPDATE {table} SET project_id=%s WHERE project_id=%s", (body.into, project_id))
        conn.execute("SELECT lighthouse_merge_project_events(%s,%s)", (project_id, body.into))
        conn.execute("UPDATE projects SET status='archived',updated_at=now() WHERE id=%s", (project_id,))
        owner_event(conn, body.into, "project_merged", principal, {"source_project_id": str(project_id)})
    return {"project_id": str(project_id), "merged_into": str(body.into), "status": "archived"}


@app.patch("/api/v1/tasks/{task_id}")
def patch_task(task_id: UUID, body: TaskPatchIn, principal: Principal = Depends(require("task:write", user_only=True))):
    with connection() as conn:
        row = conn.execute("UPDATE tasks SET status=%s,updated_at=now() WHERE id=%s AND status NOT IN ('verified','cancelled') RETURNING *", (body.status, task_id)).fetchone()
        if not row:
            raise HTTPException(404, "Open task not found")
        owner_event(conn, row["project_id"], "task_closed", principal, {"status": body.status}, task_id=task_id)
    return {"task": row_json(row)}


@app.post("/api/v1/incidents/{incident_id}/close")
def close_incident(incident_id: UUID, body: IncidentCloseIn, principal: Principal = Depends(require("incident:ack", user_only=True))):
    with connection() as conn:
        row = conn.execute("UPDATE incidents SET state='closed',resolved_at=COALESCE(resolved_at,now()) WHERE id=%s AND state NOT IN ('resolved','closed') RETURNING project_id,task_id,resolved_at", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(409, "Incident is already closed")
        conn.execute("UPDATE notification_outbox SET state='failed',last_error='closed by owner' WHERE incident_id=%s AND state='pending'", (incident_id,))
        owner_event(conn, row["project_id"], "incident_closed", principal, {"incident_id": str(incident_id), "note": body.note}, task_id=row["task_id"])
    return {"incident_id": str(incident_id), "state": "closed", "resolved_at": row["resolved_at"].isoformat()}
