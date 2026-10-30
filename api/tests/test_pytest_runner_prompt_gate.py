"""The prompt gate: a generator's prompt is shown and required by config values."""
from __future__ import annotations

import pytest
import yaml

from runners import prompt_gate
from test_pytest_runners import draft, runner_app as runner_app

FIELDS = [
    {"key": "session", "type": "dropdown", "options": ["oneshot", "interactive"], "default": "oneshot"},
    {"key": "surface", "type": "dropdown", "options": ["chat", "annotation"], "default": "chat",
     "show_if": {"session": "interactive"}},
]
SHOW = {"prompt_show_if": {"session": "oneshot"}}
REQUIRE = {"prompt_required_if": {"session": "oneshot"}}


@pytest.mark.parametrize("manifest,values,expected", [
    ({}, {"session": "oneshot", "surface": "chat"}, (True, False)),
    ({"prompt_required": True}, {"session": "interactive", "surface": "chat"}, (True, True)),
    (SHOW, {"session": "oneshot", "surface": "chat"}, (True, False)),
    (SHOW, {"session": "interactive", "surface": "chat"}, (False, False)),
    ({**SHOW, **REQUIRE}, {"session": "oneshot", "surface": "chat"}, (True, True)),
    ({**SHOW, **REQUIRE}, {"session": "interactive", "surface": "chat"}, (False, False)),
    ({**SHOW, "prompt_required": True}, {"session": "interactive", "surface": "chat"}, (False, False)),
    ({"prompt_required_if": {"surface": "annotation"}}, {"session": "interactive", "surface": "annotation"}, (True, True)),
    # A referent hidden by its own show_if can never satisfy a condition.
    ({"prompt_required_if": {"surface": "annotation"}}, {"session": "oneshot", "surface": "annotation"}, (True, False)),
])
def test_prompt_gate_is_visibility_first(manifest, values, expected):
    assert prompt_gate(manifest, FIELDS, values) == expected


def conditional(source, **manifest_values):
    """Give the echo generator a session field and the given prompt conditions."""
    manifest = next(f for f in source.files if f.name == "echo/choice.generator")
    data = yaml.safe_load(manifest.content)
    data.update(prompt_intent="The task for a one-shot run", **manifest_values)
    manifest.content = yaml.safe_dump(data)
    schema = next(f for f in source.files if f.name == "echo/settings.set_yaml")
    schema.content = yaml.safe_dump({"fields": [
        {"key": "message", "type": "text", "default": "hello"},
        {"key": "session", "type": "dropdown", "options": ["oneshot", "interactive"], "default": "oneshot"},
    ]})


def body(session, prompt):
    definition = draft()
    definition["runner"]["settings"] = {"settings.set_yaml": {"message": "saved", "session": session}}
    definition["runner"]["prompt"] = prompt
    return definition


def test_generator_payload_carries_the_prompt_conditions(runner_app):
    client, _, source, _ = runner_app
    assert "prompt_show_if" not in client.get("/api/runner_generators").json()[0]
    conditional(source, prompt_show_if={"session": "oneshot"}, prompt_required_if={"session": "oneshot"})
    entry = client.get("/api/runner_generators").json()[0]
    assert entry["prompt_show_if"] == {"session": "oneshot"}
    assert entry["prompt_required_if"] == {"session": "oneshot"}
    assert entry["prompt_intent"] == "The task for a one-shot run"


def test_a_hidden_prompt_is_never_missing_and_a_shown_one_is_required(runner_app):
    client, _, source, _ = runner_app
    conditional(source, prompt_show_if={"session": "oneshot"}, prompt_required_if={"session": "oneshot"})
    assert client.post("/api/runners", json=body("interactive", "")).status_code == 200
    row = client.get("/api/runners").json()[0]
    assert row["missing_required"] == [] and "invalid" not in row
    editor = client.get("/api/runners/example").json()
    editor["runner"]["settings"]["settings.set_yaml"]["session"] = "oneshot"
    assert client.put("/api/runners/example", json={"runner": editor["runner"],
        "files": editor["files"], "revision": editor["revision"]}).status_code == 200
    row = client.get("/api/runners").json()[0]
    assert [item["key"] for item in row["missing_required"]] == ["prompt"]
    assert row["invalid"]["reason"] == "Runner has incomplete or invalid settings"


def test_a_missing_conditional_prompt_blocks_the_run(runner_app):
    client, _, source, target = runner_app
    conditional(source, prompt_required_if={"session": "oneshot"})
    assert client.post("/api/runners", json=body("oneshot", "")).status_code == 200
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example",
        "cell_id": str(target.id)})
    assert response.status_code == 400
    assert "Prompt" in response.text


def test_an_unconditional_prompt_still_blocks_the_run(runner_app):
    client, _, source, target = runner_app
    conditional(source, prompt_required=True)
    assert client.post("/api/runners", json=body("interactive", "")).status_code == 200
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": "example",
        "cell_id": str(target.id)})
    assert response.status_code == 400
    assert "Prompt" in response.text
