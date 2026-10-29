"""T3 connect: OAuth with a pairing code against a local fake T3 server; no real T3 or network."""
import base64
import contextlib
import io
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock
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
            self._json(200, {"authorization_endpoint": base + "/oauth/mcp/authorize",
                             "registration_endpoint": base + "/oauth/mcp/register",
                             "token_endpoint": base + "/oauth/mcp/token"})
        elif self.path.startswith("/oauth/mcp/authorize?"):
            # Stands in for the user approving on T3's Connect agent page: T3 then sends the browser
            # back to the redirect URI with a code.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self.server.authorize_query = query
            self.send_response(302)
            self.send_header("Location", query["redirect_uri"][0] + "?" + urllib.parse.urlencode(
                {"code": "code-9", "state": query["state"][0]}))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.server.requests.append((self.path, self.headers["Content-Type"], raw))
        if self.path == "/oauth/mcp/register":
            self._json(200, {"client_id": "client-1"})
        elif self.path == "/oauth/mcp/decision":
            if self.server.decision_status:
                self._json(self.server.decision_status, {})
                return
            body = json.loads(raw)
            if body["decision"].get("code") != "pair-123":
                self._json(400, {"message": "That pairing code is not valid."})
                return
            self.server.decided_access = body["decision"]["access"]
            auth = body["authorization"]
            state = self.server.answer_state or auth["state"]
            self._json(200, {"redirectTo": auth["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "code-9", "state": state})})
        elif self.path == "/oauth/mcp/token":
            form = urllib.parse.parse_qs(raw)
            self.server.verifier = form["code_verifier"][0]
            # T3 grants what the approval asked for; for the browser page the test sets `server.grant`.
            grant = self.server.grant or self.server.decided_access or "full-access"
            self._json(200, {"access_token": self.server.access_token, "token_type": "Bearer", "expires_in": 2592000,
                             "scope": " ".join(scopes_for(grant))})
        else:
            self._json(404, {})


class T3ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lighthouse-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "secrets" / "t3-mcp-token"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeT3)
        self.server.requests, self.server.answer_state = [], None
        self.server.access_token, self.server.decision_status = "t3-access", None
        self.server.grant, self.server.decided_access = None, None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def test_connect_saves_a_0600_token_with_its_expiry(self):
        self.server.access_token = t3_token("new-1")
        expires_at = t3_connect.connect("pair-123", self.endpoint, self.token_file, now=lambda: 1000)
        self.assertEqual(expires_at, 1000 + 2592000)
        self.assertEqual(json.loads(self.token_file.read_text()),
                         {"access_token": t3_token("new-1"), "expires_at": expires_at, "session_id": "new-1"})
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.token_file.parent.stat().st_mode), 0o700)
        decision = json.loads(next(raw for path, _, raw in self.server.requests if path == "/oauth/mcp/decision"))
        self.assertEqual(decision["decision"], {"_tag": "pairing-code", "access": "full-access", "code": "pair-123"})
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

    def test_connect_records_the_session_its_token_belongs_to(self):
        self.server.access_token = t3_token("new-1")
        t3_connect.connect("pair-123", self.endpoint, self.token_file)
        self.assertEqual(json.loads(self.token_file.read_text())["session_id"], "new-1")

    def test_reconnect_revokes_only_this_installs_session(self):
        # Another install on this Mac also holds a session labelled Lighthouse; it must keep working.
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        self.server.access_token = t3_token("new-1")
        run, calls = fake_cli([LIGHTHOUSE_SESSION, OTHER_INSTALL_SESSION, *OTHER_SESSIONS])
        t3_connect.connect_with_cli("t3", self.endpoint, self.token_file, run)
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"]])
        self.assertEqual(t3_connect.held_session(self.token_file), "new-1")

    def test_a_failed_automatic_approval_prints_the_browser_step(self):
        self.server.decision_status = 404
        err = io.StringIO()
        with mock.patch.object(t3_connect, "t3_cli", lambda: "t3"), \
                mock.patch.object(t3_connect, "connect_with_cli",
                                  lambda cli: t3_connect.connect("pair-123", self.endpoint, self.token_file)), \
                contextlib.redirect_stderr(err):
            self.assertEqual(t3_connect.main([]), 1)
        self.assertIn("404 at /oauth/mcp/decision", err.getvalue())
        self.assertIn("t3-connect --browser", err.getvalue())
        self.assertIn("Settings, Sharing & access", err.getvalue())
        self.assertFalse(self.token_file.exists())

    def connect_in_browser(self, **kwargs):
        """Runs the browser step against the fake T3, with a browser that follows T3's redirect."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.opened = []

        def browser(url):
            self.opened.append(url)
            threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()

        with mock.patch.object(t3_connect, "REDIRECT_URI", f"http://127.0.0.1:{port}/t3-callback"):
            return t3_connect.connect_in_browser(self.endpoint, self.token_file, opener=browser, wait_seconds=10, **kwargs)

    def test_the_browser_step_saves_the_token_t3_sends_back(self):
        self.server.access_token = t3_token("new-1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            expires_at = self.connect_in_browser(now=lambda: 1000)
        opened = self.opened
        self.assertIn('pick "Full access"', out.getvalue())
        self.assertEqual(expires_at, 1000 + 2592000)
        self.assertTrue(opened[0].startswith(f"http://127.0.0.1:{self.server.server_port}/oauth/mcp/authorize?"))
        self.assertEqual(self.server.authorize_query["code_challenge_method"], ["S256"])
        self.assertEqual(t3_connect.held_session(self.token_file), "new-1")
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)

    def test_a_read_only_grant_from_the_browser_is_rejected_and_the_working_token_kept(self):
        # T3's Connect agent page starts on "Read only"; that token cannot post into a thread.
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100, "old-1")
        self.server.grant, self.server.access_token = "read-only", t3_token("rejected-1", "read-only")
        run, calls = fake_cli()
        with mock.patch.object(t3_connect, "t3_cli", lambda: "t3"), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(t3_connect.T3ConnectError, '"Read only".*cannot send notices.*pick "Full access"'):
            self.connect_in_browser(run=run)
        self.assertEqual(json.loads(self.token_file.read_text())["session_id"], "old-1")
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "rejected-1"]])
        self.assertEqual(t3_connect.pending_revocations(self.token_file), [])

    def test_a_rejected_grant_t3_cannot_revoke_now_is_kept_to_revoke_later(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100, "old-1")
        self.server.grant, self.server.access_token = "read-only", t3_token("rejected-1", "read-only")
        with mock.patch.object(t3_connect, "t3_cli", lambda: None), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(t3_connect.T3ConnectError, "auth session revoke rejected-1"):
            self.connect_in_browser()
        self.assertEqual(t3_connect.held_session(self.token_file), "old-1")
        self.assertEqual(t3_connect.pending_revocations(self.token_file), ["rejected-1"])

    def test_a_full_access_grant_is_accepted(self):
        # T3 refuses a notice to a thread with more access than the token, and new threads start on
        # Full access, so that is the grant Lighthouse keeps without a warning.
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100, "old-1")
        self.server.grant, self.server.access_token = "full-access", t3_token("new-1", "full-access")
        run, calls = fake_cli()
        err = io.StringIO()
        with mock.patch.object(t3_connect, "t3_cli", lambda: "t3"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.connect_in_browser(run=run)
        self.assertEqual(t3_connect.held_session(self.token_file), "new-1")
        self.assertEqual(t3_connect.granted_access({}, t3_token("new-1", "full-access")), "full-access")
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(["auth", "session", "revoke", "new-1"], revokes(calls))

    def test_a_narrower_grant_is_kept_with_a_warning(self):
        self.server.grant, self.server.access_token = "approval-required", t3_token("new-1", "approval-required")
        err = io.StringIO()
        with mock.patch.object(t3_connect, "t3_cli", lambda: "t3"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.connect_in_browser()
        self.assertEqual(t3_connect.held_session(self.token_file), "new-1")
        self.assertIn('"Supervised" access', err.getvalue())
        self.assertIn("Mac notifications", err.getvalue())
        self.assertIn('pick "Full access"', err.getvalue())

    def test_a_renewal_keeps_the_narrower_access_the_user_picked(self):
        t3_connect.write_token(self.token_file, t3_token("old-1", "auto"), 100, "old-1")
        self.server.access_token = t3_token("new-1", "auto")
        run, calls = fake_cli()
        with contextlib.redirect_stderr(io.StringIO()):
            t3_connect.renew_with_cli("t3", self.token_file, run, self.endpoint)
        self.assertEqual(self.server.decided_access, "auto")
        self.assertEqual(scopes_asked(calls), ["orchestration:read", "orchestration:operate"])

    def test_t3_connect_asks_for_full_access_even_after_a_narrower_grant(self):
        t3_connect.write_token(self.token_file, t3_token("old-1", "approval-required"), 100, "old-1")
        self.server.access_token = t3_token("new-1", "full-access")
        run, calls = fake_cli()
        real = t3_connect.connect_with_cli
        with mock.patch.object(t3_connect, "t3_cli", lambda: "t3"), \
                mock.patch.object(t3_connect, "finish_revocations", lambda *args: []), \
                mock.patch.object(t3_connect, "connect_with_cli",
                                  lambda cli: real(cli, self.endpoint, self.token_file, run)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(t3_connect.main([]), 0)
        self.assertEqual(self.server.decided_access, "full-access")
        self.assertEqual(t3_connect.held_session(self.token_file), "new-1")

    def test_the_automatic_route_rejects_a_read_only_grant_too(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100, "old-1")
        self.server.grant, self.server.access_token = "read-only", t3_token("rejected-1", "read-only")
        run, calls = fake_cli()
        with mock.patch.object(t3_connect, "t3_cli", lambda: "t3"), \
                self.assertRaisesRegex(t3_connect.T3ConnectError, "cannot send notices"):
            t3_connect.connect("pair-123", self.endpoint, self.token_file, run=run)
        self.assertEqual(t3_connect.held_session(self.token_file), "old-1")
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "rejected-1"]])

    def test_a_rejected_grant_exits_non_zero(self):
        err = io.StringIO()
        def refuse():
            raise t3_connect.T3GrantError('T3 granted Lighthouse "Read only" access')
        with mock.patch.object(t3_connect, "t3_cli", lambda: None), \
                mock.patch.object(t3_connect, "connect_in_browser", refuse), contextlib.redirect_stderr(err):
            self.assertEqual(t3_connect.main(["--browser"]), 1)
        self.assertIn("Read only", err.getvalue())

    def test_the_grant_is_read_from_the_token_response_when_the_token_has_no_claims(self):
        self.server.grant = "read-only"
        with mock.patch.object(t3_connect, "t3_cli", lambda: None), \
                self.assertRaisesRegex(t3_connect.T3ConnectError, "cannot send notices"):
            t3_connect.connect("pair-123", self.endpoint, self.token_file)
        self.assertFalse(self.token_file.exists())


def scopes_for(access):
    """The scopes T3 gives a session for an access level (mcpClientScopes in T3's server)."""
    if access == "read-only":
        return ["orchestration:read"]
    if access == "full-access":
        return ["orchestration:read", "orchestration:operate", "settings:write", "source-control:write"]
    return ["orchestration:read", "orchestration:operate"]


def scopes_asked(calls):
    """The --scope values of the `t3 auth pairing create` call."""
    command = next(call for call in calls if call[:3] == ["auth", "pairing", "create"])
    return [command[i + 1] for i, part in enumerate(command) if part == "--scope"]


def t3_token(session_id, access="full-access"):
    """A token shaped like T3's: base64url JSON claims with the session ID, scopes, and access
    ceiling (`rtc`, absent for read-only), a dot, a signature."""
    claims = {"v": 1, "kind": "session", "sid": session_id, "scopes": scopes_for(access)}
    if access != "read-only":
        claims["rtc"] = access
    return base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + ".signature"


def fake_cli(sessions=None, pairing='{"credential": "pair-123"}', list_status=0, revoke_status=0):
    """A stand-in for `subprocess.run` of the t3 command that records each call."""
    calls = []

    def run(command, **kwargs):
        calls.append(command[1:])
        if command[1:4] == ["auth", "pairing", "create"]:
            return subprocess.CompletedProcess(command, 0, pairing, "")
        if command[1:4] == ["auth", "session", "list"]:
            return subprocess.CompletedProcess(command, list_status, json.dumps(sessions or []) if list_status == 0 else "", "")
        if command[1:4] == ["auth", "session", "revoke"]:
            return subprocess.CompletedProcess(command, revoke_status, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")
    return run, calls


def revokes(calls):
    return [call for call in calls if call[:3] == ["auth", "session", "revoke"]]


LIGHTHOUSE_SESSION = {"sessionId": "old-1", "subject": "mcp-client", "client": {"label": "Lighthouse"}}
OTHER_INSTALL_SESSION = {"sessionId": "other-install", "subject": "mcp-client", "client": {"label": "Lighthouse"}}
OTHER_SESSIONS = [{"sessionId": "desktop", "subject": "desktop-bootstrap", "client": {"label": "T3 Code Desktop"}},
                  {"sessionId": "other-agent", "subject": "mcp-client", "client": {"label": "Another agent"}}]


# Tokens below are written without a session ID, as earlier versions saved them; Lighthouse reads
# the ID from the token itself.
class T3CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lighthouse-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "t3-mcp-token"

    def test_pairing_code_asks_only_for_the_scopes_full_access_needs(self):
        run, calls = fake_cli()
        self.assertEqual(t3_connect.pairing_code_from_cli("t3", run), "pair-123")
        self.assertEqual(scopes_asked(calls),
                         ["orchestration:read", "orchestration:operate", "settings:write", "source-control:write"])
        self.assertIn("5m", calls[0])

    def test_connect_with_cli_revokes_only_the_session_this_install_held(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        run, calls = fake_cli([LIGHTHOUSE_SESSION, OTHER_INSTALL_SESSION, *OTHER_SESSIONS])
        used = []

        def connector(code, endpoint, token_file):
            used.append(code)
            t3_connect._retire_held_session(token_file, keep="new-1")
            t3_connect.write_token(token_file, t3_token("new-1"), 42, "new-1")
            return 42
        t3_connect.connect_with_cli("t3", "http://t3/mcp", self.token_file, run, connector=connector)
        self.assertEqual(used, ["pair-123"])
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"]])

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

    def test_disconnect_revokes_this_installs_token_and_deletes_the_file(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        run, calls = fake_cli([LIGHTHOUSE_SESSION, *OTHER_SESSIONS])
        self.assertEqual(t3_connect.disconnect(self.token_file, cli_finder=lambda: "t3", run=run), [])
        self.assertFalse(self.token_file.exists())
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"]])
        self.assertFalse(t3_connect.revoke_file(self.token_file).exists())

    def test_disconnect_leaves_another_installs_lighthouse_session_alone(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        run, calls = fake_cli([LIGHTHOUSE_SESSION, OTHER_INSTALL_SESSION])
        t3_connect.disconnect(self.token_file, cli_finder=lambda: "t3", run=run)
        self.assertNotIn(["auth", "session", "revoke", "other-install"], revokes(calls))

    def test_disconnect_does_not_depend_on_listing_sessions(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        run, calls = fake_cli(list_status=1)
        self.assertEqual(t3_connect.disconnect(self.token_file, cli_finder=lambda: "t3", run=run), [])
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"]])

    def test_a_failed_revoke_is_kept_reported_and_retried(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        run, _ = fake_cli([LIGHTHOUSE_SESSION], revoke_status=1)
        self.assertEqual(t3_connect.disconnect(self.token_file, cli_finder=lambda: "t3", run=run), ["old-1"])
        self.assertFalse(self.token_file.exists())
        self.assertEqual(t3_connect.pending_revocations(self.token_file), ["old-1"])
        run, calls = fake_cli([LIGHTHOUSE_SESSION])
        self.assertEqual(t3_connect.disconnect(self.token_file, cli_finder=lambda: "t3", run=run), [])
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"]])
        self.assertEqual(t3_connect.pending_revocations(self.token_file), [])

    def test_without_the_t3_command_disconnect_keeps_the_session_to_revoke(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        self.assertEqual(t3_connect.disconnect(self.token_file, cli_finder=lambda: None), ["old-1"])
        self.assertEqual(t3_connect.pending_revocations(self.token_file), ["old-1"])

    def test_disconnect_reports_incomplete_revocation_with_the_command_that_finishes_it(self):
        err = io.StringIO()
        with mock.patch.object(t3_connect, "disconnect", lambda: ["old-1"]), \
                mock.patch.object(t3_connect, "t3_cli", lambda: "/home/me/.t3/bin/t3"), contextlib.redirect_stderr(err):
            status = t3_connect.main(["--disconnect"])
        self.assertEqual(status, 3)
        self.assertIn("/home/me/.t3/bin/t3 auth session revoke old-1", err.getvalue())
        self.assertIn("Settings, Sharing & access", err.getvalue())

    def test_a_failed_revoke_from_a_renewal_is_retried_on_the_next_connect(self):
        t3_connect.write_token(self.token_file, t3_token("old-1"), 100)
        run, _ = fake_cli(revoke_status=1)

        def connector(code, endpoint, token_file):
            t3_connect._retire_held_session(token_file, keep="new-1")
            t3_connect.write_token(token_file, t3_token("new-1"), 42, "new-1")
            return 42
        t3_connect.connect_with_cli("t3", "http://t3/mcp", self.token_file, run, connector=connector)
        self.assertEqual(t3_connect.pending_revocations(self.token_file), ["old-1"])
        run, calls = fake_cli()

        def connector2(code, endpoint, token_file):
            t3_connect._retire_held_session(token_file, keep="new-2")
            t3_connect.write_token(token_file, t3_token("new-2"), 43, "new-2")
            return 43
        t3_connect.connect_with_cli("t3", "http://t3/mcp", self.token_file, run, connector=connector2)
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"], ["auth", "session", "revoke", "new-1"]])
        self.assertEqual(t3_connect.pending_revocations(self.token_file), [])

    def test_renew_retries_a_failed_revoke_hourly_between_renewals(self):
        t3_connect.write_token(self.token_file, t3_token("new-1"), 10 * 86400, "new-1")
        t3_connect._save_pending(self.token_file, ["old-1"])
        run, calls = fake_cli(revoke_status=1)
        state = {}
        with self.assertRaisesRegex(t3_connect.T3ConnectError, "t3 auth session revoke old-1"):
            t3_connect.renew_if_due(state, self.token_file, now=lambda: 0, cli_finder=lambda: "t3", run=run)
        self.assertIsNone(t3_connect.renew_if_due(state, self.token_file, now=lambda: 60, cli_finder=lambda: "t3", run=run))
        self.assertEqual(len(revokes(calls)), 1)
        run, calls = fake_cli()
        self.assertIsNone(t3_connect.renew_if_due(state, self.token_file, now=lambda: 3600, cli_finder=lambda: "t3", run=run))
        self.assertEqual(revokes(calls), [["auth", "session", "revoke", "old-1"]])
        self.assertEqual(t3_connect.pending_revocations(self.token_file), [])

    def test_t3_cli_prefers_the_copy_in_dot_t3(self):
        home = Path(self.temp.name)
        cli = home / ".t3" / "bin" / "t3"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\n")
        os.chmod(cli, 0o755)
        self.assertEqual(t3_connect.t3_cli(home), str(cli))


if __name__ == "__main__":
    unittest.main()
