#!/usr/bin/env python3
"""Connect Lighthouse to T3 Code, so stall notices can go into T3 threads.

T3 Code's MCP server takes OAuth tokens (see /.well-known/oauth-authorization-server on the
T3 server). This script registers Lighthouse as a client, approves it with a T3 pairing code,
the same code T3's "Connect agent" page asks for, and saves the token for the collector.
The pairing code comes from `t3 auth pairing create` when the t3 command is on this Mac,
otherwise from the user (T3: Settings, Sharing & access). It is used once and never stored.

T3 issues no refresh tokens, so the collector runs this again before the token expires.

Each token is one T3 session. Lighthouse keeps the ID of the session it holds, and revokes only
that session when it renews or disconnects; another install's sessions, even ones labelled
Lighthouse, are left alone. A revocation that fails is kept in a file next to the token and tried
again on the next connect, renewal, or disconnect.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

T3_ENDPOINT = os.environ.get("T3_MCP_URL", "http://127.0.0.1:3773/mcp")
T3_TOKEN_FILE = Path(os.environ.get("T3_MCP_TOKEN_FILE", "~/.config/lighthouse/secrets/t3-mcp-token")).expanduser()
SCOPES = ("orchestration:read", "orchestration:operate")
# The least access that still lets Lighthouse post into a thread: T3 caps any turn a notice
# starts at "approval required".
ACCESS = "approval-required"
CLIENT_NAME = "Lighthouse"
# Loopback address T3 redirects to with the code. Nothing listens there: the code is read from
# the redirect T3 returns.
REDIRECT_URI = "http://127.0.0.1:51829/t3-callback"
RENEW_SECONDS = 3 * 86400
RETRY_SECONDS = 3600
BROWSER_WAIT_SECONDS = 300
# How the installer names the manage command, for the steps this script prints.
MANAGE_COMMAND = os.environ.get("LIGHTHOUSE_COMMAND", "lighthouse")
# Exit status of --disconnect when T3 may still accept a token Lighthouse held.
EXIT_REVOKE_INCOMPLETE = 3


class T3ConnectError(RuntimeError):
    pass


class T3ApprovalError(T3ConnectError):
    """T3 refused or failed the automatic approval call."""


def t3_cli(home=None):
    """The t3 command: the copy T3 installs in ~/.t3/bin, else one on PATH."""
    home = Path(home or Path.home())
    candidate = home / ".t3" / "bin" / "t3"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("t3")


def pairing_code_from_cli(cli, run=subprocess.run):
    """A one-time pairing code from T3's own CLI, limited to the scopes Lighthouse needs."""
    command = [cli, "auth", "pairing", "create", "--ttl", "5m", "--label", CLIENT_NAME, "--json"]
    for scope in SCOPES:
        command += ["--scope", scope]
    result = run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise T3ConnectError("`t3 auth pairing create` failed; is T3 Code running?")
    try:
        code = json.loads(result.stdout)["credential"]
    except (ValueError, KeyError, TypeError):
        raise T3ConnectError("`t3 auth pairing create` returned no pairing code") from None
    if not isinstance(code, str) or not code:
        raise T3ConnectError("`t3 auth pairing create` returned no pairing code")
    return code


def _post(url, body, form=False):
    if form:
        data = urllib.parse.urlencode(body).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode()
        content_type = "application/json"
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": content_type, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}")
            message = detail.get("message") or detail.get("error_description") or detail.get("error")
        except ValueError:
            message = None
        raise T3ConnectError(f"T3 answered {error.code} at {urllib.parse.urlsplit(url).path}" + (f": {message}" if message else "")) from None
    except (urllib.error.URLError, OSError) as error:
        raise T3ConnectError(f"cannot reach T3 Code at {url} ({error}); is T3 Code running?") from None


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise T3ConnectError(f"cannot reach T3 Code at {url} ({error}); is T3 Code running?") from None


def _write_json(path, data):
    """Write JSON with mode 0600 in a 0700 folder, replacing any earlier file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def session_id_of(access_token):
    """The T3 session ID inside a token T3 issued: its first part is base64url JSON with `sid`."""
    try:
        part = access_token.split(".")[0]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, ValueError, TypeError):
        return None
    sid = claims.get("sid") if isinstance(claims, dict) else None
    return sid if isinstance(sid, str) and sid else None


def write_token(path, access_token, expires_at, session_id=None):
    data = {"access_token": access_token, "expires_at": int(expires_at)}
    if session_id:
        data["session_id"] = session_id
    _write_json(path, data)


def held_session(token_file=T3_TOKEN_FILE):
    """The ID of the T3 session behind this install's token, or None."""
    try:
        data = json.loads(Path(token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    return sid if isinstance(sid, str) and sid else session_id_of(data.get("access_token"))


def revoke_file(token_file=T3_TOKEN_FILE):
    return Path(token_file).with_name("t3-mcp-revoke-pending")


def pending_revocations(token_file=T3_TOKEN_FILE):
    """IDs of T3 sessions Lighthouse held and has not revoked yet."""
    try:
        ids = json.loads(revoke_file(token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in ids if isinstance(item, str) and item] if isinstance(ids, list) else []


def _save_pending(token_file, ids):
    ids = list(dict.fromkeys(ids))
    if ids:
        _write_json(revoke_file(token_file), ids)
    else:
        revoke_file(token_file).unlink(missing_ok=True)


def _retire_held_session(token_file, keep=None):
    """Move the session behind the current token to the revoke list, before the token is replaced."""
    sid = held_session(token_file)
    if sid and sid != keep:
        _save_pending(token_file, pending_revocations(token_file) + [sid])


def incomplete_message(session_ids, cli=None):
    commands = "; ".join(f"{cli or 't3'} auth session revoke {sid}" for sid in session_ids)
    return ("T3 Code may still accept a token Lighthouse held, so revocation is not finished. "
            f"With T3 Code open, run: {commands}. Or in T3 Code open Settings, Sharing & access, and revoke "
            "the Lighthouse session.")


class _Authorization:
    """One OAuth authorization request to T3: the client registration, PKCE verifier, and state."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        parts = urllib.parse.urlsplit(endpoint)
        self.base = f"{parts.scheme}://{parts.netloc}"
        self.metadata = _get(self.base + "/.well-known/oauth-authorization-server")
        for key in ("registration_endpoint", "token_endpoint"):
            if not isinstance(self.metadata.get(key), str):
                raise T3ConnectError(f"T3 Code does not offer {key.replace('_', ' ')}; update T3 Code")
        client = _post(self.metadata["registration_endpoint"], {
            "client_name": CLIENT_NAME, "redirect_uris": [REDIRECT_URI], "grant_types": ["authorization_code"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        self.client_id = client.get("client_id")
        if not isinstance(self.client_id, str) or not self.client_id:
            raise T3ConnectError("T3 did not register Lighthouse as a client")
        self.verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        self.state = secrets.token_urlsafe(24)
        self.request = {"response_type": "code", "client_id": self.client_id, "redirect_uri": REDIRECT_URI,
                        "code_challenge": challenge, "code_challenge_method": "S256", "state": self.state,
                        "resource": endpoint}

    def browser_url(self):
        """T3's own approval page for this request (its authorize endpoint sends the browser to /connect-agent)."""
        authorize = self.metadata.get("authorization_endpoint") or self.base + "/oauth/mcp/authorize"
        return authorize + "?" + urllib.parse.urlencode(self.request)

    def finish(self, query, token_file, now):
        """Exchange the code T3 sent back for a token and save it. Returns its expiry (epoch seconds)."""
        if query.get("state") != [self.state]:
            raise T3ConnectError("T3's answer did not match this request")
        if "error" in query or "code" not in query:
            raise T3ConnectError("T3 did not approve Lighthouse: " + (query.get("error_description") or query.get("error") or ["no code"])[0])
        token = _post(self.metadata["token_endpoint"], {
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT_URI,
            "client_id": self.client_id, "code_verifier": self.verifier, "resource": self.endpoint}, form=True)
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise T3ConnectError("T3 returned no access token")
        expires_at = int(now()) + int(token.get("expires_in") or 0)
        session_id = session_id_of(access_token)
        _retire_held_session(token_file, keep=session_id)
        write_token(token_file, access_token, expires_at, session_id)
        return expires_at


def connect(pairing_code, endpoint=T3_ENDPOINT, token_file=T3_TOKEN_FILE, now=time.time):
    """Run T3's OAuth flow with a pairing code and save the token. Returns its expiry (epoch seconds).

    The approval is the call T3's "Connect agent" page makes: POST /oauth/mcp/decision.
    """
    authorization = _Authorization(endpoint)
    try:
        decision = _post(authorization.base + "/oauth/mcp/decision", {
            "authorization": authorization.request,
            "decision": {"_tag": "pairing-code", "access": ACCESS, "code": pairing_code}})
    except T3ConnectError as error:
        raise T3ApprovalError(str(error)) from None
    redirect = urllib.parse.urlsplit(str(decision.get("redirectTo") or ""))
    return authorization.finish(urllib.parse.parse_qs(redirect.query), token_file, now)


def connect_in_browser(endpoint=T3_ENDPOINT, token_file=T3_TOKEN_FILE, now=time.time, opener=None,
                       wait_seconds=BROWSER_WAIT_SECONDS):
    """Open T3's Connect agent page and wait for T3 to send the browser back with a code.

    The user approves Lighthouse on that page with a pairing code from T3 (Settings, Sharing &
    access). Returns the token's expiry (epoch seconds).
    """
    authorization = _Authorization(endpoint)
    redirect = urllib.parse.urlsplit(REDIRECT_URI)
    answers = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != redirect.path:
                self.send_response(404)
                self.end_headers()
                return
            answers.append(urllib.parse.parse_qs(url.query))
            body = b"Lighthouse got T3's answer. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = HTTPServer((redirect.hostname, redirect.port), Callback)
    except OSError as error:
        raise T3ConnectError(f"cannot listen on {redirect.hostname}:{redirect.port} for T3's answer ({error})") from None
    with server:
        server.timeout = 1
        url = authorization.browser_url()
        print("Opening T3 Code's Connect agent page. Approve Lighthouse there with a pairing code from "
              "T3 Code: Settings, Sharing & access. If no page opens, open this address:\n" + url, flush=True)
        (opener or _open_in_browser)(url)
        deadline = time.monotonic() + wait_seconds
        while not answers and time.monotonic() < deadline:
            server.handle_request()
    if not answers:
        raise T3ConnectError("T3 sent no answer within five minutes; run this again")
    return authorization.finish(answers[0], token_file, now)


def _open_in_browser(url):
    subprocess.run(["open", url], capture_output=True, timeout=30)


def guided_step():
    return (f"To connect by hand, keep T3 Code open and run: {MANAGE_COMMAND} t3-connect --browser\n"
            "It opens T3's Connect agent page, where you approve Lighthouse with a pairing code from "
            "T3 Code: Settings, Sharing & access.")


def revoke_sessions(cli, session_ids, run=subprocess.run):
    """Revoke the given T3 sessions with T3's own command. Returns the IDs that were not revoked."""
    failed = []
    for session_id in session_ids:
        try:
            result = run([cli, "auth", "session", "revoke", session_id], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            failed.append(session_id)
            continue
        # T3 exits 0 both for "Revoked session" and "No active session found": either way it is gone.
        if result.returncode != 0:
            failed.append(session_id)
    return failed


def finish_revocations(cli, token_file=T3_TOKEN_FILE, run=subprocess.run):
    """Revoke the sessions on the revoke list. Returns the IDs still on it."""
    pending = pending_revocations(token_file)
    if not pending:
        return []
    left = revoke_sessions(cli, pending, run) if cli else pending
    _save_pending(token_file, left)
    return left


def connect_with_cli(cli, endpoint=T3_ENDPOINT, token_file=T3_TOKEN_FILE, run=subprocess.run, connector=None):
    """Connect with a pairing code from the t3 command, then revoke the session the old token used."""
    expires_at = (connector or connect)(pairing_code_from_cli(cli, run), endpoint, token_file)
    finish_revocations(cli, token_file, run)
    return expires_at


def token_expiry(token_file=T3_TOKEN_FILE):
    try:
        return int(json.loads(Path(token_file).read_text(encoding="utf-8"))["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def renew_if_due(state, token_file=T3_TOKEN_FILE, now=time.time, cli_finder=t3_cli, renew=connect_with_cli,
                 run=subprocess.run):
    """Renew a T3 token that expires within RENEW_SECONDS, at most once per RETRY_SECONDS.

    Between renewals, retries revocations that failed earlier, also at most once per RETRY_SECONDS.
    Without a token file or a revoke list nothing happens: Lighthouse was never connected to T3.
    Returns the new expiry, or None when nothing was renewed.
    """
    expires_at = token_expiry(token_file)
    if expires_at is None or expires_at - now() > RENEW_SECONDS:
        attempted_at = state.get("t3_revoke_attempted_at")
        if pending_revocations(token_file) and (attempted_at is None or now() - attempted_at >= RETRY_SECONDS):
            state["t3_revoke_attempted_at"] = now()
            cli = cli_finder()
            left = finish_revocations(cli, token_file, run)
            if left:
                raise T3ConnectError(incomplete_message(left, cli))
        return None
    attempted_at = state.get("t3_renew_attempted_at")
    if attempted_at is not None and now() - attempted_at < RETRY_SECONDS:
        return None
    state["t3_renew_attempted_at"] = now()
    cli = cli_finder()
    if not cli:
        raise T3ConnectError(f"the T3 token expires soon and the t3 command was not found; run: {MANAGE_COMMAND} t3-connect --browser")
    return renew(cli, token_file=token_file)


def disconnect(token_file=T3_TOKEN_FILE, cli_finder=t3_cli, run=subprocess.run):
    """Revoke the T3 session behind this install's token and delete the token file.

    Returns the IDs of sessions that could not be revoked; they stay on the revoke list.
    """
    _retire_held_session(token_file)
    Path(token_file).unlink(missing_ok=True)
    return finish_revocations(cli_finder(), token_file, run)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Connect Lighthouse to T3 Code so stall notices go into T3 threads.")
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--pairing-code-stdin", action="store_true",
                     help="read a pairing code from stdin instead of asking the t3 command for one")
    how.add_argument("--browser", action="store_true",
                     help="approve Lighthouse on T3's Connect agent page instead")
    how.add_argument("--disconnect", action="store_true", help="revoke Lighthouse's T3 token and delete the token file")
    args = parser.parse_args(argv)
    cli = t3_cli()
    if args.disconnect:
        left = disconnect()
        if left:
            print(f"error: {incomplete_message(left, cli)}", file=sys.stderr)
            return EXIT_REVOKE_INCOMPLETE
        print("Disconnected from T3 Code.")
        return 0
    try:
        if args.pairing_code_stdin:
            code = sys.stdin.readline().strip()
            if not code:
                raise T3ConnectError("no pairing code on stdin")
            expires_at = connect(code)
        elif args.browser:
            expires_at = connect_in_browser()
        elif cli:
            expires_at = connect_with_cli(cli)
        else:
            raise T3ConnectError("the t3 command was not found, so Lighthouse cannot get a pairing code by itself")
    except T3ConnectError as error:
        print(f"error: {error}", file=sys.stderr)
        if not args.browser:
            print(guided_step(), file=sys.stderr)
        return 1
    left = finish_revocations(cli)
    renewal = "the collector renews it before then" if cli else "run this again before then"
    print(f"Connected to T3 Code. Stall notices for T3 subagents now go into the parent thread. "
          f"The token expires {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at))}; {renewal}.")
    if left:
        print(f"warning: {incomplete_message(left, cli)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
