import os

import httpx
from fastmcp import Context, FastMCP

mcp = FastMCP("Lighthouse")
API_URL = os.getenv("LIGHTHOUSE_API_URL", "http://api:8000")


def api_call(ctx: Context, path: str, method: str, payload: dict | None = None, idempotency_key: str | None = None):
    request = ctx.get_http_request()
    headers_in = request.headers
    authorization = headers_in.get("authorization") if headers_in else None
    if not authorization or not authorization.startswith("Bearer "):
        raise PermissionError("Bearer token required")
    headers = {"Authorization": authorization}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    response = httpx.request(method, API_URL + path, headers=headers, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


@mcp.tool()
def list_projects(ctx: Context) -> dict:
    """List active projects."""
    return api_call(ctx, "/api/v1/projects", "GET")


@mcp.tool()
def get_project_brief(ctx: Context, project_id: str) -> dict | None:
    return api_call(ctx, f"/api/v1/projects/{project_id}", "GET")["project"].get("brief")


@mcp.tool()
def create_task(ctx: Context, project_id: str, title: str, description: str = "", acceptance: dict | None = None, thresholds: dict | None = None, idempotency_key: str = "") -> dict:
    return api_call(ctx, f"/api/v1/projects/{project_id}/tasks", "POST", {"title": title, "description": description, "acceptance": acceptance or {}, "thresholds": thresholds or {}}, idempotency_key)


@mcp.tool()
def report_progress(ctx: Context, project_id: str, summary: str, source_event_id: str, observed_at: str, meaningful: bool, status: str = "running", task_id: str | None = None, parent_thread_id: str | None = None, idempotency_key: str = "") -> dict:
    return api_call(ctx, "/api/v1/events", "POST", {"project_id": project_id, "task_id": task_id, "parent_thread_id": parent_thread_id, "source": "mcp", "confidence": "high", "event_type": "progress", "status": status, "summary": summary, "meaningful": meaningful, "source_event_id": source_event_id, "observed_at": observed_at, "details": {}}, idempotency_key)


@mcp.tool()
def register_controller(ctx: Context, project_id: str, idempotency_key: str, policy: dict | None = None) -> dict:
    return api_call(ctx, f"/api/v1/projects/{project_id}/controllers", "POST", {"policy": policy or {}}, idempotency_key)


@mcp.tool()
def register_parent_thread(ctx: Context, project_id: str, source: str, external_thread_id: str, idempotency_key: str, state: str = "unknown") -> dict:
    return api_call(ctx, f"/api/v1/projects/{project_id}/threads", "POST", {"source": source, "external_thread_id": external_thread_id, "state": state}, idempotency_key)


@mcp.tool()
def acknowledge_incident(ctx: Context, incident_id: str, idempotency_key: str, note: str = "") -> dict:
    return api_call(ctx, f"/api/v1/incidents/{incident_id}/ack", "POST", {"note": note}, idempotency_key)


@mcp.tool()
def review_task(ctx: Context, task_id: str, decision: str, evidence: dict, idempotency_key: str) -> dict:
    """Owner only: record a review decision for a task. Needs the owner token; agent tokens get 403."""
    return api_call(ctx, f"/api/v1/tasks/{task_id}/review", "POST", {"decision": decision, "evidence": evidence}, idempotency_key)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), path="/mcp")
