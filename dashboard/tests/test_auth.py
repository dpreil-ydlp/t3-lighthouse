import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OWNER_TOKEN = "test-owner-token-never-served"
OBSERVATION_IDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
]
# A folder seen by two tools; binding its second source fails once, then succeeds.
MULTI_IDS = ["44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"]
CREATED_PROJECT = "66666666-6666-4666-8666-666666666666"


class ApiHandler(BaseHTTPRequestHandler):
    calls = []
    multi_bind_failures = 1
    multi_first_linked = False

    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.__class__.calls.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/api/v1/me":
            if self.headers.get("Authorization") == f"Bearer {OWNER_TOKEN}":
                return self.reply(200, {"id": "test-user", "kind": "user"})
            return self.reply(401, {"detail": "Invalid bearer token"})
        if self.headers.get("Authorization") != f"Bearer {OWNER_TOKEN}":
            return self.reply(401, {})
        if self.path.startswith("/api/v1/unassigned"):
            return self.reply(200, {"items": [
                {"observation_id": OBSERVATION_IDS[0], "source": "codex", "working_directory": "/Users/test/Projects/bind", "last_seen": "2026-01-01T00:00:00Z", "observations": 1},
                {"observation_id": OBSERVATION_IDS[1], "source": "codex", "working_directory": "/Users/test/Projects/create", "last_seen": "2026-01-01T00:00:00Z", "observations": 1},
                {"observation_id": OBSERVATION_IDS[2], "source": "codex", "working_directory": "/Users/test/Projects/ignore", "last_seen": "2026-01-01T00:00:00Z", "observations": 1},
                *([] if self.__class__.multi_first_linked else [{"observation_id": MULTI_IDS[0], "source": "codex", "working_directory": "/Users/test/Projects/multi", "last_seen": "2026-01-01T00:00:00Z", "observations": 1}]),
                {"observation_id": MULTI_IDS[1], "source": "claude_code", "working_directory": "/Users/test/Projects/multi", "last_seen": "2026-01-01T00:00:00Z", "observations": 1},
            ]})
        if self.path.split("?")[0] == "/api/v1/projects":
            return self.reply(200, {"items": [{"id": "project-1", "name": "Test project", "attention_state": "healthy", "status": "active"}]})
        return self.reply(200, {"items": []})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        self.__class__.calls.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != f"Bearer {OWNER_TOKEN}":
            return self.reply(401, {})
        if self.path == f"/api/v1/unassigned/{MULTI_IDS[0]}/create-project":
            self.__class__.multi_first_linked = True
            return self.reply(200, {"project_id": CREATED_PROJECT, "bound": True})
        if self.path == f"/api/v1/unassigned/{MULTI_IDS[1]}/bind" and self.__class__.multi_bind_failures > 0:
            self.__class__.multi_bind_failures -= 1
            return self.reply(409, {"detail": "This source is already bound to another project"})
        if self.path.endswith("/create-project"):
            return self.reply(200, {"project_id": "77777777-7777-4777-8777-777777777777", "bound": True})
        return self.reply(200, {"ok": True})


def free_port():
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port != 3000:
            return port


def wait_for_mutations(count):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len([call for call in ApiHandler.calls if call[0] == "POST"]) >= count:
            return
        time.sleep(0.05)
    pytest.fail(f"Expected {count} API mutations, observed {len([call for call in ApiHandler.calls if call[0] == 'POST'])}")


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory):
    ApiHandler.calls = []
    api = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()
    temp = tmp_path_factory.mktemp("dashboard-auth")
    token_file = temp / "owner-token"
    token_file.write_text(OWNER_TOKEN)
    key_file = temp / "session-key"
    key_file.write_bytes(b"test-only-session-signing-key-32")
    port = free_port()
    env = {
        **os.environ,
        "LIGHTHOUSE_API_URL": f"http://127.0.0.1:{api.server_port}",
        "OWNER_TOKEN_FILE": str(token_file),
        "DASHBOARD_SESSION_KEY_FILE": str(key_file),
        "DASHBOARD_SESSION_KEY": key_file.read_text() if key_file.exists() else "test-only-session-signing-key-32",
        "PORT": str(port),
        "HOSTNAME": "127.0.0.1",
    }
    process = subprocess.Popen(
        ["node_modules/.bin/next", "dev", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if process.poll() is not None:
                pytest.fail("Next.js dashboard stopped during startup")
            try:
                import urllib.request
                urllib.request.urlopen(base + "/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            pytest.fail("Next.js dashboard did not start")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)
        api.shutdown()
        api.server_close()


def test_unauthenticated_pages_redirect_and_actions_do_not_call_api(dashboard):
    with sync_playwright() as p:
        request = p.request.new_context()
        for path in ("/", "/sessions", "/projects", "/unassigned", "/incidents"):
            response = request.get(dashboard + path, max_redirects=0)
            assert response.status == 307
            assert response.headers.get("location", "").endswith("/login")
        before = len(ApiHandler.calls)
        for action in ("bind", "create", "ignore"):
            response = request.post(
                dashboard + "/sessions",
                headers={"Next-Action": f"unauthenticated-{action}"},
                data={"action": action},
            )
            assert response.status == 401
        after = len(ApiHandler.calls)
        assert after == before == 0
        request.dispose()


def test_wrong_token_is_rejected(dashboard):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(dashboard + "/login")
        page.get_by_label("Owner token").fill("wrong-token")
        page.get_by_role("button", name="Log in").click()
        page.get_by_text("Invalid owner token").wait_for()
        assert not page.context.cookies()
        browser.close()


def test_owner_login_actions_and_token_not_served(dashboard, tmp_path):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        served = []
        def capture(response):
            if response.request.resource_type in ("document", "script") and response.url.startswith(dashboard):
                try:
                    served.append(response.text())
                except Exception:
                    pass
        page.on("response", capture)
        page.goto(dashboard + "/login")
        page.screenshot(path=str(tmp_path / "login.png"))
        page.get_by_label("Owner token").fill(OWNER_TOKEN)
        page.get_by_role("button", name="Log in").click()
        page.wait_for_url(dashboard + "/")
        page.wait_for_load_state("networkidle")
        nav = page.get_by_role("navigation", name="Main")
        assert nav.get_by_role("link", name="Other activity").count() == 1, page.url + " " + page.locator("body").inner_text()
        nav.get_by_role("link", name="Other activity").click()
        page.get_by_role("heading", name="Other activity").wait_for()
        rows = page.get_by_role("row")
        rows.filter(has_text="Projects/bind").get_by_role("button", name="Track").click()
        page.get_by_role("tab", name="Add to existing").click()
        page.get_by_role("combobox").click()
        page.get_by_role("option", name="Test project").click()
        page.get_by_role("button", name="Link folder").click()
        wait_for_mutations(1)
        page.get_by_role("dialog").wait_for(state="hidden")
        rows.filter(has_text="Projects/create").get_by_role("button", name="Track").click()
        page.get_by_role("button", name="Create and track").click()
        wait_for_mutations(2)
        page.get_by_role("dialog").wait_for(state="hidden")
        rows.filter(has_text="Projects/ignore").get_by_role("button", name="Ignore").click()
        wait_for_mutations(3)

        mutations = [call for call in ApiHandler.calls if call[0] == "POST"][:3]
        assert [call[1] for call in mutations] == [
            f"/api/v1/unassigned/{OBSERVATION_IDS[0]}/bind",
            f"/api/v1/unassigned/{OBSERVATION_IDS[1]}/create-project",
            "/api/v1/unassigned/ignore",
        ]
        assert all(OWNER_TOKEN not in body for body in served)
        cookie = next(item for item in context.cookies() if item["name"] == "lighthouse_session")
        assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
        assert cookie["secure"] is False
        assert 29 * 86400 <= cookie["expires"] - time.time() <= 30 * 86400
        page.get_by_role("button", name="Log out").click()
        page.wait_for_url("**/login")
        assert not context.cookies()
        browser.close()


def test_partial_track_failure_is_reported_and_retry_links_the_rest(dashboard):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(dashboard + "/login")
        page.get_by_label("Owner token").fill(OWNER_TOKEN)
        page.get_by_role("button", name="Log in").click()
        page.wait_for_url(dashboard + "/")
        page.goto(dashboard + "/sessions")
        row = page.get_by_role("row").filter(has_text="Projects/multi")
        row.get_by_role("button", name="Track").click()
        page.get_by_role("button", name="Create and track").click()
        page.get_by_text("linked 1 of 2 sources").wait_for()
        assert page.get_by_role("dialog").is_visible()
        page.keyboard.press("Escape")
        page.reload()
        row = page.get_by_role("row").filter(has_text="Projects/multi")
        row.get_by_role("button", name="Track").click()
        page.get_by_role("tab", name="Add to existing").click()
        page.get_by_role("combobox").click()
        page.get_by_role("option", name="Test project").click()
        page.get_by_role("button", name="Link folder").click()
        page.get_by_text("Folder linked").wait_for()
        binds = [c[1] for c in ApiHandler.calls if c[0] == "POST" and MULTI_IDS[1] in c[1]]
        assert binds == [f"/api/v1/unassigned/{MULTI_IDS[1]}/bind"] * 2
        browser.close()
