from datetime import datetime, timedelta, timezone
import uuid

import pytest
from psycopg.types.json import Jsonb

from conftest import make_project
from lighthouse.db import connection
from lighthouse.project_discovery import ensure_auto_thread


def add_parent(project, native_id, t3_id=None):
    with connection() as conn:
        thread_id = ensure_auto_thread(conn, project["id"], "codex", native_id)
        if t3_id:
            conn.execute("DELETE FROM thread_titles WHERE t3_thread_id=%s", (t3_id,))
            conn.execute("INSERT INTO thread_titles(source,external_thread_id,title,title_origin,t3_thread_id,t3_subagent) VALUES('codex',%s,'Parent','t3',%s,false)", (native_id,t3_id))
    return thread_id


def upload(client, headers, child_id, status="running", activity=None, parent_root=None, parent_t3_id="t3-parent", child_output=None, output_missing=False):
    subagent={
        "parent_t3_thread_id":parent_t3_id, "parent_title":"Parent title",
        "parent_project_root":parent_root, "child_t3_thread_id":child_id,
        "title":"Child title", "status":status,
        "last_activity_at":(activity or datetime.now(timezone.utc)).isoformat(),
    }
    if child_output is not None:
        subagent["last_child_output_at"] = child_output.isoformat()
    elif output_missing:
        subagent["last_child_output_at"] = None
    return client.post("/api/v1/discovery/subagents", headers=headers, json={"subagents":[{
        **subagent
    }]})


def test_parent_chat_and_project_bind_from_t3_workspace_root(client,headers):
    project=make_project(client,headers,"subagent root binding")
    root="/tmp/subagent-"+uuid.uuid4().hex
    with connection() as conn:
        conn.execute("INSERT INTO project_roots(project_id,path,display_path,display_name,origin) VALUES(%s,%s,%s,%s,'folder')",
                     (project["id"],root,root,"subagent drill root"))
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    result=upload(client,headers,child_id,parent_root=root,parent_t3_id="t3-parent-new")
    assert result.status_code==200,result.text
    with connection() as conn:
        row=conn.execute("""SELECT t.project_id,pt.source,pt.external_thread_id,tt.t3_thread_id
          FROM tasks t JOIN parent_threads pt ON pt.id=t.parent_thread_id
          JOIN thread_titles tt ON tt.source=pt.source AND tt.external_thread_id=pt.external_thread_id
          WHERE t.derived_external_id=%s""",(child_id,)).fetchone()
    assert str(row["project_id"])==project["id"] and row["source"]=="t3"
    assert row["external_thread_id"]=="t3-parent-new" and row["t3_thread_id"]=="t3-parent-new"


def test_safe_unknown_t3_root_is_registered_for_parent_binding(client,headers):
    project_root="/Users/example/Projects/Subagent-"+uuid.uuid4().hex
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    response=upload(client,headers,child_id,parent_root=project_root,parent_t3_id="t3-parent-auto")
    assert response.status_code==200,response.text
    with connection() as conn:
        row=conn.execute("""SELECT p.origin,r.path,t.project_id FROM tasks t
          JOIN projects p ON p.id=t.project_id JOIN project_roots r ON r.project_id=p.id
          WHERE t.derived_external_id=%s""",(child_id,)).fetchone()
    assert row["origin"]=="auto" and row["path"]==project_root.lower()


@pytest.mark.parametrize(("source_status","task_status"), [("running","running"),("completed","complete"),("cancelled","cancelled"),("failed","blocked"),("waiting_approval","blocked"),("interrupted","blocked")])
def test_subagent_state_maps_to_running_only_for_source_running(client, headers, source_status, task_status):
    project=make_project(client,headers,"subagent state")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    response=upload(client,headers,child_id,source_status)
    assert response.status_code==200,response.text
    with connection() as conn:
        row=conn.execute("SELECT status,derived_source,derived_external_id,parent_thread_id FROM tasks WHERE derived_external_id=%s",(child_id,)).fetchone()
    assert row["status"]==task_status and row["derived_source"]=="t3_subagent"
    assert row["parent_thread_id"] is not None


def test_completed_child_with_old_activity_does_not_open_incident(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent completed")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"completed").status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(datetime.now(timezone.utc)-timedelta(hours=2),child_id))
    assert evaluate_stalls()==[]


def test_child_finishing_with_pending_incident_cancels_undelivered_kick(client,headers):
    from lighthouse.worker import advance_incidents,evaluate_stalls
    project=make_project(client,headers,"subagent finish")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"running").status_code==200
    old=datetime.now(timezone.utc)-timedelta(minutes=31)
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(old,child_id))
    now=datetime.now(timezone.utc)
    incident=evaluate_stalls(now)[0]
    assert upload(client,headers,child_id,"completed").status_code==200
    advance_incidents(now+timedelta(minutes=11))
    with connection() as conn:
        row=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()
        notices=conn.execute("SELECT state FROM notification_outbox WHERE incident_id=%s",(incident,)).fetchall()
    assert row["state"]=="resolved" and notices and all(item["state"]=="failed" for item in notices)


def test_child_removed_from_complete_source_snapshot_is_stopped(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent removed child")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"running").status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",
                     (datetime.now(timezone.utc)-timedelta(minutes=31),child_id))
    incident=evaluate_stalls()[0]
    response=client.post("/api/v1/discovery/subagents",headers=headers,json={"subagents":[]})
    assert response.status_code==200,response.text
    with connection() as conn:
        task=conn.execute("SELECT status FROM tasks WHERE derived_external_id=%s",(child_id,)).fetchone()
        state=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()
        notices=conn.execute("SELECT state FROM notification_outbox WHERE incident_id=%s",(incident,)).fetchall()
    assert task["status"]=="blocked" and state["state"]=="resolved"
    assert notices and all(row["state"]=="failed" for row in notices)


def test_subagent_upsert_refreshes_progress_and_keeps_one_row(client,headers):
    project=make_project(client,headers,"subagent refresh")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent")
    child_id="thread:delegated-task:"+"x"*220
    first=datetime.now(timezone.utc)-timedelta(minutes=2)
    second=datetime.now(timezone.utc)
    assert upload(client,headers,child_id,"running",first,child_output=first).status_code==200
    assert upload(client,headers,child_id,"running",second,child_output=second).status_code==200
    with connection() as conn:
        rows=conn.execute("SELECT status,last_meaningful_progress_at FROM tasks WHERE derived_external_id=%s",(child_id,)).fetchall()
    assert len(rows)==1 and rows[0]["status"]=="running"
    assert rows[0]["last_meaningful_progress_at"]>=second-timedelta(seconds=1)


def test_stall_outbox_targets_parent_t3_and_carries_child_link(client,headers):
    from lighthouse import worker
    project=make_project(client,headers,"subagent delivery")
    parent_native="parent-"+uuid.uuid4().hex
    parent_id=add_parent(project,parent_native,"t3-parent")
    with connection() as conn:
        conn.execute("UPDATE controllers SET policy=%s WHERE project_id=%s",(Jsonb({"channel":"t3_webhook","t3_webhook_url":"https://example.invalid/hook"}),project["id"]))
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"running").status_code==200
    old=datetime.now(timezone.utc)-timedelta(minutes=31)
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(old,child_id))
    now=datetime.now(timezone.utc)
    opened=worker.evaluate_stalls(now)
    assert len(opened)==1
    with connection() as conn:
        outbox=conn.execute("SELECT channel,recipient,payload FROM notification_outbox WHERE incident_id=%s",(opened[0],)).fetchone()
        env=conn.execute("SELECT environment_id FROM t3_discovery_state WHERE singleton=true").fetchone()
    assert outbox["channel"]=="t3_mcp" and outbox["recipient"]==str(parent_id)
    assert outbox["payload"]["thread_id"]=="t3-parent"
    child_link=f"t3code://threads/{env['environment_id']}/{child_id}" if env and env["environment_id"] else f"t3code://threads/{child_id}"
    assert outbox["payload"]["child_url"]==child_link
    assert "message" not in outbox["payload"]
    assert worker.deliver_outbox()==[]


def test_non_t3_parent_stall_is_logged_without_mac_alert(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent log only")
    parent_id=add_parent(project,"native-parent-"+uuid.uuid4().hex)
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    old=datetime.now(timezone.utc)-timedelta(minutes=31)
    with connection() as conn:
        conn.execute("""INSERT INTO tasks(project_id,parent_thread_id,title,description,status,last_meaningful_progress_at,derived_source,derived_external_id)
          VALUES(%s,%s,'Subagent: Child title','t3code://threads/environment/child','running',%s,'t3_subagent',%s)""",
          (project["id"],parent_id,old,child_id))
    opened=evaluate_stalls()
    assert len(opened)==1
    with connection() as conn:
        notices=conn.execute("SELECT channel FROM notification_outbox WHERE incident_id=%s",(opened[0],)).fetchall()
        event=conn.execute("SELECT count(*) AS n FROM events WHERE event_type='incident_opened' AND details->>'incident_id'=%s",(opened[0],)).fetchone()
    assert notices==[] and event["n"]==1


def test_derived_t3_parent_uses_mcp_payload_and_sends_one_second_kick(client,headers):
    from lighthouse import worker
    project=make_project(client,headers,"subagent watchdog")
    parent_id=add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-watchdog")
    with connection() as conn:
        conn.execute("UPDATE controllers SET policy=%s WHERE project_id=%s",
          (Jsonb({"channel":"t3_webhook","t3_webhook_url":"https://example.invalid/watchdog"}),project["id"]))
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"running",parent_t3_id="t3-parent-watchdog").status_code==200
    old=datetime.now(timezone.utc)-timedelta(minutes=31)
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(old,child_id))
    opened=worker.evaluate_stalls()
    assert len(opened)==1
    with connection() as conn:
        first=conn.execute("SELECT id,channel,recipient,payload FROM notification_outbox WHERE incident_id=%s",(opened[0],)).fetchone()
    assert first["channel"]=="t3_mcp" and first["recipient"]==str(parent_id)
    payload=first["payload"]
    assert {"kind","incident_id","parent_thread_id","child_thread_id","child_title","silent_minutes","child_url"} <= payload.keys()
    assert payload["kind"]=="first" and payload["incident_id"]==opened[0]
    assert payload["parent_thread_id"]=="t3-parent-watchdog" and payload["child_thread_id"]==child_id
    assert payload["child_title"]=="Child title" and payload["silent_minutes"]>=30
    assert payload["child_url"].startswith("t3code://threads/")
    assert worker.deliver_outbox()==[]
    delivered=client.post(f"/api/v1/t3-mcp/{first['id']}/delivered",headers=headers,json={})
    assert delivered.status_code==200,delivered.text
    with connection() as conn:
        first_delivered=conn.execute("SELECT delivered_at FROM notification_outbox WHERE incident_id=%s AND payload->>'kind'='first'",(opened[0],)).fetchone()["delivered_at"]
    worker.advance_incidents(first_delivered+timedelta(minutes=29))
    with connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM notification_outbox WHERE incident_id=%s AND payload->>'kind'='second'",(opened[0],)).fetchone()["n"]==0
    now=first_delivered+timedelta(minutes=30)
    worker.advance_incidents(now)
    with connection() as conn:
        second=conn.execute("SELECT channel,payload FROM notification_outbox WHERE incident_id=%s AND payload->>'kind'='second'",(opened[0],)).fetchall()
        incident=conn.execute("SELECT state FROM incidents WHERE id=%s",(opened[0],)).fetchone()
    assert len(second)==1 and second[0]["channel"]=="t3_mcp"
    assert second[0]["payload"]["incident_id"]==opened[0]
    assert incident["state"]=="escalated"
    worker.advance_incidents(now+timedelta(minutes=31))
    with connection() as conn:
        count=conn.execute("SELECT count(*) AS n FROM notification_outbox WHERE incident_id=%s AND payload->>'kind'='second'",(opened[0],)).fetchone()["n"]
        mac=conn.execute("SELECT count(*) AS n FROM notification_outbox WHERE incident_id=%s AND channel='mac_alert'",(opened[0],)).fetchone()["n"]
    assert count==1 and mac==0


def test_subagent_progress_resolves_incident_and_suppresses_pending_kick(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent progress resolution")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-progress")
    with connection() as conn:
        conn.execute("UPDATE controllers SET policy=%s WHERE project_id=%s",
          (Jsonb({"channel":"t3_webhook","t3_webhook_url":"https://example.invalid/watchdog"}),project["id"]))
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"running",parent_t3_id="t3-parent-progress").status_code==200
    old=datetime.now(timezone.utc)-timedelta(minutes=31)
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(old,child_id))
    incident=evaluate_stalls()[0]
    progress=datetime.now(timezone.utc)
    assert upload(client,headers,child_id,"running",progress,parent_t3_id="t3-parent-progress",child_output=progress).status_code==200
    with connection() as conn:
        state=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()["state"]
        outbox=conn.execute("SELECT state FROM notification_outbox WHERE incident_id=%s",(incident,)).fetchall()
    assert state=="resolved" and outbox and all(row["state"]=="failed" for row in outbox)


def test_generic_t3_activity_does_not_resolve_derived_stall(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent generic activity")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-generic")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    output=datetime.now(timezone.utc)-timedelta(minutes=31)
    assert upload(client,headers,child_id,"running",output,parent_t3_id="t3-parent-generic",child_output=output).status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(output,child_id))
    incident=evaluate_stalls()[0]
    heartbeat=datetime.now(timezone.utc)
    response=upload(client,headers,child_id,"running",heartbeat,parent_t3_id="t3-parent-generic",output_missing=True)
    assert response.status_code==200,response.text
    with connection() as conn:
        task=conn.execute("SELECT last_meaningful_progress_at FROM tasks WHERE derived_external_id=%s",(child_id,)).fetchone()
        state=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()["state"]
    assert task["last_meaningful_progress_at"]==output
    assert state!="resolved"


def test_new_child_output_resolves_derived_stall(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent child output")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-output")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    output=datetime.now(timezone.utc)-timedelta(minutes=31)
    assert upload(client,headers,child_id,"running",output,parent_t3_id="t3-parent-output",child_output=output).status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(output,child_id))
    incident=evaluate_stalls()[0]
    fresh_output=datetime.now(timezone.utc)
    response=upload(client,headers,child_id,"running",fresh_output+timedelta(seconds=45),parent_t3_id="t3-parent-output",child_output=fresh_output)
    assert response.status_code==200,response.text
    with connection() as conn:
        task=conn.execute("SELECT last_meaningful_progress_at FROM tasks WHERE derived_external_id=%s",(child_id,)).fetchone()
        state=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()["state"]
    assert task["last_meaningful_progress_at"]==fresh_output
    assert state=="resolved"


def test_fresh_assistant_text_prevents_false_stall(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent assistant output prevents stall")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-assistant-fresh")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    old_output=datetime.now(timezone.utc)-timedelta(minutes=31)
    assert upload(client,headers,child_id,"running",old_output,parent_t3_id="t3-parent-assistant-fresh",child_output=old_output).status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s,thresholds=%s WHERE derived_external_id=%s",
                     (old_output,Jsonb({"meaningful_progress_seconds":60}),child_id))
    fresh_output=datetime.now(timezone.utc)
    response=upload(client,headers,child_id,"running",fresh_output,parent_t3_id="t3-parent-assistant-fresh",child_output=fresh_output)
    assert response.status_code==200,response.text
    assert evaluate_stalls()==[]
    with connection() as conn:
        task=conn.execute("SELECT last_meaningful_progress_at FROM tasks WHERE derived_external_id=%s",(child_id,)).fetchone()
    assert task["last_meaningful_progress_at"]==fresh_output


def test_fresh_assistant_text_resolves_derived_stall(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent assistant output resolves stall")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-assistant-resolve")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    old_output=datetime.now(timezone.utc)-timedelta(minutes=31)
    assert upload(client,headers,child_id,"running",old_output,parent_t3_id="t3-parent-assistant-resolve",child_output=old_output).status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(old_output,child_id))
    incident=evaluate_stalls()[0]
    fresh_output=datetime.now(timezone.utc)
    response=upload(client,headers,child_id,"running",fresh_output,parent_t3_id="t3-parent-assistant-resolve",child_output=fresh_output)
    assert response.status_code==200,response.text
    with connection() as conn:
        state=conn.execute("SELECT state FROM incidents WHERE id=%s",(incident,)).fetchone()["state"]
    assert state=="resolved"


def test_second_kick_cap_survives_cooldown_until_fresh_child_output(client,headers,monkeypatch):
    from lighthouse import worker
    project=make_project(client,headers,"subagent second kick cap")
    monkeypatch.setenv("T3_DERIVED_SECOND_KICK_SECONDS","180")
    parent_id=add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent-cap")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    progress=datetime.now(timezone.utc)-timedelta(minutes=31)
    assert upload(client,headers,child_id,"running",progress,parent_t3_id="t3-parent-cap",child_output=progress).status_code==200
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s,thresholds=%s WHERE derived_external_id=%s",
                     (progress,Jsonb({"meaningful_progress_seconds":60}),child_id))
        conn.execute("UPDATE controllers SET policy=%s WHERE project_id=%s",
                     (Jsonb({"channel":"t3_mcp","cooldown_seconds":30}),project["id"]))
    opened=worker.evaluate_stalls(datetime.now(timezone.utc))
    assert len(opened)==1
    with connection() as conn:
        first=conn.execute("SELECT id FROM notification_outbox WHERE incident_id=%s AND payload->>'kind'='first'",(opened[0],)).fetchone()
    assert first
    assert client.post(f"/api/v1/t3-mcp/{first['id']}/delivered",headers=headers,json={}).status_code==200
    delivered_at=datetime.now(timezone.utc)
    worker.advance_incidents(delivered_at+timedelta(seconds=180))
    worker.evaluate_stalls(delivered_at+timedelta(seconds=181))
    with connection() as conn:
        notices=conn.execute("SELECT payload->>'kind' kind FROM notification_outbox WHERE incident_id=%s ORDER BY created_at",(opened[0],)).fetchall()
        incident=conn.execute("SELECT cooldown_until FROM incidents WHERE id=%s",(opened[0],)).fetchone()
    assert [row["kind"] for row in notices]==["first","second"]
    worker.evaluate_stalls(incident["cooldown_until"]+timedelta(seconds=1))
    with connection() as conn:
        count=conn.execute("SELECT count(*) n FROM incidents WHERE task_id=(SELECT id FROM tasks WHERE derived_external_id=%s)",(child_id,)).fetchone()["n"]
    assert count==1
    fresh=incident["cooldown_until"]+timedelta(seconds=2)
    assert upload(client,headers,child_id,"running",fresh,parent_t3_id="t3-parent-cap",child_output=fresh).status_code==200
    later=worker.evaluate_stalls(fresh+timedelta(seconds=61))
    assert len(later)==1


def test_cooldown_suppresses_repeated_kick_and_finished_child_is_not_watched(client,headers):
    from lighthouse.worker import evaluate_stalls
    project=make_project(client,headers,"subagent dedupe")
    add_parent(project,"parent-"+uuid.uuid4().hex,"t3-parent")
    child_id="thread:delegated-task:"+uuid.uuid4().hex
    assert upload(client,headers,child_id,"running").status_code==200
    old=datetime.now(timezone.utc)-timedelta(minutes=31)
    with connection() as conn:
        conn.execute("UPDATE tasks SET last_meaningful_progress_at=%s WHERE derived_external_id=%s",(old,child_id))
    now=datetime.now(timezone.utc)
    first=evaluate_stalls(now)
    assert len(first)==1 and evaluate_stalls(now+timedelta(seconds=1))==[]
    with connection() as conn:
        conn.execute("UPDATE incidents SET state='escalated' WHERE id=%s",(first[0],))
    assert evaluate_stalls(now+timedelta(minutes=1))==[]
    with connection() as conn:
        count=conn.execute("SELECT count(*) AS n FROM notification_outbox WHERE incident_id=%s",(first[0],)).fetchone()["n"]
    assert evaluate_stalls(now+timedelta(minutes=2))==[]
    with connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM notification_outbox WHERE incident_id=%s",(first[0],)).fetchone()["n"]==count

