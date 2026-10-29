"""A notice T3 refuses becomes a Mac notification; no real T3, API, or notification."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector import collector, t3_mcp

OUTBOX = {"id": "outbox-1", "payload": {"kind": "first", "parent_thread_id": "t3-parent",
          "child_thread_id": "thread:delegated-task:child", "child_title": "Child", "silent_minutes": 30,
          "child_link": "t3code://threads/child"}}
REFUSAL = {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "content": [{"type": "text", "text":
           "runtime_mode_escalation_denied: Child runtime mode full-access is broader than parent mode approval-required."}]}}


class RefusingClient(t3_mcp.T3McpClient):
    """T3's MCP server as it answers a token with less access than the thread."""

    def _post(self, message):
        if message.get("method") == "tools/call" and message["params"]["name"] == "t3_thread_read":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"structuredContent": {"thread": {"status": "running"}}}}
        if message.get("method") == "tools/call":
            return REFUSAL
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}


class T3FallbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="lighthouse-unit-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.token_file = Path(temp.name) / "t3-mcp-token"
        self.token_file.write_text(json.dumps({"access_token": "local-test-token", "expires_at": 2_000_000_000}))
        os.chmod(self.token_file, 0o600)
        self.reports = []

        def api(url, token, payload=None):
            if url.endswith("/pending"):
                return {"items": [OUTBOX]}
            if url.endswith("/status"):
                return {}
            self.reports.append((url.rsplit("/", 1)[-1], payload))
            return {}
        patcher = mock.patch.object(t3_mcp, "_json_request", api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, state, notify):
        return t3_mcp.deliver_pending("http://api", "api-token", state, None, notify,
                                      token_file=self.token_file, client_factory=RefusingClient)

    def test_a_refused_notice_is_shown_as_a_mac_notification_and_not_retried(self):
        shown = []
        with self.assertLogs("lighthouse.collector.t3_mcp", "WARNING") as logs:
            self.assertEqual(self.deliver({}, lambda message: shown.append(message) or "osascript"), 0)
        self.assertEqual(len(shown), 1)
        self.assertIn("“Child” has been marked working but silent for 30 min", shown[0])
        self.assertIn("runtime_mode_escalation_denied", logs.output[0])
        self.assertIn(("failed", {"retryable": False, "reason": "send_failed"}), self.reports)

    def test_a_failed_mac_notification_is_retried_a_bounded_number_of_times(self):
        state = {}

        def broken(message):
            raise RuntimeError("osascript is not available")
        with self.assertLogs("lighthouse.collector.t3_mcp", "WARNING"):
            for _ in range(t3_mcp.FALLBACK_ATTEMPTS):
                self.deliver(state, broken)
        self.assertEqual([payload["retryable"] for name, payload in self.reports],
                         [True] * (t3_mcp.FALLBACK_ATTEMPTS - 1) + [False])
        self.assertEqual(state["t3_mcp_fallback_attempts"], {})

    def test_a_shown_notice_is_not_shown_again_when_its_report_was_lost(self):
        state = {"t3_mcp_fallback_shown": {"outbox-1": True}}
        self.deliver(state, lambda message: self.fail("shown twice"))
        self.assertEqual(self.reports, [("failed", {"retryable": False, "reason": "send_failed"})])
        self.assertEqual(state["t3_mcp_fallback_shown"], {})

    def test_the_collector_passes_a_mac_notifier(self):
        calls = []
        with mock.patch.object(collector.shutil, "which", lambda name: "/bin/terminal-notifier" if name == "terminal-notifier" else None), \
                mock.patch.object(collector.subprocess, "run", lambda command, **kwargs: calls.append(command)):
            self.assertEqual(collector.show_t3_notice("Lighthouse: your subagent"), "terminal-notifier")
        self.assertIn("Lighthouse: your subagent", calls[0])


if __name__ == "__main__":
    unittest.main()
