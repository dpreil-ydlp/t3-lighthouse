#!/usr/bin/env python3
"""Connect Lighthouse to T3 Code, so stall notices can go into T3 threads.

T3 Code's MCP server takes OAuth tokens (see /.well-known/oauth-authorization-server on the
T3 server). This script registers Lighthouse as a client, approves it with a T3 pairing code,
the same code T3's "Connect agent" page asks for, and saves the token for the collector.
The pairing code comes from `t3 auth pairing create` when the t3 command is on this Mac,
otherwise from the user (T3: Settings, Sharing & access). It is used once and never stored.

T3 issues no refresh tokens, so the collector runs this again before the token expires.
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


class T3ConnectError(RuntimeError):
    pass


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


def write_token(path, access_token, expires_at):
    """Write the token file with mode 0600 in a 0700 folder, replacing any earlier one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=".t3-mcp-token.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({"access_token": access_token, "expires_at": int(expires_at)}, file)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def connect(pairing_code, endpoint=T3_ENDPOINT, token_file=T3_TOKEN_FILE, now=time.time):
    """Run T3's OAuth flow with a pairing code and save the token. Returns its expiry (epoch seconds)."""
    parts = urllib.parse.urlsplit(endpoint)
    base = f"{parts.scheme}://{parts.netloc}"
    metadata = _get(base + "/.well-known/oauth-authorization-server")
    for key in ("registration_endpoint", "token_endpoint"):
        if not isinstance(metadata.get(key), str):
            raise T3ConnectError(f"T3 Code does not offer {key.replace('_', ' ')}; update T3 Code")
    client = _post(metadata["registration_endpoint"], {
        "client_name": CLIENT_NAME, "redirect_uris": [REDIRECT_URI], "grant_types": ["authorization_code"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    client_id = client.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise T3ConnectError("T3 did not register Lighthouse as a client")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    authorization = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
                     "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                     "resource": endpoint}
    decision = _post(base + "/oauth/mcp/decision", {
        "authorization": authorization,
        "decision": {"_tag": "pairing-code", "access": ACCESS, "code": pairing_code}})
    redirect = urllib.parse.urlsplit(str(decision.get("redirectTo") or ""))
    query = urllib.parse.parse_qs(redirect.query)
    if query.get("state") != [state]:
        raise T3ConnectError("T3's answer did not match this request")
    if "error" in query or "code" not in query:
        raise T3ConnectError("T3 did not approve Lighthouse: " + (query.get("error_description") or query.get("error") or ["no code"])[0])
    token = _post(metadata["token_endpoint"], {
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT_URI,
        "client_id": client_id, "code_verifier": verifier, "resource": endpoint}, form=True)
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise T3ConnectError("T3 returned no access token")
    expires_at = int(now()) + int(token.get("expires_in") or 0)
    write_token(token_file, access_token, expires_at)
    return expires_at


def lighthouse_sessions(cli, run=subprocess.run):
    """IDs of the T3 sessions earlier connects made for Lighthouse."""
    result = run([cli, "auth", "session", "list", "--json"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return []
    try:
        sessions = json.loads(result.stdout)
    except ValueError:
        return []
    return [item["sessionId"] for item in sessions if isinstance(item, dict) and isinstance(item.get("sessionId"), str)
            and item.get("subject") == "mcp-client" and (item.get("client") or {}).get("label") == CLIENT_NAME]


def revoke_sessions(cli, session_ids, run=subprocess.run):
    for session_id in session_ids:
        run([cli, "auth", "session", "revoke", session_id], capture_output=True, text=True, timeout=60)


def connect_with_cli(cli, endpoint=T3_ENDPOINT, token_file=T3_TOKEN_FILE, run=subprocess.run, connector=None):
    """Connect with a pairing code from the t3 command, then revoke the tokens earlier connects made."""
    earlier = lighthouse_sessions(cli, run)
    expires_at = (connector or connect)(pairing_code_from_cli(cli, run), endpoint, token_file)
    revoke_sessions(cli, earlier, run)
    return expires_at


def token_expiry(token_file=T3_TOKEN_FILE):
    try:
        return int(json.loads(Path(token_file).read_text(encoding="utf-8"))["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def renew_if_due(state, token_file=T3_TOKEN_FILE, now=time.time, cli_finder=t3_cli, renew=connect_with_cli):
    """Renew a T3 token that expires within RENEW_SECONDS, at most once per RETRY_SECONDS.

    Without a token file nothing happens: Lighthouse was never connected to T3.
    Returns the new expiry, or None when nothing was renewed.
    """
    expires_at = token_expiry(token_file)
    if expires_at is None or expires_at - now() > RENEW_SECONDS:
        return None
    attempted_at = state.get("t3_renew_attempted_at")
    if attempted_at is not None and now() - attempted_at < RETRY_SECONDS:
        return None
    state["t3_renew_attempted_at"] = now()
    cli = cli_finder()
    if not cli:
        raise T3ConnectError("the T3 token expires soon and the t3 command was not found; run: lighthouse t3-connect")
    return renew(cli, token_file=token_file)


def disconnect(token_file=T3_TOKEN_FILE, cli_finder=t3_cli, run=subprocess.run):
    """Revoke the T3 tokens Lighthouse holds and delete the token file."""
    cli = cli_finder()
    if cli:
        revoke_sessions(cli, lighthouse_sessions(cli, run), run)
    Path(token_file).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Connect Lighthouse to T3 Code so stall notices go into T3 threads.")
    parser.add_argument("--pairing-code-stdin", action="store_true",
                        help="read a pairing code from stdin instead of asking the t3 command for one")
    parser.add_argument("--disconnect", action="store_true", help="revoke Lighthouse's T3 tokens and delete the token file")
    args = parser.parse_args(argv)
    try:
        if args.disconnect:
            disconnect()
            print("Disconnected from T3 Code.")
            return 0
        cli = t3_cli()
        if args.pairing_code_stdin:
            code = sys.stdin.readline().strip()
            if not code:
                raise T3ConnectError("no pairing code on stdin")
            expires_at = connect(code)
        elif cli:
            expires_at = connect_with_cli(cli)
        else:
            raise T3ConnectError("the t3 command was not found. In T3 Code, open Settings, Sharing & access, create a "
                                 "pairing code, and pipe it to this command with --pairing-code-stdin")
    except T3ConnectError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    renewal = "the collector renews it before then" if cli else "run this again before then"
    print(f"Connected to T3 Code. Stall notices for T3 subagents now go into the parent thread. "
          f"The token expires {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at))}; {renewal}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
