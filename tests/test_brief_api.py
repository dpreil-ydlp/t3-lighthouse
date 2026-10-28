import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from lighthouse.db import connection

def make_project(client, headers):
    result = client.post("/api/v1/projects", headers={**headers, "Idempotency-Key": uuid.uuid4().hex}, json={"name": "Brief " + uuid.uuid4().hex[:8]})
    assert result.status_code == 200, result.text
    return result.json()["project"]


def valid_brief(**overrides):
    value = {
        "about": "A project that helps its users complete a clear task.",
        "now": "The first release is in review.",
        "done": ["The core flow is ready."],
        "left": [
            {"text": "You must review the release wording.", "who": "you"},
            {"text": "The agents must test the release.", "who": "agents", "hours": 10},
        ],
        "eta_basis": "Ten active hours remain, based on the current work list.",
        "activity_through": datetime.now(timezone.utc).isoformat(),
    }
    return value | overrides


def test_brief_scope_user_write_and_append_only_history(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    agent = {"Authorization": f"Bearer {principals['agent'][0]}"}
    user = {"Authorization": f"Bearer {principals['user'][0]}"}

    with connection() as conn:
        conn.execute("UPDATE principals SET permission_scopes=array_remove(permission_scopes,'project:brief') WHERE id=%s", (principals["agent"][1],))
    assert client.put(f"/api/v1/projects/{project['id']}/brief", headers=agent, json=valid_brief()).status_code == 403
    first = client.put(f"/api/v1/projects/{project['id']}/brief", headers=user, json=valid_brief())
    second = client.put(f"/api/v1/projects/{project['id']}/brief", headers=user, json=valid_brief(now="The agents test the release."))
    assert first.status_code == 200 and first.json()["brief"]["version"] == 1
    assert second.status_code == 200 and second.json()["brief"]["version"] == 2
    with connection() as conn:
        rows = conn.execute("SELECT version,body FROM project_brief_history WHERE project_id=%s ORDER BY version", (project["id"],)).fetchall()
        can_delete = conn.execute("SELECT has_table_privilege(current_user,'project_brief_history','DELETE') AS allowed").fetchone()["allowed"]
    assert [row["version"] for row in rows] == [1, 2]
    assert rows[1]["body"]["now"] == "The agents test the release."
    assert can_delete is False


@pytest.mark.parametrize("change", [
    {"about": "x" * 601}, {"now": "x" * 241}, {"done": ["x"] * 9},
    {"left": [{"text": "x" * 201, "who": "you"}]},
    {"left": [{"text": "Review the release."}]},
    {"left": [{"text": "Review the release.", "who": "you", "hours": None}]},
    {"left": [{"text": "The agents test the release.", "who": "agents"}]},
    {"left": [{"text": "The agents test the release.", "who": "agents", "hours": -1}]},
    {"now": "The project is finished."},
    {"remaining_hours": 4}, {"eta_confidence": "medium"},
    {"eta_basis": "x" * 301}, {"unexpected": "field"}, {"message_text": "private transcript"},
])
def test_brief_validation_returns_422(client, principals, change):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    response = client.put(f"/api/v1/projects/{project['id']}/brief", headers={"Authorization": f"Bearer {principals['user'][0]}"}, json=valid_brief(**change))
    assert response.status_code == 422


def test_api_model_rejects_nonfinite_agent_hours():
    from lighthouse.api import ProjectBriefIn

    value = valid_brief(left=[{"text": "The agents test the release.", "who": "agents", "hours": float("inf")}])
    with pytest.raises(ValidationError):
        ProjectBriefIn.model_validate(value)


@pytest.mark.parametrize("claim", [
    "The project is now finished.",
    "Everything is now complete.",
    "The project is fully done.",
    "All done.",
])
def test_api_model_rejects_adverb_completion_claims_with_open_items(claim):
    from lighthouse.api import ProjectBriefIn

    with pytest.raises(ValidationError):
        ProjectBriefIn.model_validate(valid_brief(now=claim))


def test_api_model_accepts_subwork_completion_with_open_items():
    from lighthouse.api import ProjectBriefIn

    ProjectBriefIn.model_validate(valid_brief(done=["The agents finished the settings screen."]))


def test_api_model_accepts_completed_upload_flows_with_open_items():
    from lighthouse.api import ProjectBriefIn

    ProjectBriefIn.model_validate(valid_brief(done=["The agents checked all completed upload flows."]))


def test_brief_api_accepts_now_at_240_characters(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    now_text = "x" * 240
    response = client.put(
        f"/api/v1/projects/{project['id']}/brief",
        headers={"Authorization": f"Bearer {principals['user'][0]}"},
        json=valid_brief(now=now_text),
    )
    assert response.status_code == 200, response.text
    assert response.json()["brief"]["now"] == now_text


def test_brief_api_derives_status_and_omits_finish_date_fields(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    now = datetime.now(timezone.utc)
    activity_through = datetime.now(timezone.utc).isoformat()
    with connection() as conn:
        for bucket in range(28):
            observed = now - timedelta(days=1, minutes=bucket * 15)
            conn.execute("INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,'collector_observation','codex','medium',%s,%s,%s)",
                         (project["id"], f"brief-eta-{uuid.uuid4().hex}", observed, Jsonb({"summary": "Observed"})))
    headers = {"Authorization": f"Bearer {principals['agent'][0]}"}
    written = client.put(f"/api/v1/projects/{project['id']}/brief", headers=headers, json=valid_brief(activity_through=activity_through))
    assert written.status_code == 200, written.text
    full = client.get(f"/api/v1/projects/{project['id']}", headers=headers).json()["project"]["brief"]
    assert full["about"] == valid_brief()["about"]
    assert full["needs_you"] == {"action": "Review the release wording.", "count": 1}
    assert full["agent_hours"] == 10
    assert full["state"] == "waiting_on_you"
    assert full["checked_at"] == full["updated_at"]
    assert datetime.fromisoformat(full["activity_through"]) == datetime.fromisoformat(activity_through)
    items = client.get("/api/v1/projects?view=all", headers=headers).json()["items"]
    brief = next(item["brief"] for item in items if item["id"] == str(project["id"]))
    assert brief["needs_you"] == full["needs_you"]
    assert brief["agent_hours"] == 10
    assert not ({"eta_date", "eta_confidence", "pace_hours_per_day", "remaining_hours"} & set(brief))


def test_unknown_agent_hours_propagate_to_api_and_mcp(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    unknown = valid_brief(left=[{"text": "The agents test the release.", "who": "agents", "hours": None}])
    response = client.put(f"/api/v1/projects/{project['id']}/brief", headers={"Authorization": f"Bearer {principals['user'][0]}"}, json=unknown)
    assert response.status_code == 200
    detail = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["project"]["brief"]
    assert detail["agent_hours"] is None
    assert detail["state"] == "idle"
    assert not ({"eta_date", "eta_confidence", "pace_hours_per_day", "remaining_hours"} & set(detail))


@pytest.mark.parametrize(("left", "expected"), [
    ([{"text": "You approve the plan.", "who": "you"}], {"action": "You approve the plan.", "count": 1}),
    ([{"text": "You approve the plan.", "who": "you"}, {"text": "You sign the form.", "who": "you"}], {"action": "You approve the plan.", "count": 2}),
])
def test_owner_only_brief_derives_needs_you_and_zero_agent_hours(client, principals, left, expected):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    response = client.put(f"/api/v1/projects/{project['id']}/brief", headers={"Authorization": f"Bearer {principals['user'][0]}"}, json=valid_brief(left=left))
    assert response.status_code == 200
    brief = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["project"]["brief"]
    assert brief["needs_you"] == expected
    assert brief["agent_hours"] == 0
    assert brief["state"] == "waiting_on_you"


def test_mixed_brief_sums_known_agent_hours_and_unknown_outside_items(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    left = [
        {"text": "Approve the plan.", "who": "you"},
        {"text": "The agents test the plan.", "who": "agents", "hours": 2.5},
        {"text": "The agents update the guide.", "who": "agents", "hours": 3},
        {"text": "The vendor supplies the file.", "who": "outside"},
    ]
    response = client.put(f"/api/v1/projects/{project['id']}/brief", headers={"Authorization": f"Bearer {principals['user'][0]}"}, json=valid_brief(left=left))
    assert response.status_code == 200
    brief = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["project"]["brief"]
    assert brief["agent_hours"] == 5.5
    assert brief["needs_you"] == {"action": "Approve the plan.", "count": 1}


def test_no_open_items_get_nothing_open_state(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    response = client.put(f"/api/v1/projects/{project['id']}/brief", headers={"Authorization": f"Bearer {principals['user'][0]}"}, json=valid_brief(left=[]))
    assert response.status_code == 200
    brief = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["project"]["brief"]
    assert brief["state"] == "nothing_open"
    assert brief["needs_you"] is None
    assert brief["agent_hours"] == 0


def test_owner_actions_take_precedence_over_paused_state(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    old = datetime.now(timezone.utc) - timedelta(days=8)
    with connection() as conn:
        conn.execute("INSERT INTO events(project_id,event_type,source,confidence,source_event_id,observed_at,details) VALUES(%s,'collector_observation','codex','medium',%s,%s,%s)",
                     (project["id"], "paused-" + uuid.uuid4().hex, old, Jsonb({"summary": "Observed"})))
    response = client.put(f"/api/v1/projects/{project['id']}/brief", headers={"Authorization": f"Bearer {principals['user'][0]}"}, json=valid_brief())
    assert response.status_code == 200
    brief = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"}).json()["project"]["brief"]
    assert brief["state"] == "waiting_on_you"
    assert brief["paused_since"] is None


@pytest.mark.parametrize("activity_state,age_days,left,expected", [
    ("working_now", 8, [{"text": "The agents check the release.", "who": "agents", "hours": 1}], "agents_working"),
    ("today", 0, [{"text": "Review the release.", "who": "you"}], "waiting_on_you"),
    ("quiet", 8, [{"text": "Review the release.", "who": "you"}], "waiting_on_you"),
    ("quiet", 8, [{"text": "The agents check the release.", "who": "agents", "hours": 1}], "paused"),
    ("quiet", 2, [], "nothing_open"),
    ("quiet", 2, [{"text": "The agents check the release.", "who": "agents", "hours": 1}], "idle"),
])
def test_derive_brief_state_rule(activity_state, age_days, left, expected):
    from lighthouse.api import derive_brief
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    brief = derive_brief({"left": left, "updated_at": stamp.isoformat()}, activity_state, stamp)
    assert brief["state"] == expected


@pytest.mark.parametrize(("text", "expected"), [("You must approve merge #135", "Approve merge #135"), ("You need to approve merge #135", "Approve merge #135"), ("You approve merge #135", "You approve merge #135")])
def test_derive_brief_strips_only_supported_legacy_owner_prefixes_and_capitalizes(text, expected):
    from lighthouse.api import derive_brief
    stamp = datetime.now(timezone.utc)
    legacy = derive_brief({"left": [{"text": text, "who": "you"}]}, "quiet", stamp)
    assert legacy["needs_you"] == {"action": expected, "count": 1}
    assert legacy["left"][0]["text"] == expected
    lowercase = derive_brief({"left": [{"text": "decide whether to approve the passkey.", "who": "you"}]}, "quiet", stamp)
    assert lowercase["needs_you"]["action"] == "Decide whether to approve the passkey."
    trading = derive_brief({"left": [{"text": "You will run the strategy.", "who": "you"}]}, "quiet", stamp)
    assert trading["needs_you"]["action"] == "You will run the strategy."
    assert derive_brief({"left": []}, "quiet", stamp)["needs_you"] is None


def test_legacy_string_left_items_are_unknown_without_finish_estimate(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    with connection() as conn:
        conn.execute("""INSERT INTO project_briefs(project_id,about,now,done,"left",remaining_hours,eta_confidence,eta_basis,version,schema_version)
            VALUES(%s,'A project.','Work is active.','[]'::jsonb,'["Legacy task"]'::jsonb,NULL,NULL,NULL,1,1)""", (project["id"],))
    headers = {"Authorization": f"Bearer {principals['user'][0]}"}
    brief = client.get(f"/api/v1/projects/{project['id']}", headers=headers).json()["project"]["brief"]
    assert brief["left"] == [{"text": "Legacy task", "who": "unknown"}]
    assert brief["agent_hours"] is None
    assert brief["schema_version"] == 1


def test_project_open_tasks_hide_finished_and_blocked_derived_children(client, principals):
    project = make_project(client, {"Authorization": f"Bearer {principals['agent'][0]}"})
    with connection() as conn:
        conn.execute("""INSERT INTO tasks(project_id,title,status) VALUES
          (%s,'Registered blocked task','blocked'),
          (%s,'Registered complete task','complete')""", (project["id"], project["id"]))
        conn.execute("""INSERT INTO tasks(project_id,title,status,derived_source,derived_external_id) VALUES
          (%s,'Running child','running','t3_subagent',%s),
          (%s,'Blocked child','blocked','t3_subagent',%s),
          (%s,'Finished child','complete','t3_subagent',%s)""",
          (project["id"], f"running-{principals['prefix']}", project["id"], f"blocked-{principals['prefix']}",
           project["id"], f"finished-{principals['prefix']}"))
    response = client.get(f"/api/v1/projects/{project['id']}", headers={"Authorization": f"Bearer {principals['user'][0]}"})
    assert response.status_code == 200
    titles = {task["title"] for task in response.json()["open_tasks"]}
    assert titles == {"Registered blocked task", "Running child"}


def test_mcp_brief_tool_returns_api_detail_brief(monkeypatch):
    from lighthouse import mcp_server

    expected = {"about": "Brief", "now": "Review", "done": [], "left": [], "state": "nothing_open",
                "agent_hours": None, "needs_you": None, "checked_at": "2026-10-26T12:00:00+00:00"}
    seen = []
    monkeypatch.setattr(mcp_server, "api_call", lambda ctx, path, method, payload=None, idempotency_key=None:
                        seen.append((path, method)) or {"project": {"brief": expected}})
    assert mcp_server.get_project_brief.fn(object(), "project-id") == expected
    assert not ({"eta_date", "eta_confidence", "remaining_hours", "pace_hours_per_day"} & set(expected))
    assert seen == [("/api/v1/projects/project-id", "GET")]
