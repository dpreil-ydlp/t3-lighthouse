import uuid


def test_active_project_without_next_action_has_no_attention_state(client, headers):
    response = client.post(
        "/api/v1/projects",
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
        json={"name": "attention " + uuid.uuid4().hex[:8], "kind": "research"},
    )
    assert response.status_code == 200, response.text
    project = response.json()["project"]
    assert project["next_action"] is None

    listed = client.get("/api/v1/projects?view=active", headers=headers)
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()["items"] if item["id"] == project["id"])
    assert item["attention_state"] != "needs_next_action"
