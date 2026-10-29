import uuid
import hashlib
import secrets
from argon2 import PasswordHasher

from conftest import SafeHeaders, make_project


def test_auth_required_and_scope_enforced(client, headers):
    assert client.get("/api/v1/projects").status_code == 401
    response = client.get("/api/v1/projects", headers={"Authorization":headers["Authorization"],"Idempotency-Key":"not-used"})
    assert response.status_code == 200


def test_current_principal_identifies_user_and_agent(client, principals):
    user = client.get("/api/v1/me", headers={"Authorization": f"Bearer {principals['user'][0]}"})
    agent = client.get("/api/v1/me", headers={"Authorization": f"Bearer {principals['agent'][0]}"})
    invalid = client.get("/api/v1/me", headers={"Authorization": "Bearer wrong-token"})
    assert user.status_code == 200 and user.json()["kind"] == "user"
    assert agent.status_code == 200 and agent.json()["kind"] == "agent"
    assert invalid.status_code == 401
    assert "token" not in user.text.lower()


def test_verified_token_cache_skips_argon2_and_stores_only_digest(client, principals, monkeypatch):
    from lighthouse import security

    token = principals["user"][0]
    authorization = {"Authorization": f"Bearer {token}"}
    calls = 0
    hasher_type = type(security.hasher)
    original_verify = hasher_type.verify

    def count_verify(hasher, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_verify(hasher, *args, **kwargs)

    security._verified_token_cache.clear()
    security._verified_token_expiry.clear()
    monkeypatch.setattr(hasher_type, "verify", count_verify)
    assert client.get("/api/v1/me", headers=authorization).status_code == 200
    assert client.get("/api/v1/me", headers=authorization).status_code == 200
    assert calls == 1
    assert token not in repr(security._verified_token_cache)
    assert token not in security._verified_token_cache
    assert list(security._verified_token_cache.values()) == [principals["user"][1]]


def test_cached_token_rejects_revoked_principal_immediately(client, principals):
    from lighthouse import security
    from lighthouse.db import connection

    token, principal_id = principals["user"]
    authorization = {"Authorization": f"Bearer {token}"}
    security._verified_token_cache.clear()
    security._verified_token_expiry.clear()
    assert client.get("/api/v1/me", headers=authorization).status_code == 200
    with connection() as conn:
        conn.execute("UPDATE principals SET enabled=false WHERE id=%s", (principal_id,))
    assert client.get("/api/v1/me", headers=authorization).status_code == 401


def test_wrong_token_does_not_use_or_populate_verified_cache(client, principals):
    from lighthouse import security

    token = principals["user"][0]
    security._verified_token_cache.clear()
    security._verified_token_expiry.clear()
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    cached = dict(security._verified_token_cache)
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert security._verified_token_cache == cached


def test_cached_principal_scopes_are_read_fresh(client, principals):
    from lighthouse import security
    from lighthouse.db import connection

    token, principal_id = principals["agent"]
    authorization = {"Authorization": f"Bearer {token}"}
    security._verified_token_cache.clear()
    security._verified_token_expiry.clear()
    assert client.get("/api/v1/projects", headers=authorization).status_code == 200
    with connection() as conn:
        conn.execute("UPDATE principals SET permission_scopes=array_remove(permission_scopes,'project:read') WHERE id=%s", (principal_id,))
    assert client.get("/api/v1/projects", headers=authorization).status_code == 403


def test_verified_token_cache_expires_after_ttl(client, principals, monkeypatch):
    from lighthouse import security

    token = principals["user"][0]
    authorization = {"Authorization": f"Bearer {token}"}
    calls = 0
    now = 100.0
    hasher_type = type(security.hasher)
    original_verify = hasher_type.verify

    def count_verify(hasher, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_verify(hasher, *args, **kwargs)

    security._verified_token_cache.clear()
    security._verified_token_expiry.clear()
    monkeypatch.setattr(security, "_monotonic", lambda: now)
    monkeypatch.setattr(hasher_type, "verify", count_verify)
    assert client.get("/api/v1/me", headers=authorization).status_code == 200
    now += security._VERIFIED_TOKEN_CACHE_TTL_SECONDS - 1
    assert client.get("/api/v1/me", headers=authorization).status_code == 200
    now += 2
    assert client.get("/api/v1/me", headers=authorization).status_code == 200
    assert calls == 2


def test_agent_cannot_review_task(client, headers, principals):
    project = make_project(client, headers)
    task = client.post(f"/api/v1/projects/{project['id']}/tasks", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"title":"review gate"}).json()["task"]
    response = client.post(f"/api/v1/tasks/{task['id']}/review", headers={**headers,"Idempotency-Key":uuid.uuid4().hex}, json={"decision":"verified","evidence":{"note":"agent claim"}})
    assert response.status_code == 403


def test_agent_cannot_confirm_project_source(client, headers):
    project=make_project(client,headers)
    response=client.post(f"/api/v1/projects/{project['id']}/sources",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"source":"claude_code","match_kind":"working_directory","match_value":"/tmp/agent-selected"})
    assert response.status_code==403


def test_user_can_confirm_exact_source(client, principals):
    project=client.post("/api/v1/projects",headers={"Authorization":f"Bearer {principals['user'][0]}","Idempotency-Key":uuid.uuid4().hex},json={"name":"owner-bound-"+uuid.uuid4().hex,"kind":"personal"}).json()['project']
    response=client.post(f"/api/v1/projects/{project['id']}/sources",headers={"Authorization":f"Bearer {principals['user'][0]}","Idempotency-Key":uuid.uuid4().hex},json={"source":"claude_code","match_kind":"working_directory","match_value":f"/tmp/owner-confirmed-{uuid.uuid4().hex}"})
    assert response.status_code==200 and response.json()['confidence']=='high'


def test_unbound_agent_cannot_acknowledge(client,headers):
    from datetime import datetime, timezone, timedelta
    from lighthouse.db import connection
    from lighthouse.worker import evaluate_stalls,deliver_outbox
    project=make_project(client,headers)
    task=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"ack scope","thresholds":{"meaningful_progress_seconds":1}}).json()['task']
    with connection() as conn: conn.execute("UPDATE tasks SET status='running',last_meaningful_progress_at=%s WHERE id=%s",(datetime.now(timezone.utc)-timedelta(seconds=10),task['id']))
    incident=evaluate_stalls()[0]
    deliver_outbox()
    response=client.post(f"/api/v1/incidents/{incident}/ack",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert response.status_code==403


def test_unrelated_agent_cannot_replace_controller_or_ack(client,headers,principals):
    from datetime import datetime,timezone,timedelta
    from lighthouse.db import connection
    from lighthouse.worker import deliver_outbox,evaluate_stalls
    project=make_project(client,headers,"controller ownership")
    controller=client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert controller.status_code==200
    thread=client.post(f"/api/v1/projects/{project['id']}/threads",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"source":"test_sink","external_thread_id":"owned-"+uuid.uuid4().hex,"state":"running"}).json()
    task=client.post(f"/api/v1/projects/{project['id']}/tasks",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={"title":"controller acknowledgement","thresholds":{"meaningful_progress_seconds":1}}).json()["task"]
    now=datetime.now(timezone.utc)
    with connection() as conn:
        conn.execute("UPDATE tasks SET parent_thread_id=%s,status='running',last_meaningful_progress_at=%s WHERE id=%s",(thread["parent_thread_id"],now-timedelta(seconds=10),task["id"]))
    evaluate_stalls(now)
    with connection() as conn:
        incident=conn.execute("SELECT id FROM incidents WHERE task_id=%s ORDER BY opened_at DESC LIMIT 1",(task["id"],)).fetchone()["id"]
    deliver_outbox()
    token="lh_"+secrets.token_urlsafe(24)
    name="unrelated-controller-"+uuid.uuid4().hex
    with connection() as conn:
        conn.execute("INSERT INTO principals(name,kind,permission_scopes,token_hash,token_lookup_hash) VALUES(%s,'agent',%s,%s,%s)",(name,["controller:write","incident:ack"],PasswordHasher().hash(token),hashlib.sha256(token.encode()).hexdigest()))
    agent_b={"Authorization":f"Bearer {token}","Idempotency-Key":uuid.uuid4().hex}
    replacement=client.post(f"/api/v1/projects/{project['id']}/controllers",headers=agent_b,json={})
    ack=client.post(f"/api/v1/incidents/{incident}/ack",headers={**agent_b,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert replacement.status_code==403
    assert ack.status_code==403
    owner_replace=client.post(f"/api/v1/projects/{project['id']}/controllers",headers={"Authorization":f"Bearer {principals['user'][0]}","Idempotency-Key":uuid.uuid4().hex},json={})
    assert owner_replace.status_code==200


def _extra_agent(scopes=("controller:write",)):
    from lighthouse.db import connection
    token="lh_"+secrets.token_urlsafe(24)
    with connection() as conn:
        conn.execute("INSERT INTO principals(name,kind,permission_scopes,token_hash,token_lookup_hash) VALUES(%s,'agent',%s,%s,%s)",("extra-agent-"+uuid.uuid4().hex,list(scopes),PasswordHasher().hash(token),hashlib.sha256(token.encode()).hexdigest()))
    return SafeHeaders({"Authorization":f"Bearer {token}"})


def test_agent_can_claim_discovered_controller_once(client,headers):
    from lighthouse.db import connection
    from lighthouse.project_discovery import ensure_auto_thread
    project=make_project(client,headers,"discovered controller")
    with connection() as conn:
        ensure_auto_thread(conn,project["id"],"codex","auto-"+uuid.uuid4().hex)
        discovered=conn.execute("SELECT id,agent_identity_id FROM controllers WHERE project_id=%s",(project["id"],)).fetchone()
    assert discovered["agent_identity_id"] is None
    claimed=client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert claimed.status_code==200,claimed.text
    assert claimed.json()["controller_id"]==str(discovered["id"])
    with connection() as conn:
        assert conn.execute("SELECT agent_identity_id FROM controllers WHERE id=%s",(discovered["id"],)).fetchone()["agent_identity_id"] is not None
    takeover=client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**_extra_agent(),"Idempotency-Key":uuid.uuid4().hex},json={})
    assert takeover.status_code==403
    again=client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={})
    assert again.status_code==200


def test_thread_and_source_registration_reject_missing_fields_with_422(client,headers,principals):
    project=make_project(client,headers,"missing fields")
    assert client.post(f"/api/v1/projects/{project['id']}/controllers",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json={}).status_code==200
    for body in ({},{"source":"mcp"},{"external_thread_id":"t-"+uuid.uuid4().hex},{"source":"mcp","external_thread_id":"t-"+uuid.uuid4().hex,"state":"sleeping"}):
        response=client.post(f"/api/v1/projects/{project['id']}/threads",headers={**headers,"Idempotency-Key":uuid.uuid4().hex},json=body)
        assert response.status_code==422,(body,response.status_code)
    owner={"Authorization":f"Bearer {principals['user'][0]}"}
    for body in ({"match_kind":"working_directory","match_value":"/tmp/x"},{"source":"codex","match_kind":"working_directory"}):
        response=client.post(f"/api/v1/projects/{project['id']}/sources",headers={**owner,"Idempotency-Key":uuid.uuid4().hex},json=body)
        assert response.status_code==422,(body,response.status_code)

def test_idempotency_rejects_changed_payload(client, headers):
    key=uuid.uuid4().hex
    first=client.post("/api/v1/projects",headers={**headers,"Idempotency-Key":key},json={"name":"one","kind":"other"})
    changed=client.post("/api/v1/projects",headers={**headers,"Idempotency-Key":key},json={"name":"two","kind":"other"})
    assert first.status_code==200 and changed.status_code==409


def test_readme_agent_credential_can_deliver_mac_alerts_and_save_briefs(client, headers, tmp_path):
    import shlex
    from pathlib import Path
    from lighthouse import admin
    from test_brief_api import valid_brief

    # The manual install steps moved from the README to docs/install-by-hand.md.
    readme = (Path(__file__).parents[1] / "docs" / "install-by-hand.md").read_text()
    line = next(x for x in readme.splitlines() if "admin-entrypoint issue-principal" in x)
    argv = shlex.split(line)
    argv = argv[argv.index("admin-entrypoint") + 1:]
    token_file = tmp_path / "agent-token"
    argv[argv.index("--name") + 1] = "readme-agent-" + uuid.uuid4().hex
    argv[argv.index("--token-file") + 1] = str(token_file)
    admin.main(argv)
    agent = SafeHeaders({"Authorization": f"Bearer {token_file.read_text().strip()}"})
    project = make_project(client, headers, "readme agent")
    assert client.get("/api/v1/mac-alerts/pending", headers=agent).status_code == 200
    saved = client.put(f"/api/v1/projects/{project['id']}/brief", headers=agent, json=valid_brief())
    assert saved.status_code == 200, saved.text
