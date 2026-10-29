"""T3 connect: OAuth with a pairing code against a local fake T3 server; no real T3 or network."""
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from collector import t3_connect


class FakeT3(BaseHTTPRequestHandler):
    """Answers like T3's MCP OAuth endpoints. `server.answer_state` can break the state check."""

    def log_message(self, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/.well-known/oauth-authorization-server":
            self._json(200, {"registration_endpoint": base + "/oauth/mcp/register",
                             "token_endpoint": base + "/oauth/mcp/token"})
        else:
            self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.server.requests.append((self.path, self.headers["Content-Type"], raw))
        if self.path == "/oauth/mcp/register":
            self._json(200, {"client_id": "client-1"})
        elif self.path == "/oauth/mcp/decision":
            body = json.loads(raw)
            if body["decision"].get("code") != "pair-123":
                self._json(400, {"message": "That pairing code is not valid."})
                return
            auth = body["authorization"]
            state = self.server.answer_state or auth["state"]
            self._json(200, {"redirectTo": auth["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "code-9", "state": state})})
        elif self.path == "/oauth/mcp/token":
            form = urllib.parse.parse_qs(raw)
            self.server.verifier = form["code_verifier"][0]
            self._json(200, {"access_token": "t3-access", "token_type": "Bearer", "expires_in": 2592000, "scope": "orchestration:read"})
        else:
            self._json(404, {})


class T3ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lighthouse-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "secrets" / "t3-mcp-token"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeT3)
        self.server.requests, self.server.answer_state = [], None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def test_connect_saves_a_0600_token_with_its_expiry(self):
        expires_at = t3_connect.connect("pair-123", self.endpoint, self.token_file, now=lambda: 1000)
        self.assertEqual(expires_at, 1000 + 2592000)
        self.assertEqual(json.loads(self.token_file.read_text()), {"access_token": "t3-access", "expires_at": expires_at})
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.token_file.parent.stat().st_mode), 0o700)
        decision = json.loads(next(raw for path, _, raw in self.server.requests if path == "/oauth/mcp/decision"))
        self.assertEqual(decision["decision"], {"_tag": "pairing-code", "access": "approval-required", "code": "pair-123"})
        self.assertEqual(decision["authorization"]["code_challenge_method"], "S256")
        self.assertEqual(decision["authorization"]["resource"], self.endpoint)
        token_request = next(item for item in self.server.requests if item[0] == "/oauth/mcp/token")
        self.assertEqual(token_request[1], "application/x-www-form-urlencoded")
        self.assertTrue(self.server.verifier)

    def test_a_wrong_pairing_code_saves_nothing(self):
        with self.assertRaisesRegex(t3_connect.T3ConnectError, "not valid"):
            t3_connect.connect("wrong", self.endpoint, self.token_file)
        self.assertFalse(self.token_file.exists())

    def test_an_answer_for_another_request_is_refused(self):
        self.server.answer_state = "someone-else"
        with self.assertRaisesRegex(t3_connect.T3ConnectError, "did not match"):
            t3_connect.connect("pair-123", self.endpoint, self.token_file)
        self.assertFalse(self.token_file.exists())

    def test_t3_not_running_is_reported(self):
        with self.assertRaisesRegex(t3_connect.T3ConnectError, "is T3 Code running"):
            t3_connect.connect("pair-123", "http://127.0.0.1:9/mcp", self.token_file)


def fake_cli(sessions=None, pairing='{"credential": "pair-123"}'):
    """A stand-in for `subprocess.run` of the t3 command that records each call."""
    calls = []

    def run(command, **kwargs):
        calls.append(command[1:])
        if command[1:4] == ["auth", "pairing", "create"]:
            return subprocess.CompletedProcess(command, 0, pairing, "")
        if command[1:4] == ["auth", "session", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(sessions or []), "")
        return subprocess.CompletedProcess(command, 0, "", "")
    return run, calls


LIGHTHOUSE_SESSION = {"sessionId": "old-1", "subject": "mcp-client", "client": {"label": "Lighthouse"}}
OTHER_SESSIONS = [{"sessionId": "desktop", "subject": "desktop-bootstrap", "client": {"label": "T3 Code Desktop"}},
                  {"sessionId": "other-agent", "subject": "mcp-client", "client": {"label": "Another agent"}}]


class T3CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lighthouse-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "t3-mcp-token"

    def test_pairing_code_asks_only_for_the_scopes_lighthouse_needs(self):
        run, calls = fake_cli()
        self.assertEqual(t3_connect.pairing_code_from_cli("t3", run), "pair-123")
        command = calls[0]
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--scope"],
                         ["orchestration:read", "orchestration:operate"])
        self.assertIn("5m", command)

    def test_connect_with_cli_revokes_only_earlier_lighthouse_tokens(self):
        run, calls = fake_cli([LIGHTHOUSE_SESSION, *OTHER_SESSIONS])
        used = []
        t3_connect.connect_with_cli("t3", "http://t3/mcp", self.token_file, run,
                                    connector=lambda code, endpoint, token_file: used.append(code) or 42)
        self.assertEqual(used, ["pair-123"])
        self.assertEqual([call for call in calls if call[:3] == ["auth", "session", "revoke"]],
                         [["auth", "session", "revoke", "old-1"]])

    def test_renew_waits_until_three_days_before_expiry_and_retries_hourly(self):
        renewed = []
        renew = lambda cli, token_file: renewed.append(cli) or 99
        t3_connect.write_token(self.token_file, "t", 10 * 86400)
        state = {}
        self.assertIsNone(t3_connect.renew_if_due(state, self.token_file, now=lambda: 0, cli_finder=lambda: "t3", renew=renew))
        self.assertEqual(renewed, [])
        now = 10 * 86400 - 2 * 86400
        self.assertEqual(t3_connect.renew_if_due(state, self.token_file, now=lambda: now, cli_finder=lambda: "t3", renew=renew), 99)
        self.assertIsNone(t3_connect.renew_if_due(state, self.token_file, now=lambda: now + 60, cli_finder=lambda: "t3", renew=renew))
        self.assertEqual(renewed, ["t3"])
        self.assertEqual(t3_connect.renew_if_due(state, self.token_file, now=lambda: now + 3600, cli_finder=lambda: "t3", renew=renew), 99)

    def test_renew_does_nothing_without_a_token_file(self):
        self.assertIsNone(t3_connect.renew_if_due({}, self.token_file, cli_finder=lambda: "t3", renew=lambda *a, **k: 1))

    def test_renew_without_the_t3_command_says_what_to_run(self):
        t3_connect.write_token(self.token_file, "t", 100)
        with self.assertRaisesRegex(t3_connect.T3ConnectError, "lighthouse t3-connect"):
            t3_connect.renew_if_due({}, self.token_file, now=lambda: 0, cli_finder=lambda: None)

    def test_disconnect_revokes_lighthouse_tokens_and_deletes_the_file(self):
        t3_connect.write_token(self.token_file, "t", 100)
        run, calls = fake_cli([LIGHTHOUSE_SESSION, *OTHER_SESSIONS])
        t3_connect.disconnect(self.token_file, cli_finder=lambda: "t3", run=run)
        self.assertFalse(self.token_file.exists())
        self.assertEqual([call for call in calls if call[:3] == ["auth", "session", "revoke"]],
                         [["auth", "session", "revoke", "old-1"]])

    def test_t3_cli_prefers_the_copy_in_dot_t3(self):
        home = Path(self.temp.name)
        cli = home / ".t3" / "bin" / "t3"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\n")
        os.chmod(cli, 0o755)
        self.assertEqual(t3_connect.t3_cli(home), str(cli))


if __name__ == "__main__":
    unittest.main()
