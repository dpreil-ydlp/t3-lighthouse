import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

from lighthouse.channels import channel_for
from lighthouse.db import connection, row_json
from psycopg.types.json import Jsonb
from lighthouse.project_discovery import ensure_auto_thread, excluded_root, match_project_root, normalize_root

logger = logging.getLogger(__name__)


def backfill_unmapped_observations(conn, batch_size=100000):
    roots = conn.execute("SELECT project_id,path,display_name AS name,origin FROM project_roots ORDER BY length(path) DESC").fetchall()
    rows = conn.execute("SELECT * FROM unmapped_observations WHERE resolved_project_id IS NULL AND working_directory IS NOT NULL ORDER BY observed_at,id LIMIT %s FOR UPDATE SKIP LOCKED", (batch_size,)).fetchall()
    mapped = 0
    for observation in rows:
        if excluded_root(observation["working_directory"]):
            continue
        root = match_project_root(observation["working_directory"], roots)
        if not root:
            continue
        project_id = root["project_id"]
        if observation["external_thread_id"]:
            thread = conn.execute("SELECT project_id FROM parent_threads WHERE source=%s AND external_thread_id=%s", (observation["source"], observation["external_thread_id"])).fetchone()
            if thread and thread["project_id"] != project_id:
                continue
        existing = conn.execute("SELECT id,project_id FROM events WHERE source=%s AND source_event_id=%s", (observation["source"], observation["source_event_id"])).fetchone()
        if existing and existing["project_id"] != project_id:
            continue
        thread_id = ensure_auto_thread(conn, project_id, observation["source"], observation["external_thread_id"])
        details = dict(observation["details"] or {})
        details.setdefault("summary", f"Observed {observation['source']} session activity")
        details["working_directory"] = observation["working_directory"]
        if not existing:
            conn.execute("""INSERT INTO events(project_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details)
              VALUES(%s,%s,'collector_observation',%s,'medium',%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING""", (project_id, thread_id, observation["source"], observation["source_event_id"], observation["observed_at"], Jsonb(details)))
        conn.execute("UPDATE unmapped_observations SET resolved_project_id=%s,bound_at=COALESCE(bound_at,now()) WHERE id=%s AND resolved_project_id IS NULL", (project_id, observation["id"]))
        mapped += 1
    return mapped


def escalate_incident(conn, incident_id, now, reason):
    incident = conn.execute("""UPDATE incidents i SET state='escalated'
      WHERE i.id=%s AND i.state NOT IN ('resolved','closed','escalated')
      RETURNING i.id,i.project_id,i.task_id,i.parent_thread_id,
        EXISTS(SELECT 1 FROM tasks t WHERE t.id=i.task_id AND t.derived_source='t3_subagent') AS is_derived""", (incident_id,)).fetchone()
    if not incident:
        return
    conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,'incident_escalated','worker','high',%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING", (incident['project_id'],incident['task_id'],incident['parent_thread_id'],f"incident-escalated:{incident['id']}",now,Jsonb({"incident_id":str(incident['id']),"reason":reason})))
    if incident["is_derived"]:
        return
    conn.execute("""INSERT INTO notification_outbox(incident_id,channel,recipient,payload,next_attempt_at)
      SELECT %s,'mac_alert','escalation',jsonb_build_object('incident_id',%s::text,'project_id',%s::text,'task_id',%s::text,'reason',%s::text),%s
      WHERE NOT EXISTS(SELECT 1 FROM notification_outbox WHERE incident_id=%s AND channel='mac_alert' AND recipient='escalation')""", (incident['id'],str(incident['id']),str(incident['project_id']),str(incident['task_id']) if incident['task_id'] else None,reason,now,incident['id']))


def evaluate_stalls(now=None):
    now = now or datetime.now(timezone.utc)
    opened = []
    with connection() as conn:
        tasks = conn.execute("""
          SELECT t.*,p.name project_name,c.policy,pt.external_thread_id parent_external_thread_id,tt.t3_thread_id parent_t3_thread_id
          FROM tasks t JOIN projects p ON p.id=t.project_id
          LEFT JOIN controllers c ON c.project_id=p.id
          LEFT JOIN parent_threads pt ON pt.id=t.parent_thread_id
          LEFT JOIN thread_titles tt ON tt.source=pt.source AND tt.external_thread_id=pt.external_thread_id
          WHERE t.status='running' AND t.last_meaningful_progress_at IS NOT NULL
        """).fetchall()
        for task in tasks:
            try:
                threshold = int(task["thresholds"].get("meaningful_progress_seconds", 1800))
                if threshold <= 0:
                    raise ValueError("meaningful_progress_seconds must be positive")
                if (now - task["last_meaningful_progress_at"]).total_seconds() < threshold:
                    continue
                latest = conn.execute("SELECT * FROM incidents WHERE task_id=%s ORDER BY opened_at DESC LIMIT 1", (task["id"],)).fetchone()
                if latest and latest["state"] not in {"resolved", "closed", "escalated"}:
                    continue
                # An owner-closed incident silences this exact stall; only a later, distinct stall reopens.
                if latest and latest["state"] == "closed" and (latest["details"] or {}).get("stall_evidence_at"):
                    if datetime.fromisoformat(latest["details"]["stall_evidence_at"]) == task["last_meaningful_progress_at"]:
                        continue
                # A derived incident already owns both notices for this exact stall evidence.
                # A later child output changes the evidence and permits a new stall episode.
                if latest and task.get("derived_source") == "t3_subagent" and (latest["details"] or {}).get("stall_evidence_at"):
                    if datetime.fromisoformat(latest["details"]["stall_evidence_at"]) == task["last_meaningful_progress_at"]:
                        continue
                if latest and latest["cooldown_until"] and latest["cooldown_until"] > now:
                    conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,'cooldown_suppressed','worker','high',%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING", (task["project_id"], task["id"], task["parent_thread_id"], f"cooldown:{latest['id']}", now, Jsonb({"incident_id": str(latest["id"])})))
                    continue
                policy = task["policy"] or {}
                ack_seconds = int(policy.get("ack_deadline_seconds", 600))
                cooldown_seconds = int(policy.get("cooldown_seconds", 1800))
                incident = conn.execute("""
                  INSERT INTO incidents(project_id,task_id,parent_thread_id,kind,state,recovery_level,reason,ack_deadline,cooldown_until,retry_budget,details)
                  VALUES(%s,%s,%s,'stall','open',1,'No meaningful progress within the task threshold',%s,%s,%s,%s) RETURNING *
                """, (task["project_id"], task["id"], task["parent_thread_id"], now + timedelta(seconds=ack_seconds), now + timedelta(seconds=cooldown_seconds), int(policy.get("retry_budget", 3)), Jsonb({"threshold_seconds": threshold, "stall_evidence_at": task["last_meaningful_progress_at"].isoformat()}))).fetchone()
                payload = {"incident_id": str(incident["id"]), "project_id": str(task["project_id"]), "task_id": str(task["id"]), "reason": "No meaningful progress within the task threshold"}
                channel = policy.get("channel", "mac_alert")
                enqueue_notice = True
                if task.get("derived_source") == "t3_subagent":
                    silent_minutes = min(10000, max(1, int((now - task["last_meaningful_progress_at"]).total_seconds() // 60)))
                    child_link = task.get("description") or ""
                    child_title = task["title"].removeprefix("Subagent: ")
                    parent_is_t3 = bool(task.get("parent_t3_thread_id"))
                    if parent_is_t3:
                        channel = "t3_mcp"
                        payload.update({"kind": "first", "incident_id": str(incident["id"]),
                                        "parent_thread_id": task["parent_t3_thread_id"],
                                        "child_thread_id": task["derived_external_id"],
                                        "child_title": child_title, "silent_minutes": silent_minutes,
                                        "child_url": child_link})
                    else:
                        enqueue_notice = False
                    if enqueue_notice:
                        payload.update({"child_url": child_link,
                                        "thread_id": task.get("parent_t3_thread_id")})
                if enqueue_notice:
                    conn.execute("INSERT INTO notification_outbox(incident_id,channel,recipient,payload,next_attempt_at) VALUES(%s,%s,%s,%s,now())", (incident["id"], channel, str(task["parent_thread_id"] or "owner"), Jsonb(payload)))
                conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,'incident_opened','worker','high',%s,%s,%s)", (task["project_id"], task["id"], task["parent_thread_id"], f"incident-opened:{incident['id']}", now, Jsonb({"incident_id": str(incident["id"])})))
                opened.append(str(incident["id"]))
            except Exception:
                logger.exception("Skipping malformed task during stall evaluation task_id=%s", task["id"])
    return opened


def deliver_outbox():
    delivered = []
    with connection() as conn:
        rows = conn.execute("""
          SELECT n.*,CASE WHEN t.derived_source='t3_subagent' THEN
              (SELECT jsonb_build_object('t3_webhook_url',policy->>'t3_webhook_url') FROM controllers WHERE policy->>'t3_webhook_url' IS NOT NULL LIMIT 1)
            ELSE c.policy END AS policy FROM notification_outbox n
          JOIN incidents i ON i.id=n.incident_id
          LEFT JOIN tasks t ON t.id=i.task_id
          LEFT JOIN parent_threads pt ON pt.id=i.parent_thread_id
          LEFT JOIN controllers c ON c.id=pt.controller_id
          WHERE n.state='pending' AND n.channel NOT IN ('mac_alert','t3_mcp') AND COALESCE(n.next_attempt_at,now())<=now()
          ORDER BY n.created_at LIMIT 20 FOR UPDATE OF n SKIP LOCKED
        """).fetchall()
        for row in rows:
            try:
                conn.execute("UPDATE incidents SET state='notifying' WHERE id=%s AND state='open'", (row["incident_id"],))
                conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) SELECT project_id,task_id,parent_thread_id,'incident_notifying','worker','high',%s,now(),%s FROM incidents WHERE id=%s ON CONFLICT(source,source_event_id) DO NOTHING", (f"incident-notifying:{row['incident_id']}",Jsonb({"incident_id":str(row['incident_id'])}),row["incident_id"]))
                channel_for(row["channel"], row["policy"]).send(row["payload"])
                conn.execute("UPDATE notification_outbox SET state='delivered',delivered_at=now(),attempt_count=attempt_count+1 WHERE id=%s", (row["id"],))
                conn.execute("UPDATE incidents SET state='awaiting_ack' WHERE id=%s AND state='notifying'", (row["incident_id"],))
                conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) SELECT project_id,task_id,parent_thread_id,'notification_delivered','worker','high',%s,now(),%s FROM incidents WHERE id=%s ON CONFLICT(source,source_event_id) DO NOTHING", (f"notification-delivered:{row['incident_id']}",Jsonb({"outbox_id":str(row['id'])}),row["incident_id"]))
                delivered.append(str(row["id"]))
            except Exception:
                logger.exception("Notification delivery failed outbox_id=%s channel=%s", row["id"], row["channel"])
                attempts = row["attempt_count"] + 1
                if attempts >= 3:
                    conn.execute("UPDATE notification_outbox SET state='failed',attempt_count=%s,last_error='delivery failed after bounded retries' WHERE id=%s", (attempts, row["id"]))
                    escalate_incident(conn, row["incident_id"], datetime.now(timezone.utc), "Notification delivery failed after three attempts")
                else:
                    conn.execute("UPDATE notification_outbox SET attempt_count=%s,next_attempt_at=now()+interval '2 seconds',last_error='delivery attempt failed' WHERE id=%s", (attempts, row["id"]))
    return delivered


def advance_incidents(now=None):
    now = now or datetime.now(timezone.utc)
    second_kick_seconds = max(1, int(os.getenv("T3_DERIVED_SECOND_KICK_SECONDS", "1800")))
    with connection() as conn:
        escalated=conn.execute("""
          SELECT i.id FROM incidents i
          WHERE i.state IN ('open','awaiting_ack') AND i.ack_deadline<=%s AND i.acknowledged_at IS NULL
            AND (i.state='awaiting_ack' OR EXISTS(SELECT 1 FROM notification_outbox n WHERE n.incident_id=i.id AND n.channel='mac_alert'))
            AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.id=i.task_id AND t.derived_source='t3_subagent')
        """, (now,)).fetchall()
        for incident in escalated:
            escalate_incident(conn, incident["id"], now, "Acknowledgement deadline expired")
        due = conn.execute("""SELECT i.id,n.recipient,n.payload,t.last_meaningful_progress_at
          FROM incidents i JOIN tasks t ON t.id=i.task_id
          JOIN notification_outbox n ON n.incident_id=i.id AND n.payload->>'kind'='first' AND n.state='delivered'
          WHERE t.derived_source='t3_subagent' AND t.status='running' AND i.state='awaiting_ack'
            AND n.delivered_at<=%s-(%s*interval '1 second') AND t.last_meaningful_progress_at<=n.delivered_at
            AND NOT EXISTS(SELECT 1 FROM notification_outbox s WHERE s.incident_id=i.id AND s.payload->>'kind'='second')
          FOR UPDATE OF i""", (now, second_kick_seconds)).fetchall()
        for row in due:
            silent_minutes = min(10000, max(1, int((now - row["last_meaningful_progress_at"]).total_seconds() // 60)))
            payload = dict(row["payload"])
            payload.update({"kind": "second", "silent_minutes": silent_minutes})
            conn.execute("INSERT INTO notification_outbox(incident_id,channel,recipient,payload,next_attempt_at) VALUES(%s,'t3_mcp',%s,%s,%s)",
                         (row["id"], row["recipient"], Jsonb(payload), now))
            conn.execute("UPDATE incidents SET state='escalated' WHERE id=%s AND state='awaiting_ack'", (row["id"],))
        rows = conn.execute("SELECT i.id,i.task_id,t.last_meaningful_progress_at FROM incidents i JOIN tasks t ON t.id=i.task_id WHERE i.state='recovering' AND i.acknowledged_at IS NOT NULL AND t.last_meaningful_progress_at>i.acknowledged_at AND t.last_meaningful_progress_at>COALESCE((i.details->>'stall_evidence_at')::timestamptz,'-infinity'::timestamptz) AND (i.progress_deadline IS NULL OR t.last_meaningful_progress_at<=i.progress_deadline)").fetchall()
        for row in rows:
            conn.execute("UPDATE incidents SET state='resolved',resumed_progress_at=%s,resolved_at=%s WHERE id=%s", (row["last_meaningful_progress_at"], now, row["id"]))
            incident=conn.execute("SELECT project_id,parent_thread_id FROM incidents WHERE id=%s", (row["id"],)).fetchone()
            conn.execute("INSERT INTO events(project_id,task_id,parent_thread_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,%s,%s,'incident_resolved','worker','high',%s,%s,%s) ON CONFLICT(source,source_event_id) DO NOTHING", (incident['project_id'],row['task_id'],incident['parent_thread_id'],f"incident-resolved:{row['id']}",now,Jsonb({"incident_id":str(row['id'])})))
        expired = conn.execute("SELECT id FROM incidents WHERE state='recovering' AND acknowledged_at IS NOT NULL AND progress_deadline IS NOT NULL AND progress_deadline<=%s", (now,)).fetchall()
        for incident in expired:
            escalate_incident(conn, incident["id"], now, "Progress deadline expired after acknowledgement")


def run():
    while True:
        evaluate_stalls()
        deliver_outbox()
        advance_incidents()
        time.sleep(float(os.getenv("WORKER_INTERVAL_SECONDS", "2")))


if __name__ == "__main__":
    if len(os.sys.argv) > 1 and os.sys.argv[1] == "health":
        with connection() as conn:
            conn.execute("SELECT 1")
        print("healthy")
    elif len(os.sys.argv) > 1 and os.sys.argv[1] == "backfill-unmapped":
        with connection() as conn:
            before = conn.execute("SELECT count(*) n FROM unmapped_observations WHERE resolved_project_id IS NULL").fetchone()["n"]
            mapped = backfill_unmapped_observations(conn)
            after = conn.execute("SELECT count(*) n FROM unmapped_observations WHERE resolved_project_id IS NULL").fetchone()["n"]
        print(json.dumps({"unmapped_before": before, "mapped": mapped, "unmapped_after": after}))
    else:
        run()
