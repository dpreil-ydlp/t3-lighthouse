import hashlib
import hmac
import json
import os
import stat
import urllib.request
from pathlib import Path
from typing import Protocol


class Channel(Protocol):
    def send(self, payload: dict) -> None: ...


class TestSink:
    def send(self, payload: dict) -> None:
        return None


class T3Webhook:
    def __init__(self, url: str, secret_file: str):
        self.url = url
        self.secret_file = Path(secret_file)

    def send(self, payload: dict) -> None:
        mode = stat.S_IMODE(self.secret_file.stat().st_mode)
        if mode != 0o600:
            raise RuntimeError("webhook secret file must have mode 0600")
        secret = self.secret_file.read_bytes().strip()
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/json", "X-T3-Signature": signature, "User-Agent": "Lighthouse/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise RuntimeError("T3 webhook returned non-success status")


def channel_for(name: str, policy: dict | None = None) -> Channel:
    if name == "test_sink":
        return TestSink()
    if name == "t3_webhook":
        url = (policy or {}).get("t3_webhook_url", "")
        path = os.getenv("T3_WEBHOOK_SECRET_FILE", "/host-secrets/t3-webhook-hmac")
        if not url or not Path(path).is_file():
            raise RuntimeError("T3 webhook is not configured")
        return T3Webhook(url, path)
    raise RuntimeError("channel is delivered by the Mac collector")
