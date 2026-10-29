"""Deliver derived-stall notices through T3's local MCP server."""
import json
import logging
import os
import re
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

T3_ENDPOINT = os.environ.get("T3_MCP_URL", "http://127.0.0.1:3773/mcp")
T3_TOKEN_FILE = Path(os.environ.get("T3_MCP_TOKEN_FILE", "~/.config/lighthouse/secrets/t3-mcp-token")).expanduser()
THREAD_ID = re.compile(r"^[A-Za-z0-9:%._-]{1,300}$")
MCP_VERSION = "2025-06-18"
# Tries at showing a refused notice as a Mac notification before giving up on it.
FALLBACK_ATTEMPTS = 3

logger = logging.getLogger("lighthouse.collector.t3_mcp")


class T3McpError(RuntimeError):
    pass


def _error_text(result):
    """What T3 said when a tool call failed: the JSON-RPC error, or the tool's error text."""
    if not result:
        return "no answer"
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "error")[:300]
    parts = [str(item.get("text")) for item in (result.get("result") or {}).get("content", [])
             if isinstance(item, dict) and item.get("type") == "text"]
    return " ".join(parts)[:300] or "error"


def valid_thread_id(value):
    return isinstance(value, str) and THREAD_ID.fullmatch(value) is not None


def render_kick(payload):
    kind = payload.get("kind")
    title = str(payload.get("child_title") or "Untitled subagent")[:80]
    minutes = max(0, int(payload.get("silent_minutes") or 0))
    link = str(payload.get("child_link") or payload.get("child_url") or "")
    if kind == "first":
        return f"Lighthouse: your subagent “{title}” has been marked working but silent for {minutes} min. Check on it: read its thread, nudge it, or cancel it. {link}"
    if kind == "second":
        return f"Lighthouse: your subagent “{title}” is still silent ({minutes} min). It needs a decision: nudge it or cancel it. {link}"
    raise ValueError("Unknown T3 MCP kick kind")


def _json_request(url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def _read_token(path):
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("T3 MCP token file must have mode 0600")
    token_data = json.loads(path.read_text(encoding="utf-8"))
    token = token_data.get("access_token")
    expiry = int(token_data["expires_at"])
    if not isinstance(token, str) or not token:
        raise ValueError("T3 MCP access token is missing")
    return token, expiry


class T3McpClient:
    def __init__(self, token, endpoint=T3_ENDPOINT):
        self.token = token
        self.endpoint = endpoint
        self.session_id = None
        self.next_id = 1

    def _post(self, message):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.endpoint, data=json.dumps(message).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
            raw = response.read().decode("utf-8", "replace")
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                raw = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
            for line in raw.splitlines():
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None

    def connect(self):
        result = self._post({
            "jsonrpc": "2.0", "id": self.next_id, "method": "initialize",
            "params": {"protocolVersion": MCP_VERSION, "capabilities": {},
                       "clientInfo": {"name": "Lighthouse collector", "version": "1"}},
        })
        self.next_id += 1
        if not result or "error" in result:
            raise T3McpError("T3 MCP initialize failed")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call_read_tool(self, name, arguments):
        result = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})
        self.next_id += 1
        if not result or "error" in result:
            raise T3McpError("T3 MCP read failed")
        content = result.get("result") or {}
        if content.get("isError"):
            raise T3McpError("T3 MCP read failed")
        structured = content.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in content.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (KeyError, ValueError):
                    pass
        raise T3McpError("T3 MCP read returned no data")

    def child_status(self, child_id):
        result = self.call_read_tool("t3_thread_read", {"threadId": child_id, "view": "messages", "limit": 1, "maxCharsPerItem": 1000})
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
        return thread.get("status")

    def send(self, parent_id, message, request_id):
        result = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call",
                             "params": {"name": "t3_thread_send", "arguments": {
                                 "threadId": parent_id, "message": message, "mode": "auto",
                                 "clientRequestId": request_id,
                             }}})
        self.next_id += 1
        if not result or "error" in result or (result.get("result") or {}).get("isError"):
            raise T3McpError("T3 refused the notice: " + _error_text(result))


def deliver_pending(host, api_token, state, checkpoint=None, notify=None, token_file=T3_TOKEN_FILE,
                    client_factory=T3McpClient):
    """Send pending T3 kicks and report their result. The API token is separate from T3's token.

    When T3 refuses a notice, for example with runtime_mode_escalation_denied because the thread has
    more access than Lighthouse's token, `notify(message, outbox_id)` shows it as a Mac notification instead and
    the notice is not sent to T3 again.
    """
    base = host.rstrip("/") + "/api/v1/t3-mcp"
    try:
        t3_token, expires_at = _read_token(Path(token_file))
    except (OSError, ValueError, KeyError, TypeError, PermissionError):
        t3_token, expires_at = None, 0

    if expires_at and state.get("t3_mcp_expiry_reported") != expires_at:
        try:
            _json_request(base + "/status", api_token, {"expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at))})
            state["t3_mcp_expiry_reported"] = expires_at
            if checkpoint:
                checkpoint()
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
            pass

    try:
        pending = _json_request(base + "/pending", api_token).get("items", [])
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
        return 0
    if not pending:
        return 0

    client = None
    client_attempted = False
    client_error = None
    sent = state.setdefault("t3_mcp_sent", {})
    completed = 0
    for item in pending:
        outbox_id = str(item.get("id") or "")
        if not outbox_id:
            continue
        if outbox_id in state.get("t3_mcp_fallback_shown", {}):
            # Already shown as a Mac notification, or given up on after FALLBACK_ATTEMPTS;
            # only the report to the API was lost.
            _report_fallback(base, api_token, state, outbox_id, False)
            continue
        if outbox_id in sent:
            try:
                _json_request(f"{base}/{outbox_id}/delivered", api_token, {})
                sent.pop(outbox_id, None)
                if checkpoint:
                    checkpoint()
                completed += 1
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
                continue
            continue

        payload = item.get("payload") or {}
        parent_id = payload.get("parent_thread_id")
        child_id = payload.get("child_thread_id")
        if not valid_thread_id(parent_id) or not valid_thread_id(child_id):
            try:
                _json_request(f"{base}/{outbox_id}/failed", api_token, {"retryable": False, "reason": "malformed_thread_id"})
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
                pass
            continue
        if not client_attempted and t3_token and expires_at > int(time.time()):
            client_attempted = True
            try:
                client = client_factory(t3_token)
                client.connect()
            except Exception as error:
                client, client_error = None, error
        if client is None and client_error is not None and notify:
            _fall_back(base, api_token, state, outbox_id, payload, client_error, notify, checkpoint)
            continue
        if client is None:
            try:
                _json_request(f"{base}/{outbox_id}/failed", api_token, {"retryable": True, "reason": "send_failed"})
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
                pass
            continue
        status = None
        try:
            status = client.child_status(child_id)
            if status == "running":
                client.send(parent_id, render_kick(payload), "lighthouse-" + outbox_id)
        except Exception as error:
            if notify:
                _fall_back(base, api_token, state, outbox_id, payload, error, notify, checkpoint)
                continue
            try:
                _json_request(f"{base}/{outbox_id}/failed", api_token, {"retryable": True, "reason": "send_failed"})
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
                pass
            continue
        if status != "running":
            try:
                _json_request(f"{base}/{outbox_id}/failed", api_token, {"retryable": False, "reason": "child_not_working"})
            except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
                pass
            continue

        state.get("t3_mcp_fallback_attempts", {}).pop(outbox_id, None)
        sent[outbox_id] = True
        if checkpoint:
            checkpoint()
        try:
            _json_request(f"{base}/{outbox_id}/delivered", api_token, {})
            sent.pop(outbox_id, None)
            if checkpoint:
                checkpoint()
            completed += 1
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
            continue
    return completed


def _report_fallback(base, api_token, state, outbox_id, retry):
    """Tell the API a notice will not go to T3 (or should be tried again). Returns whether it heard."""
    try:
        _json_request(f"{base}/{outbox_id}/failed", api_token, {"retryable": retry, "reason": "send_failed"})
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
        return False
    state.get("t3_mcp_fallback_shown", {}).pop(outbox_id, None)
    return True


def _fall_back(base, api_token, state, outbox_id, payload, error, notify, checkpoint):
    """Show a notice T3 did not take as a Mac notification, and stop sending it to T3.

    If the Mac notification fails too, the notice is tried again, at most FALLBACK_ATTEMPTS times.
    """
    reason = str(error)[:300] or type(error).__name__
    attempts = state.setdefault("t3_mcp_fallback_attempts", {})
    tries = attempts.pop(outbox_id, 0) + 1
    try:
        method = notify(render_kick(payload), outbox_id)
    except Exception as notify_error:
        retry = tries < FALLBACK_ATTEMPTS
        if retry:
            attempts[outbox_id] = tries
        else:
            # Remember the give-up, so a lost report does not restart the count.
            state.setdefault("t3_mcp_fallback_shown", {})[outbox_id] = "gave_up"
        logger.warning("T3 notice not sent outbox=%s reason=%s; Mac notification failed error=%s%s",
                       outbox_id, reason, type(notify_error).__name__, "" if retry else "; giving up on it")
    else:
        retry = False
        state.setdefault("t3_mcp_fallback_shown", {})[outbox_id] = True
        logger.warning("T3 notice not sent outbox=%s reason=%s; shown as a Mac notification via %s",
                       outbox_id, reason, method)
    if checkpoint:
        checkpoint()
    _report_fallback(base, api_token, state, outbox_id, retry)
    if checkpoint:
        checkpoint()
