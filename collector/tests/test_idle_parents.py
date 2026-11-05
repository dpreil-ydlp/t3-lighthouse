import json
from collector.idle_parents import deliver_idle_parents


def test_one_nudge_per_unchanged_turn(tmp_path):
    token = tmp_path / 'token'
    token.write_text(json.dumps({'access_token': 'test', 'expires_at': 9999999999}))
    token.chmod(0o600)
    sent = []
    class Client:
        def __init__(self, token): pass
        def connect(self): pass
        def send(self, *args): sent.append(args)
    parent = {'thread_id': 'parent', 'turn_id': 'turn', 'completed_at': 1000,
              'text': 'Next action: Run the focused regression.', 'project_root': '/project'}
    projects = [{'roots': ['/project'], 'brief': {'state': 'idle', 'activity_through': '1970-01-01T00:16:40+00:00',
                 'left': [{'who': 'agents', 'text': 'Run the focused regression.'}]}}]
    state = {}
    run = lambda: deliver_idle_parents('http://api', 'api-token', state, token_file=token,
        client_factory=Client, read_parents=lambda: [parent],
        request=lambda *args: {'items': projects}, now=3000)
    assert run() == 1
    assert run() == 0
    assert len(sent) == 1
    assert 'existing authority' in sent[0][1]


import pytest


@pytest.mark.parametrize('change', [
    {'text': 'Audio delivery remains untested.'},
    {'text': 'The script is accepted. The loop stopped at draft 10. Next action: Test audio.'},
    {'text': 'Next action: Wait for approval.'},
    {'text': 'Do not continue. Next action: Run tests.'},
    {'text': 'Next action: Run tests. Is this authorized?'},
    {'text': 'Everything is complete. Next action: Run tests.'},
    {'text': 'Next action: Run tests. Credentials unavailable.'},
    {'completed_at': 2999}, {'completed_at': -700000},
])
def test_safety_exclusions(tmp_path, change):
    parent = {'thread_id': 'parent', 'turn_id': 'turn', 'completed_at': 1000,
              'text': 'Next action: Run tests.', 'project_root': '/project', **change}
    token = tmp_path / 'token'
    token.write_text(json.dumps({'access_token': 'test', 'expires_at': 9999999999}))
    token.chmod(0o600)
    projects = [{'roots': ['/project'], 'brief': {'state': 'idle', 'activity_through': '1970-01-01T00:16:40Z',
                'left': [{'who': 'agents', 'text': 'Run tests.'}]}}]
    class NoSend:
        def __init__(self, token): pytest.fail('must not connect')
    assert deliver_idle_parents('http://api', 'token', {}, token_file=token, client_factory=NoSend,
        read_parents=lambda: [parent], request=lambda *a: {'items': projects}, now=3000) == 0


@pytest.mark.parametrize('brief', [
    {}, {'state': 'nothing_open', 'left': []},
    {'state': 'idle', 'left': []},
    {'state': 'idle', 'left': [{'who': 'agents'}], 'activity_through': '1970-01-01T01:00:00Z'},
    {'state': 'waiting_on_you', 'left': [{'who': 'you'}]},
    {'state': 'idle', 'left': [{'who': 'outside'}]},
    {'state': 'idle', 'left': [{'who': 'agents'}], 'activity_through': '1970-01-01T00:01:00Z'},
])
def test_brief_is_no_authority(tmp_path, brief):
    parent = {'thread_id': 'parent', 'turn_id': 'turn', 'completed_at': 1000,
              'text': 'Next action: Run tests.', 'project_root': '/project'}
    token = tmp_path / 'token'
    token.write_text(json.dumps({'access_token': 'test', 'expires_at': 9999999999}))
    token.chmod(0o600)
    assert deliver_idle_parents('http://api', 'token', {}, token_file=token,
        read_parents=lambda: [parent], request=lambda *a: {'items': [{'roots': ['/project'], 'brief': brief}]}, now=3000) == 0


def test_changed_source_cancels_send_and_retry_uses_same_identity(tmp_path):
    token = tmp_path / 'token'
    token.write_text(json.dumps({'access_token': 'test', 'expires_at': 9999999999}))
    token.chmod(0o600)
    parent = {'thread_id': 'parent', 'turn_id': 'turn', 'completed_at': 1000,
              'text': 'Next action: Run tests.', 'project_root': '/project'}
    projects = [{'roots': ['/project'], 'brief': {'state': 'idle', 'activity_through': '1970-01-01T00:16:40Z',
                'left': [{'who': 'agents', 'text': 'Run tests.'}]}}]
    sent = []
    fail = [True]
    class Client:
        def __init__(self, token): pass
        def connect(self): pass
        def send(self, parent, message, request_id):
            sent.append(request_id)
            if fail[0]: raise OSError('transport failure')
    reads = iter([[parent], []])
    args = dict(token_file=token, client_factory=Client, request=lambda *a: {'items': projects}, now=3000)
    state = {}
    assert deliver_idle_parents('http://api', 'token', state, read_parents=lambda: next(reads), **args) == 0
    assert sent == []
    assert deliver_idle_parents('http://api', 'token', state, read_parents=lambda: [parent], **args) == 0
    fail[0] = False
    assert deliver_idle_parents('http://api', 'token', state, read_parents=lambda: [parent], **args) == 1
    assert sent[0] == sent[1]
    restored = json.loads(json.dumps(state))
    assert deliver_idle_parents('http://api', 'token', restored, read_parents=lambda: [parent], **args) == 0


@pytest.mark.parametrize('exclusion', ['', 'wait', 'running', 'settled', 'child', 'new_turn', 'runtime_request', 'new_user'])
def test_source_fixture_requires_idle_top_level_parent(tmp_path, monkeypatch, exclusion):
    import sqlite3
    from collector.idle_parents import read_idle_parents
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / '.t3/userdata/statev2.sqlite'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.executescript('''
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT,project_id TEXT,deleted_at TEXT,archived_at TEXT,payload_json TEXT);
        CREATE TABLE projection_threads(thread_id TEXT,deleted_at TEXT,archived_at TEXT,settled_at TEXT,settled_override TEXT,snoozed_until TEXT,pending_approval_count INTEGER,pending_user_input_count INTEGER,latest_user_message_at TEXT);
        CREATE TABLE projection_projects(project_id TEXT,workspace_root TEXT);
        CREATE TABLE orchestration_v2_projection_provider_turns(thread_id TEXT,provider_turn_id TEXT,status TEXT,completed_at TEXT,ordinal INTEGER);
        CREATE TABLE orchestration_v2_projection_turn_items(thread_id TEXT,provider_turn_id TEXT,type TEXT,status TEXT,ordinal INTEGER,payload_json TEXT);
        CREATE TABLE orchestration_v2_projection_runtime_requests(thread_id TEXT,status TEXT);
        CREATE TABLE orchestration_v2_projection_subagents(thread_id TEXT,child_thread_id TEXT,status TEXT);
        INSERT INTO orchestration_v2_projection_threads VALUES('parent','project',NULL,NULL,'{}');
        INSERT INTO projection_threads VALUES('parent',NULL,NULL,NULL,NULL,NULL,0,0,NULL);
        INSERT INTO projection_projects VALUES('project','/project');
        INSERT INTO orchestration_v2_projection_provider_turns VALUES('parent','turn','completed','1970-01-01T00:16:40Z',1);
        INSERT INTO orchestration_v2_projection_turn_items VALUES('parent','turn','assistant_message','completed',1,'{"text":"Next action: Run tests."}');
        ''')
        if exclusion == 'new_user': c.execute("UPDATE projection_threads SET latest_user_message_at='1970-01-01T00:17:00Z'")
        if exclusion == 'wait': c.execute('UPDATE projection_threads SET pending_user_input_count=1')
        if exclusion == 'running': c.execute("UPDATE orchestration_v2_projection_provider_turns SET status='running'")
        if exclusion == 'settled': c.execute("UPDATE projection_threads SET settled_at='now'")
        if exclusion == 'child': c.execute("INSERT INTO orchestration_v2_projection_subagents VALUES('other','parent','completed')")
        if exclusion == 'new_turn': c.execute("INSERT INTO orchestration_v2_projection_provider_turns VALUES('parent','new','running',NULL,2)")
        if exclusion == 'runtime_request': c.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES('parent','pending')")
    parents = read_idle_parents()
    assert len(parents) == (0 if exclusion else 1)
    if parents:
        assert parents[0]['turn_id'] == 'turn'
        assert parents[0]['text'] == 'Next action: Run tests.'

@pytest.mark.parametrize('text', [
    'Finished the draft. Next action: Run the authorized audio test.',
    'Next, run the authorized audio test.',
])
def test_brief_work_without_exact_label(tmp_path, text):
    token = tmp_path / 'token'
    token.write_text(json.dumps({'access_token': 'test', 'expires_at': 9999999999}))
    token.chmod(0o600)
    parent = {'thread_id': 'parent', 'turn_id': 'turn', 'completed_at': 1000,
              'text': text, 'project_root': '/project'}
    projects = [{'roots': ['/project'], 'brief': {'state': 'idle', 'activity_through': '1970-01-01T00:16:40Z',
                'left': [{'who': 'agents', 'text': 'Run the authorized audio test.'}]}}]
    sends = []
    class Client:
        def __init__(self, token): pass
        def connect(self): pass
        def send(self, *args): sends.append(args)
    state = {}
    def run(now):
        return deliver_idle_parents('http://api', 'token', state, token_file=token,
            client_factory=Client, read_parents=lambda: [parent], request=lambda *a: {'items': projects}, now=now)
    assert run(3000) == 1
    assert run(3000) == 0
    # A resumed turn repeating the same unfinished work must not sustain a nudge loop.
    parent.update(turn_id='resumed-turn', completed_at=4000)
    projects[0]['brief']['activity_through'] = '1970-01-01T01:06:40Z'
    state = json.loads(json.dumps(state))
    assert run(6000) == 0
    assert len(sends) == 1


@pytest.mark.parametrize('ambiguous', [False, True])
def test_producer_normalized_roots(tmp_path, ambiguous):
    from lighthouse.project_discovery import normalize_root
    token = tmp_path / 'token'
    token.write_text(json.dumps({'access_token': 'test', 'expires_at': 9999999999}))
    token.chmod(0o600)
    root = '/Users/David/Projects/YouTube/'
    parent = {'thread_id': 'parent', 'turn_id': 'turn', 'completed_at': 1000,
              'text': 'Next action: Run tests.', 'project_root': root}
    project = {'roots': [normalize_root(root)], 'brief': {'state': 'idle',
               'activity_through': '1970-01-01T00:16:40Z', 'left': [{'who': 'agents', 'text': 'Run tests.'}]}}
    projects = [project, dict(project)] if ambiguous else [project]
    sends = []
    class Client:
        def __init__(self, token): pass
        def connect(self): pass
        def send(self, *args): sends.append(args)
    assert deliver_idle_parents('http://api', 'token', {}, token_file=token,
        client_factory=Client, read_parents=lambda: [parent], request=lambda *a: {'items': projects}, now=3000) == (0 if ambiguous else 1)
    assert len(sends) == (0 if ambiguous else 1)
