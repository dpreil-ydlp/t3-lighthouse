"""Conservative idle-parent reminders. Briefs suggest work; they never grant authority."""
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import PurePosixPath

if __package__:
    from .t3_read import read_t3_database
    from .t3_mcp import T3McpClient, T3_TOKEN_FILE, _read_token, _json_request, valid_thread_id
else:
    from t3_read import read_t3_database
    from t3_mcp import T3McpClient, T3_TOKEN_FILE, _read_token, _json_request, valid_thread_id

# Reuse CR9's 30-minute silence/cooldown; never revive old objectives automatically.
SILENCE = 1800
MAX_AGE = 7 * 86400
# A finished substep is not a finished objective. Brief work supplies the next step;
# final text only vetoes continuation at explicit stop/input/authority boundaries.
STOP = re.compile(r"(?i)\b(?:stop(?:ped)?|pause(?:d)?|cancel(?:led|ed)?|wait(?:ing)?|approval|permission|blocked|unavailable|limit|budget|await(?:ing)?|authorization|untested)\b|\b(?:do not|don't)\b|\?|\b(?:everything|objective|task|project|all work)\s+(?:is\s+)?(?:complete(?:d)?|finished|done)\b")


def read_idle_parents():
    """Use the existing bounded, in-place reader. Unknown schema fails closed."""
    with read_t3_database(os.path.expanduser('~')) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
          SELECT t.thread_id, turn.provider_turn_id turn_id, turn.completed_at,
                 item.payload_json, project.workspace_root project_root
          FROM orchestration_v2_projection_threads t
          JOIN projection_threads pt USING(thread_id)
          JOIN projection_projects project ON project.project_id=t.project_id
          JOIN orchestration_v2_projection_provider_turns turn ON turn.thread_id=t.thread_id
          JOIN orchestration_v2_projection_turn_items item
            ON item.thread_id=t.thread_id AND item.provider_turn_id=turn.provider_turn_id
          WHERE t.deleted_at IS NULL AND t.archived_at IS NULL
            AND pt.deleted_at IS NULL AND pt.archived_at IS NULL
            AND pt.settled_at IS NULL AND pt.settled_override IS NULL AND pt.snoozed_until IS NULL
            AND pt.pending_approval_count=0 AND pt.pending_user_input_count=0
            AND (pt.latest_user_message_at IS NULL OR pt.latest_user_message_at<=turn.completed_at)
            AND t.thread_id NOT LIKE 'thread:delegated-task:%'
            AND turn.status='completed' AND turn.completed_at IS NOT NULL
            AND turn.ordinal=(SELECT max(x.ordinal) FROM orchestration_v2_projection_provider_turns x WHERE x.thread_id=t.thread_id)
            AND item.type='assistant_message' AND item.status='completed'
            AND item.ordinal=(SELECT max(x.ordinal) FROM orchestration_v2_projection_turn_items x
              WHERE x.thread_id=t.thread_id AND x.provider_turn_id=turn.provider_turn_id AND x.type='assistant_message')
            AND NOT EXISTS(SELECT 1 FROM orchestration_v2_projection_runtime_requests r
              WHERE r.thread_id=t.thread_id AND r.status='pending')
            AND NOT EXISTS(SELECT 1 FROM orchestration_v2_projection_subagents s
              WHERE s.thread_id=t.thread_id AND s.status='running')
            AND NOT EXISTS(SELECT 1 FROM orchestration_v2_projection_subagents s WHERE s.child_thread_id=t.thread_id)
          ORDER BY turn.completed_at DESC LIMIT 100
        """).fetchall()
        parents = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            # Native children can also carry lineage without a subagent projection row.
            thread = json.loads(conn.execute('SELECT payload_json FROM orchestration_v2_projection_threads WHERE thread_id=?', (row['thread_id'],)).fetchone()[0])
            if (thread.get('lineage') or {}).get('parentThreadId'):
                continue
            parents.append({'thread_id': row['thread_id'], 'turn_id': row['turn_id'],
                            'completed_at': datetime.fromisoformat(row['completed_at'].replace('Z', '+00:00')).timestamp(),
                            'text': payload.get('text', ''), 'project_root': row['project_root']})
        return parents


def deliver_idle_parents(host, api_token, state, checkpoint=None, token_file=T3_TOKEN_FILE,
                         client_factory=T3McpClient, read_parents=read_idle_parents,
                         request=_json_request, now=None):
    """One bounded reminder per completed turn, using CR9 OAuth/send/idempotency controls."""
    if os.environ.get('LIGHTHOUSE_DERIVE_SUBAGENT_TASKS', '1').strip().lower() in {'0', 'false', 'no', 'off'}:
        return 0
    now = time.time() if now is None else now
    try:
        parents = read_parents()
        projects = request(host.rstrip('/') + '/api/v1/projects?view=active', api_token)['items']
        token, expiry = _read_token(token_file)
        if expiry <= now:
            return 0
        sent = state.setdefault('idle_parent_sent', {})
        client = None
        count = 0
        for parent in parents:
            thread_id = parent['thread_id']
            text = parent['text']
            if not valid_thread_id(thread_id) or STOP.search(text):
                continue
            age = now - parent['completed_at']
            if not SILENCE <= age < MAX_AGE:
                continue
            # Match the API producer's normalize_root contract without a server dependency.
            root = (str(PurePosixPath(parent['project_root'])).rstrip('/') or '/').lower()
            matching = [p for p in projects if any(
                root == (str(PurePosixPath(r)).rstrip('/') or '/').lower()
                for r in p.get('roots', []))]
            if len(matching) != 1:
                continue
            brief = matching[0].get('brief') or {}
            left = brief.get('left') or []
            # Any owner/outside/unknown work blocks this deliberately narrow rule.
            if brief.get('state') != 'idle' or not left or any(i.get('who') != 'agents' for i in left):
                continue
            through = brief.get('activity_through')
            if not through or not parent['completed_at'] <= datetime.fromisoformat(through.replace('Z', '+00:00')).timestamp() <= now:
                continue
            # Ignore brief timestamps and estimates: the same work after a nudge is
            # not new work, even when the resumed parent completes another turn.
            work = hashlib.sha256(json.dumps(sorted(i['text'].strip() for i in left),
                ensure_ascii=False).encode()).hexdigest()
            marker = sent.get(thread_id, {})
            if marker.get('work') == work or marker.get('turn_id') == parent['turn_id'] or now - marker.get('at', 0) < SILENCE:
                continue
            if client is None:
                client = client_factory(token)
                client.connect()
            # Re-read source immediately before sending. A changed turn/wait cancels the reminder.
            if parent not in read_parents():
                continue
            message = ('Lighthouse: a fresh project brief records remaining agent work for this idle parent. Review your current task and existing authority. '
                       'Continue only work already authorized and actionable. Preserve all stop decisions, review limits, '
                       'budgets and pending input. An untested item grants no authority. If no authorized work remains, stop.')
            request_id = 'lighthouse-idle-' + hashlib.sha256((thread_id + ':' + parent['turn_id']).encode()).hexdigest()
            client.send(thread_id, message, request_id)
            sent[thread_id] = {'turn_id': parent['turn_id'], 'at': now, 'work': work}
            if checkpoint:
                checkpoint()
            count += 1
        return count
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
        # Like CR9 send failures, retry next poll with the same request identity. Never log private text/token.
        logging.getLogger('lighthouse.collector').warning('idle parent check or delivery failed')
        return 0
