import json
import os
import subprocess
from pathlib import Path
import pytest

from collector import briefs


BRIEF = {
    "about": "You use the app to review project work.",
    "now": "You must review the first release wording.",
    "done": ["The first flow is ready."],
    "left": [
        {"text": "Review the release wording.", "who": "you"},
        {"text": "The agents must check the release.", "who": "agents", "hours": 4},
    ],
    "eta_basis": "Four active hours remain, based on the current work list.",
}


def test_redaction_removes_common_credentials_and_private_keys():
    source = "Bearer abcdefghijklmnop sk-live_1234567890 ghp_abcdefghijklmnopqrstuvwxyz012345 " \
             "https://example.test/?access_token=supersecret " \
             "-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----"
    clean = briefs.redact_text(source)
    assert "abcdefghijklmnop" not in clean
    assert "sk-live_1234567890" not in clean
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in clean
    assert "access_token=supersecret" not in clean
    assert "secret material" not in clean


def test_extract_only_user_assistant_text_and_skip_tools_thinking_and_results():
    assert briefs.extract_text({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Public summary"},
        {"type": "thinking", "thinking": "private reasoning"},
        {"type": "tool_use", "name": "read_file", "input": {"path": "/secret"}},
    ]}}, "claude_code") == ["Public summary"]
    assert briefs.extract_text({"type": "event_msg", "payload": {"type": "tool_result", "message": "file body"}}, "codex") == []
    assert briefs.extract_text({"type": "event_msg", "payload": {"type": "user_message", "message": "New request"}}, "codex") == ["New request"]


def test_work_dir_codex_session_is_ignored(tmp_path):
    assert briefs.is_briefs_work_session({"cwd": str(tmp_path / ".config/lighthouse/briefs-work")}, tmp_path)
    assert not briefs.is_briefs_work_session({"cwd": str(tmp_path / "Projects/Lighthouse")}, tmp_path)


def test_work_dir_claude_session_is_ignored(tmp_path):
    assert briefs.is_briefs_work_session({"cwd": str(tmp_path / ".config/lighthouse/briefs-work/run")}, tmp_path)
    assert not briefs.is_briefs_work_session({"cwd": str(tmp_path / "Projects/Lighthouse")}, tmp_path)


def test_ste_check_counts_abbreviations_and_currency_decimals():
    brief = BRIEF.copy()
    brief["about"] = "You check e.g. the test plan. The agents track $1,250.40 in hosting costs."
    brief["now"] = "You must review the release wording before the app publishes the recipe to the readers."
    assert briefs.ste_check(brief) == []


def test_ste_check_allows_thirty_word_now_but_keeps_other_fields_within_ste():
    brief = BRIEF.copy()
    brief["now"] = "The agents finished the recipe search page, the dashboard now shows the new status, and your review is still needed before the release can reach users this week from you."
    assert briefs._word_count(brief["now"]) == 30
    assert briefs.ste_check(brief) == []


def test_ste_check_still_rejects_a_twenty_one_word_done_item():
    brief = BRIEF.copy()
    brief["done"] = ["The agents finished the recipe search page and checked every screen before they sent the release to you for review today."]
    assert briefs._word_count(brief["done"][0]) == 21
    assert briefs.ste_check(brief) == [brief["done"][0]]


def test_sentences_keep_wrapped_text_and_abbreviations_together():
    text = "You check e.g. the U.S. plan and read i.e. the notes from Dr. Lee vs. Mr. Cole.\nThe agents track 2.5 units and $1,250.40; they report results."
    assert briefs._sentences(text) == [
        "You check e.g. the U.S. plan and read i.e. the notes from Dr. Lee vs. Mr. Cole.",
        "The agents track 2.5 units and $1,250.40; they report results.",
    ]


def test_ste_check_checks_done_and_left_items_independently():
    brief = BRIEF.copy()
    brief["done"] = [
        "The agents review the saved form before the app sends it to you today.",
        "You check the recipe and confirm that the photo reached the correct album.",
    ]
    brief["left"] = [
        {"text": "Review the saved address before the release.", "who": "you"},
        {"text": "The agents must finish the scheduled account check.", "who": "agents", "hours": None},
    ]
    assert all(briefs._word_count(item if isinstance(item, str) else item["text"]) <= 20 for item in brief["done"] + brief["left"])
    assert briefs.ste_check(brief) == []


@pytest.mark.parametrize("item", [
    {"text": "You must review the release."},
    {"text": "You must review the release.", "who": "you", "hours": None},
    {"text": "The agents check the release.", "who": "agents"},
    {"text": "The agents check the release.", "who": "agents", "hours": float("inf")},
    {"text": "The agents check the release.", "who": "agents", "hours": float("nan")},
])
def test_typed_left_items_reject_invalid_owner_and_agent_fields(item):
    brief = BRIEF | {"left": [item]}
    with pytest.raises(ValueError):
        briefs.validate_brief(brief)


def test_typed_left_items_require_finite_nonnegative_agent_hours():
    for hours in (-1, True):
        brief = BRIEF | {"left": [{"text": "The agents check the release.", "who": "agents", "hours": hours}]}
        with pytest.raises(ValueError):
            briefs.validate_brief(brief)


@pytest.mark.parametrize("text", ["You approve the release.", "You must approve the release.", "The owner approves the release."])
def test_owner_items_must_be_imperative_without_you_subject(text):
    with pytest.raises(ValueError, match="owner_item_imperative"):
        briefs.validate_brief(BRIEF | {"left": [{"text": text, "who": "you"}]})


@pytest.mark.parametrize("verb", ["decide", "mark", "pay", "read", "reconcile"])
def test_owner_actions_accept_imperative_verbs_outside_old_allowlist(verb):
    briefs.validate_brief(BRIEF | {"left": [{"text": f"{verb.title()} the open item.", "who": "you"}]})


def test_owner_item_imperative_prompt_and_agent_prefix_are_explicit():
    prompt = briefs.make_prompt({}, None, [], [], "")
    assert "ordinary English verbs, including decide, mark, pay, or read" in prompt
    assert "Do not start with 'You', 'You must'" in prompt
    assert "Keep agent items starting with 'The agents'" in prompt


@pytest.mark.parametrize("now", [
    "The project is now finished.",
    "Everything is now complete.",
    "The project is fully done.",
    "All done.",
])
def test_finished_claim_is_rejected_when_open_items_remain(now):
    brief = BRIEF | {"now": now}
    with pytest.raises(ValueError, match="finished_claim_open_items"):
        briefs.validate_brief(brief)


def test_valid_subwork_completion_is_allowed_with_open_items():
    briefs.validate_brief(BRIEF | {"done": ["The agents finished the settings screen."]})


def test_completed_upload_flows_are_valid_subwork_with_open_items():
    briefs.validate_brief(BRIEF | {"done": ["The agents checked all completed upload flows."]})


def test_brief_schema_requests_typed_left_items_and_omits_eta_fields():
    variants = briefs.BRIEF_SCHEMA["properties"]["left"]["items"]["oneOf"]
    assert len(variants) == 3
    assert variants[0]["properties"]["who"]["enum"] == ["you"]
    assert "hours" not in variants[0]["properties"]
    assert variants[2]["required"] == ["text", "who", "hours"]
    assert "remaining_hours" not in briefs.BRIEF_SCHEMA["properties"]
    assert "eta_confidence" not in briefs.BRIEF_SCHEMA["properties"]


def test_left_item_cap_keeps_every_owner_item_first():
    items = [{"text": f"Owner action {i}.", "who": "you"} for i in range(9)]
    items += [{"text": f"Agent item {i}.", "who": "agents", "hours": 1} for i in range(4)]
    selected = briefs.prioritize_left_items(items)
    assert selected == items[:9]


def test_previous_typed_items_redact_nested_item_text():
    prior = {"left": [{"text": "You use token=secret-value to review.", "who": "you"}]}
    projected = briefs._previous_brief(prior)
    assert "secret-value" not in json.dumps(projected)
    assert projected["left"][0]["who"] == "you"


def test_ste_retry_rejects_wrapped_21_word_done_item_and_keeps_cursors(tmp_path, monkeypatch, capsys):
    remote, requests, state = _brief_run_fixture(tmp_path, monkeypatch)
    wrapped = "You check the form and the photo and the recipe and\nthe tag and the album and the saved list today."
    assert briefs._word_count(" ".join(briefs._sentences(wrapped))) == 21
    cursor_state = {
        "files": {
            str(tmp_path / "home/.claude/projects/session.jsonl"): {
                "offset": 0,
                "context": {"cwd": str(tmp_path / "home/Projects/Sample")},
            },
        },
        "projects": {},
    }
    state.write_text(json.dumps(cursor_state))
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        result = BRIEF.copy()
        result["done"] = [wrapped]
        return result

    monkeypatch.setattr(briefs, "run_model", model)
    result = briefs.run_once()
    output = capsys.readouterr().out
    assert result["updated"] == 0 and result["model_calls"] == 3
    assert len(prompts) == 3
    assert "You check the form" in prompts[1]
    assert output.count("ste_reject project=sample") == 1
    assert remote["now"] == BRIEF["now"]
    assert not any(path.endswith("/brief") for path, _ in requests)
    saved = json.loads(state.read_text())
    assert saved["files"] == cursor_state["files"]
    assert saved["projects"]["p1"]["consecutive_rejects"] == 1


@pytest.mark.parametrize("control", [
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
])
def test_run_model_excludes_provider_and_auth_environment_controls(tmp_path, monkeypatch, control):
    work = tmp_path / "briefs-work"
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"structured_output": BRIEF})
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return Result()

    monkeypatch.setattr(briefs, "WORK_DIR", work)
    monkeypatch.setattr(briefs.subprocess, "run", fake_run)
    monkeypatch.setenv(control, "synthetic-control-value")
    briefs.run_model("prompt")
    assert control not in calls[0]["env"]
    assert set(calls[0]["env"]) <= {
        "HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TERM",
    }
    assert calls[0]["env"]["PATH"] == f"{briefs.CLAUDE.rsplit('/', 1)[0]}:/usr/bin:/bin"
    assert calls[0]["env"]["TERM"] == "dumb"


@pytest.mark.parametrize("now", [
    "The team finishes the remaining work.",
    "You must complete the remaining product work.",
    "The agents continue work.",
    "You make progress on the project.",
    "You must finish the work.",
])
def test_ste_check_rejects_vague_now_lines(now):
    brief = BRIEF.copy()
    brief["now"] = now
    assert briefs.ste_check(brief) == [now]


def test_ste_check_rejects_generic_phrases_outside_now():
    brief = BRIEF.copy()
    brief["about"] = "You must continue work on the project."
    assert briefs.ste_check(brief) == [brief["about"]]


@pytest.mark.parametrize("field,value", [
    ("about", "The owner reviews the project."),
    ("now", "The TEAM reviews the project."),
    ("done", ["The Owner approved the wording."]),
    ("left", ["The team must review the recipe."]),
])
def test_ste_check_rejects_owner_and_team_actor_terms(field, value):
    brief = BRIEF.copy()
    brief[field] = value
    assert briefs.ste_check(brief) == [value[0] if isinstance(value, list) else value]


def test_run_model_uses_claude_subscription_flags_and_unsets_api_keys(tmp_path, monkeypatch):
    work = tmp_path / ".config/lighthouse/briefs-work"
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({"structured_output": BRIEF})
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(briefs, "WORK_DIR", work)
    monkeypatch.setattr(briefs.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-leak-either")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://must-not-route.invalid")
    assert briefs.run_model("prompt") == BRIEF
    command, kwargs = calls[0]
    assert command[:6] == [briefs.CLAUDE, "-p", "--model", "claude-haiku-5-5", "--effort", "medium"]
    assert "--no-session-persistence" in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--setting-sources") + 1] == "project,local"
    assert "--tools" in command and command[command.index("--tools") + 1] == ""
    assert kwargs["cwd"] == work
    assert kwargs["input"] == "prompt"
    assert kwargs["timeout"] == briefs.CALL_TIMEOUT == 180
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]
    assert "ANTHROPIC_BASE_URL" not in kwargs["env"]


def test_stored_cursor_reads_all_complete_appended_records_and_waits_for_newline(tmp_path):
    transcript = tmp_path / "session.jsonl"
    first = json.dumps({"type": "user", "message": {"content": "old"}}).encode() + b"\n"
    transcript.write_bytes(first)

    transcript.write_bytes(first + json.dumps({"type": "user", "message": {"content": "one"}}).encode() + b"\n")
    lines, cursor = briefs.read_session(transcript, "claude_code", len(first), {})
    assert [text for _, text in lines] == ["one"]

    second = json.dumps({"type": "user", "message": {"content": "two"}}).encode() + b"\n"
    third = json.dumps({"type": "user", "message": {"content": "three"}}).encode() + b"\n"
    transcript.write_bytes(transcript.read_bytes() + second + third)
    lines, cursor = briefs.read_session(transcript, "claude_code", cursor, {})
    assert [text for _, text in lines] == ["two", "three"]

    partial = json.dumps({"type": "user", "message": {"content": "completed later"}}).encode()
    transcript.write_bytes(transcript.read_bytes() + partial)
    lines, pending_cursor = briefs.read_session(transcript, "claude_code", cursor, {})
    assert lines == []
    assert pending_cursor == cursor

    transcript.write_bytes(transcript.read_bytes() + b"\n")
    lines, complete_cursor = briefs.read_session(transcript, "claude_code", pending_cursor, {})
    assert [text for _, text in lines] == ["completed later"]
    assert complete_cursor == transcript.stat().st_size


def test_session_credentials_are_redacted_before_prompt_for_json_and_key_value_forms(tmp_path):
    transcript = tmp_path / "session.jsonl"
    messages = [
        '{"token": "json-token-secret-123", "password": "synthetic-password-123"}',
        r'{\"access_token\": \"escaped-token-secret-123\", \"client_secret\": \"escaped-secret-123\"}',
        "api_key=plain-key-secret-123 passwd: plain-pass-secret-123 "  # gitleaks:allow (synthetic fixture)
        "authorization: plain-auth-secret-123 cookie=plain-cookie-secret-123",
        "session=plain-session-secret-123 refresh_token: plain-refresh-secret-123 "
        "lh_0123456789abcdef0123456789abcdef",
    ]
    transcript.write_bytes(b"".join(
        (json.dumps({"type": "user", "message": {"content": message}}) + "\n").encode()
        for message in messages
    ))
    extracted, _ = briefs.read_session(transcript, "claude_code", None, {})
    prompt = briefs.make_prompt({"name": "Sample"}, None, [], [], "\n".join(text for _, text in extracted))

    for secret in (
        "json-token-secret-123", "synthetic-password-123", "escaped-token-secret-123",
        "escaped-secret-123", "plain-key-secret-123", "plain-pass-secret-123",  # gitleaks:allow (synthetic fixture)
        "plain-auth-secret-123", "plain-cookie-secret-123", "plain-session-secret-123",
        "plain-refresh-secret-123", "lh_0123456789abcdef0123456789abcdef",  # gitleaks:allow (synthetic fixture)
    ):
        assert secret not in prompt


@pytest.mark.parametrize("source", ["claude_code", "codex"])
@pytest.mark.parametrize("message,leaked_values", [
    ('{"password": "sample\'visible-password-suffix"}', ("visible-password-suffix",)),
    (json.dumps({"password": "sample'visible-password-suffix"}).replace('"', r'\"'), ("visible-password-suffix",)),
    (json.dumps({"password": 'value with escaped " quote and visible-escaped-suffix'}), ("visible-escaped-suffix",)),
    ("password: 'value with a \"double quote\" and visible-single-suffix'", ("visible-single-suffix",)),
    ("password=plain-visible-suffix,api_key=plain-comma-suffix&token=plain-amp-suffix next", ("plain-visible-suffix", "plain-comma-suffix", "plain-amp-suffix")),
])
def test_quoted_credentials_are_fully_redacted_from_prompts(tmp_path, source, message, leaked_values):
    transcript = tmp_path / f"{source}.jsonl"
    if source == "claude_code":
        record = {"type": "user", "message": {"content": message}}
    else:
        record = {"type": "event_msg", "payload": {"type": "user_message", "message": message}}
    transcript.write_text(json.dumps(record) + "\n")
    extracted, _ = briefs.read_session(transcript, source, None, {})
    prompt = briefs.make_prompt({"name": "Sample"}, None, [], [], "\n".join(text for _, text in extracted))
    for leaked in leaked_values:
        assert leaked not in prompt


@pytest.mark.parametrize("source", ["claude_code", "codex"])
@pytest.mark.parametrize("encoding", ["plain", "escaped_quotes", "escaped_json"])
def test_nested_json_password_suffix_is_removed_from_prompt(tmp_path, source, encoding):
    message = json.dumps({"password": 'sample",VISIBLE-PASSWORD-SUFFIX'})
    if encoding == "escaped_quotes":
        message = message.replace('"', r'\"')
    elif encoding == "escaped_json":
        message = json.dumps(message)[1:-1]
    if source == "claude_code":
        record = {"type": "user", "message": {"content": message}}
    else:
        record = {"type": "event_msg", "payload": {"type": "user_message", "message": message}}
    transcript = tmp_path / f"{source}-{encoding}.jsonl"
    transcript.write_text(json.dumps(record) + "\n")

    extracted, _ = briefs.read_session(transcript, source, None, {})
    prompt = briefs.make_prompt({"name": "Sample"}, None, [], [], "\n".join(text for _, text in extracted))
    assert "VISIBLE-PASSWORD-SUFFIX" not in prompt


def test_secret_field_redaction_stops_before_next_line_and_handles_multiple_fields():
    value = "password=first-secret api_key=second-secret\nnormal next-line text"
    clean = briefs.redact_text(value)
    assert "first-secret" not in clean
    assert "second-secret" not in clean
    assert "normal next-line text" in clean


@pytest.mark.parametrize("boundary", ["\n", "\r", r"\n", r"\r"])
def test_secret_field_redaction_stops_at_escaped_line_boundaries(boundary):
    clean = briefs.redact_text(f"password=first-secret{boundary}normal next-line text")
    assert "first-secret" not in clean
    assert "normal next-line text" in clean


@pytest.mark.parametrize("surface", ["input_prompt", "output_api_payload"])
def test_nested_json_password_suffix_does_not_reach_run_once(tmp_path, monkeypatch, surface):
    home = tmp_path / "home"
    root = home / "Projects" / "Sample"
    root.mkdir(parents=True)
    transcript = home / ".claude/projects/session.jsonl"
    transcript.parent.mkdir(parents=True)
    message = json.dumps(json.dumps({"password": 'sample",VISIBLE-PASSWORD-SUFFIX'}))[1:-1]
    transcript.write_text(json.dumps({"type": "user", "cwd": str(root), "message": {"content": message}}) + "\n")
    config = home / ".config/lighthouse"
    token_file = config / "secrets/token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("synthetic-token")
    token_file.chmod(0o600)
    project = {"id": "project", "name": "Sample", "roots": [str(root)], "brief": None}
    stored = {}
    prompts = []

    def fake_api(path, token, payload=None, method=None):
        if path == "/api/v1/projects?view=active":
            return {"items": [{**project, "brief": None}]}
        if path.endswith("/brief"):
            stored.update(payload)
            return {"brief": stored}
        if path == "/api/v1/projects/project":
            return {"project": {"brief": stored}}
        raise AssertionError(path)

    def fake_model(prompt):
        prompts.append(prompt)
        result = BRIEF.copy()
        if surface == "output_api_payload":
            result["now"] = message
        return result

    monkeypatch.setattr(briefs, "TOKEN_FILE", token_file)
    monkeypatch.setattr(briefs, "STATE_FILE", config / "state.json")
    monkeypatch.setattr(briefs, "CONFIG_DIR", config)
    monkeypatch.setattr(briefs, "CONTEXT_DIR", config / "context")
    monkeypatch.setattr(briefs, "WORK_DIR", config / "work")
    monkeypatch.setattr(briefs.Path, "home", lambda: home)
    monkeypatch.setattr(briefs.glob, "glob", lambda pattern, recursive, include_hidden: [str(transcript)] if ".claude" in pattern else [])
    monkeypatch.setattr(briefs, "api_request", fake_api)
    monkeypatch.setattr(briefs, "git_activity", lambda *args: [])
    monkeypatch.setattr(briefs, "run_model", fake_model)

    assert briefs.run_once()["updated"] == 1
    observed = "\n".join(prompts) if surface == "input_prompt" else json.dumps(stored)
    assert "VISIBLE-PASSWORD-SUFFIX" not in observed


def test_secret_fields_redact_through_end_of_line():
    value = "password=visible-secret next,tail&more"
    assert briefs.redact_text(value) == "password=[REDACTED]"


def test_prompt_describes_now_as_present_state_and_strips_leading_as_of_date():
    prompt = briefs.make_prompt({"name": "Sample"}, None, [], [], "recent work")
    assert "now: 1–2 full, natural sentences (≤240 characters)" in prompt
    assert "what is happening right now, what just finished, and what it is waiting on or blocked by" in prompt
    assert "The recipe search is live and tested" in prompt
    assert "The shopping list export is updated and checked" in prompt
    assert "The agents rebuilt the welcome tour" in prompt
    assert "without dates" not in prompt
    assert briefs.clean_now("As of October 25, 2026, the owner reviews the release.") == "The owner reviews the release."
    assert briefs.clean_now("The owner reviews the release.") == "The owner reviews the release."


@pytest.mark.parametrize("value", [
    "As of October 25, the product is ready.",
    "As of Oct 25, 2026, the product is ready.",
    "As of 2026-10-25, the product is ready.",
    "As of 25 October 2026, the product is ready.",
    "As of today, the product is ready.",
    "As of yesterday, the product is ready.",
    "As of this morning, the product is ready.",
])
def test_clean_now_strips_only_recognized_date_prefixes(value):
    assert briefs.clean_now(value) == "The product is ready."


def test_clean_now_keeps_non_date_as_of_and_preserves_existing_capitalization():
    value = "As of now deployment is blocked, though all tests pass."
    assert briefs.clean_now(value) == value
    assert briefs.clean_now("iOS app shipped") == "iOS app shipped"
    assert briefs.clean_now("As of today, iOS app shipped") == "iOS app shipped"


def test_nested_project_cursor_waits_for_its_own_brief_put(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    parent_root = home / "Projects" / "Parent"
    child_root = parent_root / "Child"
    child_root.mkdir(parents=True)
    transcript = home / ".claude/projects/nested.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "cwd": str(child_root), "message": {"content": "CHILD_ONLY_ACTIVITY"}}) + "\n")
    config = home / ".config/lighthouse"
    token_file = config / "secrets/token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("test-token")
    token_file.chmod(0o600)
    state_file = config / "briefs-state.json"
    state_file.write_text(json.dumps({"files": {str(transcript): {"offset": 0, "context": {"cwd": str(child_root)}}}, "projects": {}}))
    parent = {"id": "parent", "name": "Parent", "roots": [str(parent_root)], "last_activity_at": "2026-10-25T12:00:00Z", "brief": None}
    child = {"id": "child", "name": "Child", "roots": [str(child_root)], "last_activity_at": "2026-10-25T11:00:00Z", "brief": None}
    stored = {}
    model_prompts = []
    child_failed = False

    def fake_api(path, token, payload=None, method=None):
        assert token == "test-token"
        if path == "/api/v1/projects?view=active":
            return {"items": [
                {**parent, "brief": stored.get("parent")},
                {**child, "brief": stored.get("child")},
            ]}
        if path.endswith("/brief"):
            pid = path.split("/")[-2]
            stored[pid] = {**payload, "updated_at": "2026-10-25T12:01:00Z"}
            return {"brief": stored[pid]}
        if path in ("/api/v1/projects/parent", "/api/v1/projects/child"):
            return {"project": {"brief": stored.get(path.split("/")[-1])}}
        raise AssertionError(path)

    def fake_model(prompt):
        nonlocal child_failed
        model_prompts.append(prompt)
        if "CHILD_ONLY_ACTIVITY" in prompt and not child_failed:
            child_failed = True
            raise RuntimeError("synthetic child model failure")
        return BRIEF.copy()

    monkeypatch.setattr(briefs, "TOKEN_FILE", token_file)
    monkeypatch.setattr(briefs, "STATE_FILE", state_file)
    monkeypatch.setattr(briefs, "CONFIG_DIR", config)
    monkeypatch.setattr(briefs, "CONTEXT_DIR", config / "context")
    monkeypatch.setattr(briefs, "WORK_DIR", config / "work")
    monkeypatch.setattr(briefs.Path, "home", lambda: home)
    monkeypatch.setattr(briefs.glob, "glob", lambda pattern, recursive, include_hidden: [str(transcript)] if ".claude" in pattern else [])
    monkeypatch.setattr(briefs, "api_request", fake_api)
    monkeypatch.setattr(briefs, "run_model", fake_model)

    first = briefs.run_once()
    first_offset = json.loads(state_file.read_text())["files"][str(transcript)]["offset"]
    second = briefs.run_once()
    second_offset = json.loads(state_file.read_text())["files"][str(transcript)]["offset"]
    capsys.readouterr()

    assert first["updated"] == 1
    assert first_offset == 0
    assert second["updated"] == 1
    assert second_offset == transcript.stat().st_size
    assert sum("CHILD_ONLY_ACTIVITY" in prompt for prompt in model_prompts) == 2


def test_git_activity_reads_commits_since_previous_brief(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    for message, stamp in (("old commit", "2026-01-01T00:00:00+00:00"), ("new commit", "2026-02-01T00:00:00+00:00")):
        (tmp_path / "tracked.txt").write_text(message)
        subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
        env = os.environ | {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", message], check=True, capture_output=True, env=env)
    commits = briefs.git_activity(tmp_path, {"activity_through": "2026-01-15T00:00:00+00:00"})
    assert len(commits) == 1 and commits[0].endswith("new commit")


def test_git_activity_cursor_skips_covered_commit_and_keeps_same_second_commit(tmp_path, monkeypatch, capsys):
    real_git_activity = briefs.git_activity
    remote, _requests, state = _brief_run_fixture(tmp_path, monkeypatch, transcript=False)
    root = tmp_path / "home/Projects/Sample"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    first_stamp = "2026-10-25T10:00:00+00:00"

    def commit(message):
        (root / "tracked.txt").write_text(message)
        subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
        env = os.environ | {"GIT_AUTHOR_DATE": first_stamp, "GIT_COMMITTER_DATE": first_stamp}
        subprocess.run(["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True, env=env)

    commit("covered commit")
    monkeypatch.setattr(briefs, "git_activity", real_git_activity)
    remote["activity_through"] = "2026-10-25T09:59:00+00:00"
    calls = []
    monkeypatch.setattr(briefs, "run_model", lambda prompt: calls.append(prompt) or BRIEF.copy())

    first = briefs.run_once()
    capsys.readouterr()
    unchanged = briefs.run_once()
    capsys.readouterr()
    commit("new commit with covered timestamp")
    new_commit = briefs.run_once()
    capsys.readouterr()

    assert [first["model_calls"], unchanged["model_calls"], new_commit["model_calls"]] == [1, 0, 1]
    assert len(calls) == 2
    assert "new commit with covered timestamp" in calls[-1]
    assert json.loads(state.read_text())["projects"]["p1"]["activity_commit"] == subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_commit_after_head_capture_is_left_for_the_next_prompt(tmp_path, monkeypatch, capsys):
    real_git_activity = briefs.git_activity
    remote, _, state = _brief_run_fixture(tmp_path, monkeypatch, transcript=False)
    root = tmp_path / "home/Projects/Sample"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)

    def commit(message):
        (root / "tracked.txt").write_text(message)
        subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True)

    commit("captured head")
    remote["activity_through"] = "2026-10-25T09:59:00+00:00"
    captured_head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    prompts = []
    created_after_capture = False

    def activity_with_late_commit(*args):
        nonlocal created_after_capture
        commits = real_git_activity(*args)
        if not created_after_capture:
            created_after_capture = True
            commit("created after head capture")
        return commits

    monkeypatch.setattr(briefs, "git_activity", activity_with_late_commit)
    monkeypatch.setattr(briefs, "run_model", lambda prompt: prompts.append(prompt) or BRIEF.copy())
    first = briefs.run_once()
    capsys.readouterr()
    saved = json.loads(state.read_text())["projects"]["p1"]["activity_commit"]
    second = briefs.run_once()
    capsys.readouterr()

    assert first["updated"] == second["updated"] == 1
    assert saved == captured_head
    assert "created after head capture" not in prompts[0]
    assert "created after head capture" in prompts[1]


def test_reject_counts_and_fingerprints_stay_with_each_project(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    config = home / ".config/lighthouse"
    token = config / "secrets/token"
    token.parent.mkdir(parents=True)
    token.write_text("test-token")
    token.chmod(0o600)
    state = config / "briefs-state.json"
    state.write_text(json.dumps({"files": {}, "projects": {}}))
    projects = []
    remotes = {}
    paths = []
    for index in range(5):
        pid = f"p{index + 1}"
        root = home / "Projects" / pid
        root.mkdir(parents=True)
        old = {**BRIEF, "updated_at": "2026-10-25T10:00:00+00:00", "activity_through": "2026-10-25T10:00:00+00:00"}
        remotes[pid] = old.copy()
        projects.append({"id": pid, "name": pid, "roots": [str(root)], "brief": {"now": old["now"]}})
        if index < 4:
            session = home / ".claude/projects" / f"{pid}.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_text(json.dumps({"type": "user", "cwd": str(root), "timestamp": "2026-10-26T10:00:00Z",
                                          "message": {"content": f"New work for {pid}."}}) + "\n")
            paths.append(str(session))

    def fake_api(path, bearer, payload=None, method=None):
        if path == "/api/v1/projects?view=active":
            return {"items": projects}
        if path.startswith("/api/v1/projects/") and path.endswith("/brief"):
            pid = path.split("/")[4]
            remotes[pid].update(payload)
            return {"brief": remotes[pid]}
        if path.startswith("/api/v1/projects/"):
            pid = path.split("/")[4]
            return {"project": {"brief": remotes[pid]}}
        raise AssertionError(path)

    monkeypatch.setattr(briefs, "TOKEN_FILE", token)
    monkeypatch.setattr(briefs, "CONFIG_DIR", config)
    monkeypatch.setattr(briefs, "STATE_FILE", state)
    monkeypatch.setattr(briefs, "CONTEXT_DIR", config / "context")
    monkeypatch.setattr(briefs, "WORK_DIR", config / "briefs-work")
    monkeypatch.setattr(briefs.Path, "home", lambda: home)
    monkeypatch.setattr(briefs.glob, "glob", lambda pattern, recursive, include_hidden: paths if ".claude" in pattern else [])
    monkeypatch.setattr(briefs, "api_request", fake_api)
    monkeypatch.setattr(briefs, "git_activity", lambda *args: [])
    monkeypatch.setattr(briefs, "run_model", lambda prompt: BRIEF | {"now": "The agents continue work."})

    result = briefs.run_once()
    capsys.readouterr()
    saved = json.loads(state.read_text())["projects"]

    assert result["model_calls"] == 12
    assert [saved[f"p{i}"]["consecutive_rejects"] for i in range(1, 5)] == [1, 1, 1, 1]
    assert "consecutive_rejects" not in saved["p5"]
    assert len({saved[f"p{i}"]["rejected_activity"] for i in range(1, 5)}) == 4


def test_incremental_second_run_makes_zero_model_calls_and_writes_only_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    project_root = home / "Projects" / "Sample"
    project_root.mkdir(parents=True)
    (project_root / "README.md").write_text("A test project.")
    transcript = home / ".claude/projects/session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "cwd": str(project_root), "sessionId": "s1", "timestamp": "2026-10-25T10:00:00Z", "message": {"content": "Prepare a simple first release."}}) + "\n")
    transcript_before = transcript.read_bytes()

    config = home / ".config/lighthouse"
    token_file = config / "secrets/agent-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("test-token")
    token_file.chmod(0o600)
    codex_session = home / ".codex/sessions/briefs-run.jsonl"
    codex_session.parent.mkdir(parents=True)
    codex_session.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(config / "briefs-work")}}) + "\n" +
                             json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "FAKE_WORKDIR_ACTIVITY"}}) + "\n")
    project = {"id": "project-1", "name": "Sample", "roots": [str(project_root)], "brief": None,
               "last_activity_at": "2026-10-25T10:00:00+00:00"}
    remote_brief = {}
    calls = []

    def fake_api(path, token, payload=None, method=None):
        assert token == "test-token"
        if path == "/api/v1/projects?view=active":
            compact = None
            if remote_brief:
                compact = {key: remote_brief[key] for key in ("now", "updated_at", "left")}
            return {"items": [{**project, "brief": compact}]}
        if path == "/api/v1/projects/project-1" and payload is None:
            return {"project": {"brief": remote_brief.copy()} if remote_brief else {"brief": None}}
        if path == "/api/v1/projects/project-1/brief":
            remote_brief.update(payload)
            remote_brief.update({"updated_at": "2026-10-25T10:01:00+00:00"})
            return {"brief": remote_brief.copy()}
        raise AssertionError(path)

    monkeypatch.setattr(briefs, "HOST", "http://127.0.0.1:51821")
    monkeypatch.setattr(briefs, "TOKEN_FILE", token_file)
    monkeypatch.setattr(briefs, "CONFIG_DIR", config)
    monkeypatch.setattr(briefs, "STATE_FILE", config / "briefs-state.json")
    monkeypatch.setattr(briefs, "CONTEXT_DIR", config / "context")
    monkeypatch.setattr(briefs, "WORK_DIR", config / "briefs-work")
    monkeypatch.setattr(briefs.Path, "home", lambda: home)
    monkeypatch.setattr(briefs.glob, "glob", lambda pattern, recursive, include_hidden: [str(transcript)] if ".claude" in pattern else [str(codex_session)] if ".codex" in pattern else [])
    monkeypatch.setattr(briefs, "api_request", fake_api)
    monkeypatch.setattr(briefs, "run_model", lambda prompt: calls.append(prompt) or BRIEF.copy())

    first = briefs.run_once()
    first_line = capsys.readouterr().out.strip()
    second = briefs.run_once()
    second_line = capsys.readouterr().out.strip()

    assert first == {"projects_checked": 1, "updated": 1, "model_calls": 1}
    assert first_line == "projects_checked=1 updated=1 model_calls=1"
    assert second == {"projects_checked": 1, "updated": 0, "model_calls": 0}
    assert second_line == "projects_checked=1 updated=0 model_calls=0"
    assert len(calls) == 1
    assert "FAKE_WORKDIR_ACTIVITY" not in calls[0]
    assert "Prepare a simple first release." not in json.dumps(remote_brief)
    context = config / "context/sample.md"
    assert context.is_file() and "Agent estimate basis:" in context.read_text()
    assert "2026-11-02" not in context.read_text()
    assert remote_brief["activity_through"] == project["last_activity_at"]
    assert context.stat().st_mode & 0o777 == 0o600
    assert (config / "briefs-state.json").stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in project_root.iterdir()) == ["README.md"]
    assert transcript.read_bytes() == transcript_before
    assert codex_session.is_file() and "FAKE_WORKDIR_ACTIVITY" in codex_session.read_text()
    assert not (home / ".codex/briefs-state.json").exists()


def test_dry_run_does_not_create_config_state_or_context(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "token"
    token_file.write_text("token")
    token_file.chmod(0o600)
    root = tmp_path / "project"
    root.mkdir()
    project = {"id": "p", "name": "Project", "roots": [str(root)], "brief": None}
    monkeypatch.setattr(briefs, "TOKEN_FILE", token_file)
    monkeypatch.setattr(briefs, "STATE_FILE", tmp_path / "config/state.json")
    monkeypatch.setattr(briefs, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(briefs, "CONTEXT_DIR", tmp_path / "config/context")
    monkeypatch.setattr(briefs, "api_request", lambda *args, **kwargs: {"items": [project]} if args[0].startswith("/api/v1/projects?") else {})
    result = briefs.run_once(dry_run=True)
    assert result["would_update"] == 1
    assert "would_update=project" in capsys.readouterr().out
    assert not (tmp_path / "config").exists()


def _brief_run_fixture(tmp_path, monkeypatch, transcript=True):
    home = tmp_path / "home"
    root = home / "Projects" / "Sample"
    root.mkdir(parents=True)
    config = home / ".config/lighthouse"
    token = config / "secrets/token"
    token.parent.mkdir(parents=True)
    token.write_text("test-token")
    token.chmod(0o600)
    state = config / "briefs-state.json"
    state.write_text(json.dumps({"files": {}, "projects": {}}))
    old = {**BRIEF, "activity_through": "2026-10-25T10:00:00+00:00", "updated_at": "2026-10-25T10:00:00+00:00"}
    project = {"id": "p1", "name": "Sample", "roots": [str(root)], "brief": {"now": old["now"]}}
    remote = old.copy()
    requests = []

    def fake_api(path, bearer, payload=None, method=None):
        assert bearer == "test-token"
        requests.append((path, payload))
        if path == "/api/v1/projects?view=active":
            return {"items": [{**project, "brief": {"now": remote["now"]} if remote else None}]}
        if path == "/api/v1/projects/p1" and payload is None:
            return {"project": {"brief": remote.copy() if remote else None}}
        if path == "/api/v1/projects/p1/brief":
            remote.update(payload)
            remote["updated_at"] = "2026-10-25T10:01:00+00:00"
            return {"brief": remote.copy()}
        raise AssertionError(path)

    paths = []
    if transcript:
        session = home / ".claude/projects/session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"type": "user", "cwd": str(root), "timestamp": "2026-10-25T10:00:00Z",
                                       "message": {"content": "The owner has new work."}}) + "\n")
        paths.append(str(session))
    monkeypatch.setattr(briefs, "TOKEN_FILE", token)
    monkeypatch.setattr(briefs, "CONFIG_DIR", config)
    monkeypatch.setattr(briefs, "STATE_FILE", state)
    monkeypatch.setattr(briefs, "CONTEXT_DIR", config / "context")
    monkeypatch.setattr(briefs, "WORK_DIR", config / "briefs-work")
    monkeypatch.setattr(briefs.Path, "home", lambda: home)
    monkeypatch.setattr(briefs.glob, "glob", lambda pattern, recursive, include_hidden: paths if ".claude" in pattern else [])
    monkeypatch.setattr(briefs, "api_request", fake_api)
    monkeypatch.setattr(briefs, "git_activity", lambda *args: [])
    return remote, requests, state


def test_now_over_twenty_words_is_saved_without_retry(tmp_path, monkeypatch, capsys):
    remote, requests, _ = _brief_run_fixture(tmp_path, monkeypatch)
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        result = BRIEF.copy()
        result["now"] = "The agents finished the recipe search page, the dashboard now shows the new status, and your review is still needed before the release can reach users this week from you."
        return result

    monkeypatch.setattr(briefs, "run_model", model)
    result = briefs.run_once()
    assert result["updated"] == 1 and result["model_calls"] == 1
    assert len(prompts) == 1
    assert briefs._word_count(remote["now"]) == 30
    assert any(path.endswith("/brief") for path, _ in requests)
    assert "ste_reject" not in capsys.readouterr().out


def test_vague_now_uses_the_existing_style_retry_path(tmp_path, monkeypatch, capsys):
    remote, _, _ = _brief_run_fixture(tmp_path, monkeypatch)
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        result = BRIEF.copy()
        if len(prompts) == 1:
            result["now"] = "The agents continue work."
        return result

    monkeypatch.setattr(briefs, "run_model", model)
    result = briefs.run_once()
    assert result["updated"] == 1 and result["model_calls"] == 2
    assert "The agents continue work." in prompts[1]
    assert "concrete item" in prompts[1]
    assert remote["now"] == BRIEF["now"]
    assert "ste_reject" not in capsys.readouterr().out


def test_ste_reject_keeps_previous_brief_and_project_cursors(tmp_path, monkeypatch, capsys):
    remote, requests, state = _brief_run_fixture(tmp_path, monkeypatch)
    old_now = remote["now"]
    long_sentence = "The owner reviews the release with the team and checks every change before the app publishes the recipe to the readers."
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        result = BRIEF.copy()
        result["now"] = long_sentence
        return result

    monkeypatch.setattr(briefs, "run_model", model)
    result = briefs.run_once()
    out = capsys.readouterr().out
    assert result["updated"] == 0 and result["model_calls"] == 3
    assert out.count("ste_reject project=sample") == 1
    assert remote["now"] == old_now
    assert not any(path.endswith("/brief") for path, _ in requests)
    saved = json.loads(state.read_text())
    assert saved["files"] == {}


def test_now_over_240_retries_then_rejects_without_truncating(tmp_path, monkeypatch, capsys):
    remote, requests, state = _brief_run_fixture(tmp_path, monkeypatch)
    old_now = remote["now"]
    too_long = "The agents finished the recipe search page, and your review is still needed. " + "x" * 180
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        result = BRIEF.copy()
        result["now"] = too_long
        return result

    monkeypatch.setattr(briefs, "run_model", model)
    result = briefs.run_once()
    output = capsys.readouterr().out
    assert result["updated"] == 0 and result["model_calls"] == 3
    assert len(prompts) == 3
    assert "now exceeds 240 characters" in prompts[1]
    assert output.count("ste_reject project=sample") == 1
    assert remote["now"] == old_now
    assert not any(path.endswith("/brief") for path, _ in requests)
    assert json.loads(state.read_text())["files"] == {}


def test_two_rejects_skip_same_activity_until_source_changes(tmp_path, monkeypatch, capsys):
    _remote, _requests, state = _brief_run_fixture(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(briefs, "run_model", lambda prompt: calls.append(prompt) or (BRIEF | {"now": "The agents continue work."}))
    first = briefs.run_once()
    second = briefs.run_once()
    third = briefs.run_once()
    output = capsys.readouterr().out
    assert [first["model_calls"], second["model_calls"], third["model_calls"]] == [3, 3, 0]
    assert "ste_skip project=sample reason=two_rejects_same_activity" in output
    assert json.loads(state.read_text())["projects"]["p1"]["consecutive_rejects"] == 2


def test_rewrite_all_updates_existing_brief_and_next_normal_run_is_incremental(tmp_path, monkeypatch, capsys):
    remote, requests, _ = _brief_run_fixture(tmp_path, monkeypatch, transcript=False)
    prompts = []
    monkeypatch.setattr(briefs, "run_model", lambda prompt: prompts.append(prompt) or BRIEF.copy())
    normal = briefs.run_once()
    assert normal == {"projects_checked": 1, "updated": 0, "model_calls": 0}
    capsys.readouterr()
    rewritten = briefs.run_once(rewrite_all=True)
    rewrite_line = capsys.readouterr().out.strip()
    following = briefs.run_once()
    following_line = capsys.readouterr().out.strip()
    assert rewritten == {"projects_checked": 1, "updated": 1, "model_calls": 1, "rewrite_all": True}
    assert rewrite_line.endswith("model_calls=1 rewrite_all=true")
    assert following == {"projects_checked": 1, "updated": 0, "model_calls": 0}
    assert following_line == "projects_checked=1 updated=0 model_calls=0"
    assert len(prompts) == 1 and remote["now"] == BRIEF["now"]
    assert sum(path.endswith("/brief") for path, _ in requests) == 1


def test_config_write_guard_follows_configured_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "relocated" / "lighthouse"
    home.mkdir()
    monkeypatch.setattr(briefs.Path, "home", lambda: home)
    monkeypatch.setattr(briefs, "CONFIG_DIR", config)
    briefs.write_context(config / "context" / "brief.md", "brief")
    assert (config / "context" / "brief.md").read_text() == "brief"
    with pytest.raises(PermissionError, match="limited to"):
        briefs.write_context(home / ".config" / "lighthouse" / "brief.md", "brief")
    with pytest.raises(PermissionError, match="limited to"):
        briefs.write_context(tmp_path / "relocated" / "outside.md", "brief")
    (config / "linked").symlink_to(tmp_path / "relocated", target_is_directory=True)
    with pytest.raises(PermissionError, match="symlinks"):
        briefs.write_context(config / "linked" / "brief.md", "brief")
