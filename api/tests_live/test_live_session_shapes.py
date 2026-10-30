# SPDX-License-Identifier: MIT
"""The agent session shapes, end to end on a real model.

A one-shot (an Annotation preset) answers its saved prompt — the first,
already-read message of its annotation context — with a reply in that
prompt's thread and ends itself; the cell keeps what it wrote
(``summary.md``) and the run's Logs keep the context (``annotations.json``).
The agent's default runner is an interactive Annotation run: it opens no
file, listens to its context and idles until a message arrives. The default
runner on its chat surface opens a chat. Neither interactive runner seeds an
opening turn, and a status-pane row carries at most its open-file button.

Every runner is made the product way: an Anthropic account and a model entry
through the accounts API, then a runner from a generator's shipped config
bound to that model. Gated on ``ANTHROPIC_API_KEY`` in the environment (the
key is sent to the test server only, stored in its own registry, and deleted
with the account at the end) or, without one, on ``COLLIMATE_LIVE_CLAUDE_CODE=1``
and the machine's Claude subscription (the ``claude_code`` gate's entry).
"""
from __future__ import annotations

import os
import time
import urllib.parse
import uuid

import pytest

from .conftest import (
    CHAT_POINTER,
    CLAUDE_GATE,
    LiveApi,
    annotation_context,
    make_runner,
)

pytestmark = pytest.mark.live

MODEL_PREFERENCE = ("claude-haiku-4-5", "haiku")
NOTES = """# Field notes

The pilot ran for three weeks in two regions. Region A saw a 12% drop in
support tickets after the new onboarding flow; region B saw no change, which
the team attributes to the flow shipping there a week late.

Decisions: keep the new flow, extend the pilot to region C, and re-measure
region B after a full four weeks.
"""


@pytest.fixture
def anthropic_model(api: LiveApi, request):
    """An Anthropic account plus a cheap model entry, deleted afterwards.

    With ``ANTHROPIC_API_KEY`` set: an API-key account and a haiku entry on
    the host-locus pydantic-ai harness. Without it, the session shapes run on
    the claude-code entry the ``claude_code`` gate makes from this machine's
    Claude subscription — the shapes are the generator's, not the harness's.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        if CLAUDE_GATE:
            yield request.getfixturevalue("claude_code")
            return
        pytest.skip("ANTHROPIC_API_KEY is not set and COLLIMATE_LIVE_CLAUDE_CODE is "
                    "not 1 — the session-shape scenarios bill a real model")
    tag = uuid.uuid4().hex[:6]
    owner = api.request("POST", "/api/accounts", {
        "name": f"Anthropic live {tag}", "kind": "account", "service": "anthropic",
        "auth_type": "api_key", "secrets": {"api_key": key},
    })
    ids = [owner["id"]]
    try:
        assert owner["configured"] is True and owner["capabilities"] == ["text"]
        listed = api.request("POST", f"/api/accounts/{urllib.parse.quote(owner['id'])}/models", timeout=90)["models"]
        chosen = next((m["id"] for want in MODEL_PREFERENCE for m in listed if want in m["id"]), None)
        assert chosen, f"no haiku model in the discovered list: {[m['id'] for m in listed][:10]}"
        model = api.request("POST", "/api/accounts", {
            "name": f"Haiku live {tag}", "kind": "model", "credential_account_id": owner["id"],
            "model": chosen, "metadata": {"harness": "pydantic-ai"},
        })
        ids.insert(0, model["id"])
        check = api.request("POST", "/api/accounts/check", {**{k: model[k] for k in (
            "id", "name", "kind", "service", "protocol", "endpoint", "model", "auth_type", "metadata",
            "credential_account_id", "capabilities")}, "secrets": {}}, timeout=90)
        assert check["valid"] is True, check
        yield model
    finally:
        for identity in ids:
            try:
                api.request("DELETE", f"/api/accounts/{urllib.parse.quote(identity)}")
            except Exception:  # noqa: BLE001 — cleanup never masks the verdict
                pass


def text_of(api: LiveApi, cell_id: str, path: str) -> str:
    data = api.cell_file(cell_id, path)
    if isinstance(data, dict):
        return str(data.get("content") or data.get("text") or "")
    return str(data)


def dump_run(api: LiveApi, evidence, run_id: str, cell_id: str, label: str) -> set[str]:
    """The run's log lines, its logged artefacts and the cell's files, kept as
    evidence whatever the verdict — a one-shot that misbehaves is diagnosed
    from its transcript, not from the assertion that caught it."""
    events = api.run_events(run_id).get("events", [])
    evidence.text(f"{label}.events.json", events)
    artifacts = api.run_artifacts(run_id).get("artifacts", [])
    evidence.text(f"{label}.artifacts.json", artifacts)
    for row in artifacts:
        if row.get("category") == "log" and str(row.get("path", "")).endswith(("main.json", "annotations.json", "trace.json", "logs.txt")):
            try:
                body = api.request("GET", f"/api/blob/{row['blob_hash']}")
                evidence.text(f"{label}.{row['path'].replace('/', '_')}", body)
            except Exception:  # noqa: BLE001 — evidence only
                pass
    paths = api.cell_paths(cell_id)
    for path in paths:
        if path == "summary.md":
            evidence.text(f"{label}.{path.replace('/', '_')}", text_of(api, cell_id, path))
    return {str(row.get("path")) for row in artifacts}


def test_oneshot_answers_its_prompt_in_its_context_and_ends_itself(api, anthropic_model, cell_view, evidence):
    runner = make_runner(api, "summarize", anthropic_model["id"],
                         f"Summarize live {uuid.uuid4().hex[:6]}", generator="agent")
    settings = runner["settings"]["agent-config.set_yaml"]
    assert (settings["surface"], settings["session"]) == ("annotation", "oneshot"), settings
    cell = api.new_cell("oneshot-live", files=[{"name": "notes.md", "content": NOTES}])
    run_id = None
    try:
        run_id = api.start_runner(cell, runner)
        # The prompt is the context's first message, user-authored and born
        # read, before the run's process exists.
        (prompt,) = annotation_context(api, cell, run_id)["messages"][:1]
        assert prompt["kind"] == "prompt" and prompt["author"] == "user" and prompt["read_at"], prompt
        assert not any(path.endswith(".chat") for path in api.cell_paths(cell)), "a one-shot opens no file"
        # The page learns about a run it did not start on reload, so it is
        # shot only while the run is live.
        view = cell_view(cell).reopen()
        if api.run_state(run_id).get("is_active"):
            view.shot("oneshot-running")

        api.wait_finished(run_id, timeout=600)
        state = api.run_state(run_id)
        assert state.get("status") == "completed", state
        context = annotation_context(api, cell, run_id)
        assert context["sealed"] is True and context["listening"] is False, context
        replies = [m for m in context["messages"] if m["author"] == "run" and m["parent_id"] == prompt["id"]]
        assert replies and replies[0]["body"].strip(), context["messages"]
        paths = api.cell_paths(cell)
        assert "summary.md" in paths, paths
        assert not any(p.startswith("_runs/") for p in paths), (
            f"the run folder's live session files move to the run log: {sorted(paths)}")
        logs = {str(row.get("path")) for row in api.run_artifacts(run_id).get("artifacts", [])}
        assert "annotations.json" in logs, logs
        view.reopen()
        view.shot("oneshot-finished")
    finally:
        if run_id:
            try:
                api.wait_finished(run_id, timeout=120)
            except AssertionError:
                pass
            dump_run(api, evidence, run_id, cell, "oneshot")
        api.request("DELETE", f"/api/runners/{urllib.parse.quote(runner['id'])}")


def test_default_annotation_runner_opens_no_file_and_listens(api, anthropic_model, cell_view, evidence):
    """The agent's shipped ``default`` preset: an interactive Annotation
    session that opens no file, starts listening to its context (``/runs/
    active`` says so) and goes idle with no message; its row in the status
    pane carries no button, since the run has no file to open."""
    runner = make_runner(api, "default", anthropic_model["id"],
                         f"Annotation live {uuid.uuid4().hex[:6]}", generator="agent")
    settings = runner["settings"]["agent-config.set_yaml"]
    assert (settings["surface"], settings["session"]) == ("annotation", "interactive"), settings
    cell = api.new_cell("interactive-annotation-live", files=[{"name": "notes.md", "content": NOTES}])
    try:
        run_id = api.start_runner(cell, runner)
        api.wait_until(lambda: next((r for r in api.active_runs() if r["run_id"] == run_id), {}).get("listening"),
                       timeout=180, label="the run to listen to its context", run_id=run_id)
        api.wait_idle(run_id, timeout=180)
        assert annotation_context(api, cell, run_id)["messages"] == [], "an interactive run takes no opening message"
        assert api.ui_path(cell, CHAT_POINTER) is None, "an annotation run writes no chat files"
        view = cell_view(cell).reopen()
        assert view.row_buttons(run_id) == [], view.row_buttons(run_id)
        view.shot("interactive-annotation-idle")
        api.stop_run(run_id)
        api.wait_finished(run_id, timeout=120)
    finally:
        api.request("DELETE", f"/api/runners/{urllib.parse.quote(runner['id'])}")


def test_default_chat_runner_opens_a_chat_with_no_seeded_turn(api, anthropic_model, cell_view, evidence):
    """The agent's shipped ``default`` preset on its chat surface: the
    runner has no opening message, so the session goes idle with an empty
    transcript; its row in the status pane offers only to open its chat."""
    runner = make_runner(api, "default", anthropic_model["id"],
                         f"Chat live {uuid.uuid4().hex[:6]}", generator="agent",
                         settings={"surface": "chat"})
    cell = api.new_cell("interactive-live", files=[{"name": "notes.md", "content": NOTES}])
    try:
        run_id = api.start_runner(cell, runner)
        api.wait_file(cell, api.run_path(run_id, CHAT_POINTER), timeout=120, run_id=run_id)
        api.wait_idle(run_id, timeout=180)
        time.sleep(2)
        assert api.messages(cell) == [], "an interactive session takes no opening message"
        view = cell_view(cell).reopen()
        view.open_chat_tab()
        assert view.row_buttons(run_id) == ['status-run-open-ui'], view.row_buttons(run_id)
        view.shot("interactive-idle")
        api.stop_run(run_id)
        api.wait_finished(run_id, timeout=120)
    finally:
        api.request("DELETE", f"/api/runners/{urllib.parse.quote(runner['id'])}")
