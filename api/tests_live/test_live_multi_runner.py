# SPDX-License-Identifier: MIT
"""Several runs in one cell, end to end on a real model — the multi-runner
contract (run folders and locks).

Each run owns a folder ``_runs/<label>/`` in the cell: the host writes its
run card there before the generator starts, the generator writes its UI
files there, and the folder is swept when the run ends. An Annotation run
serves no file: its card is all its folder holds. The head's run light
counts the cell's live runs; its status caret opens the cell's status pane,
whose Status view has one row per live run (its state, runner, whether it
listens, the elapsed time); a row opens the run's drilldown, whose header
carries its Stop. The Run shortcut (Alt+R) opens the cell's runners panel
whatever is focused. What a run offers besides lives on its own surface — a
web tab's toolbar carries Capture, New tab and the Desktop / Phone / Tablet
toggle. The runners panel's *in new cell* clones the cell into a new stack
right of it (minus ``_runs/``) and starts the runner there.

The scenarios run on the provider-generic gate (``COLLIMATE_LIVE_PROVIDER``
+ its key + ``COLLIMATE_LIVE_MODEL_<PROVIDER>``): an API-key account and a
model entry on the ``pydantic-ai`` harness (its ACP adapter on this machine), no container. The
agent runs are the agent generator's shipped ``default`` config on its
annotation and chat surfaces; the web run is the shipped ``web`` config as it is.

House rules (the same as ``test_live_agent_surfaces``):

* its own cell per scenario — a wedged session must never reach the next;
* bounded — every wait has a timeout and a label that says what was awaited;
* product path — click the status caret, the Status view, the runners panel; type in the
  composer; reach for the API only where the browser genuinely cannot (the
  canvas state).
"""
from __future__ import annotations

import json

import pytest

from .conftest import (
    CHAT_POINTER,
    CHAT_TRANSCRIPT,
    ApiError,
    CellView,
)

pytestmark = pytest.mark.live

NOTES = {"name": "notes.md",
         "content": "# Zephyr Ledger Notes\n\n"
                    "A small file for the agent to read.\n"
                    "It records two entries and nothing else.\n"}
LEDGER = {"name": "data/ledger.csv", "content": "entry,amount\nrent,1200\ncoffee,4\n"}


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════

def open_cell(cell_view, cell_id: str):
    """This cell's view on a freshly loaded page: the load's active-run
    recovery attaches every run the API started, so the run light and the
    status pane know them from the start."""
    return cell_view(cell_id).reopen()


def two_agent_runs(api, runners, provider_model, cell_name: str):
    """A cell with two live agent runs, both idle: an annotation run and a
    chat run of the agent's shipped ``default`` config. Returns
    ``(cell, note_run, chat_run, rows)`` with ``rows`` the ``/runs/active``
    rows by run id."""
    note = runners("default", provider_model, "Annotation live", surface="annotation")
    chat = runners("default", provider_model, "Chat live", surface="chat")
    cell = api.new_cell(cell_name, [NOTES])
    note_run = api.start_runner(cell, note)
    chat_run = api.start_runner(cell, chat)
    api.wait_idle(note_run, timeout=240)
    api.wait_idle(chat_run, timeout=240)
    rows = {r["run_id"]: r for r in api.runs_on(cell)}
    assert set(rows) == {note_run, chat_run}, (
        f"/runs/active lists {sorted(rows)} on the cell, not both runs")
    return cell, note_run, chat_run, rows


def under(folder: str, paths) -> set[str]:
    return {p for p in paths if p.startswith(folder + "/")}


# ══════════════════════════════════════════════════════════════════════════
# 1. two agent runs in one cell — folders, cards, the light, the Status view
# ══════════════════════════════════════════════════════════════════════════

def test_two_agent_runs_share_a_cell(provider_model, api, cell_view, runners):
    """An annotation run and a chat run on one cell, both live: each has its
    own folder with the host's run card, the chat run its pointer registered
    to it; ``/runs/active`` names both with distinct labels; the run light
    counts 2 and the Status view lists both runs; the chat run's row opens
    its drilldown, whose header carries its Stop; the chat pointer's tab
    takes focus with a run live.
    """
    cell, note_run, chat_run, rows = two_agent_runs(
        api, runners, provider_model, "live-two-runs")
    assert rows[note_run]["label"] != rows[chat_run]["label"]
    assert rows[note_run]["folder"] != rows[chat_run]["folder"]

    paths = api.cell_paths(cell)
    for run in (note_run, chat_run):
        label = rows[run]["label"]
        assert rows[run]["folder"] == f"_runs/{label}"
        card = api.run_path(run, f"{label}.run")
        assert card in paths, f"no run card {card} in the cell: {sorted(paths)}"
        record = api.cell_file(cell, card)
        assert record["run_id"] == run and record["label"] == label, record
    chat_pointer = api.run_path(chat_run, CHAT_POINTER)
    assert chat_pointer in paths, sorted(paths)
    assert under(rows[note_run]["folder"], paths) == {api.run_path(note_run, f"{rows[note_run]['label']}.run")}, (
        "an annotation run serves no file")
    session = api.surface(cell, chat_pointer)
    assert session and session["run_id"] == chat_run and session["chat_id"] and session["label"], session

    view = open_cell(cell_view, cell)
    api.wait_until(lambda: view.run_light_count() == 2, timeout=30,
                   label="the run light to count both runs")
    listed = {r["run_id"] for r in view.runs_rows()}
    assert listed == {note_run, chat_run}, listed
    view.shot("runs")

    view.open_run(chat_run)
    stop = view.page.locator(f"{view.sel} [data-testid='run-drilldown'] [data-testid='run-stop']")
    assert rows[chat_run]["runner_name"] in (stop.first.text_content() or ""), stop.first.text_content()
    view.shot("runs-chat-drilldown")
    view.close_runs()

    view.open_path(chat_pointer)
    api.wait_until(lambda: view.tab_selected(chat_pointer), timeout=20,
                   label="the chat pointer's tab to be the active tab")
    view.shot("chat-focused")
    assert {r["run_id"] for r in api.runs_on(cell)} == {note_run, chat_run}


# ══════════════════════════════════════════════════════════════════════════
# 3. Run in new cell — the runners panel clones the cell beside its source
# ══════════════════════════════════════════════════════════════════════════

def test_run_in_new_cell_clones_the_cell_beside_its_source(
        provider_model, api, cell_view, runners):
    """With a run live on the cell, the runners panel still opens; *in new
    cell* puts a clone of the cell (same name, the same files minus
    ``_runs/``) in a new stack right of the source's and starts the runner
    there; the source and its run are untouched."""
    runner = runners("default", provider_model, "Clone live", surface="chat")
    cell = api.new_cell("live-run-in-new-cell", [NOTES, LEDGER])
    run = api.start_runner(cell, runner)
    api.wait_idle(run, timeout=240)
    source_paths = api.cell_paths(cell)
    source_index = api.stack_index_of(cell)
    assert source_index is not None
    stacks_before = len(api.main_stacks())
    source_name = api.main_stacks()[source_index]["cells"][0]["name"]

    view = open_cell(cell_view, cell)
    view.open_from_tree("notes.md")
    panel = view.open_runners()
    try:
        assert panel.locator("[data-testid='cell-panel-note']").count() == 1, (
            "the runners panel carries no collision note")
        panel.get_by_label("Search runners").fill(runner["name"])
        row = panel.get_by_role("option").filter(has_text=runner["name"])
        row.wait_for()
        api.wait_until(lambda: row.get_attribute("aria-selected") == "true",
                       timeout=10, label="the runner to be the panel's selection",
                       every=0.2)
        button = panel.locator("[data-testid='cell-panel-secondary']")
        assert "in new cell" in (button.text_content() or ""), button.text_content()
        view.shot("runners-panel")
        button.click()
        started = api.wait_until(
            lambda: next((r for r in api.active_runs()
                          if r["runner_id"] == runner["id"] and r["cell_id"] != cell),
                         None),
            timeout=60, label="the clone's run in /runs/active")
    finally:
        try:
            close = panel.get_by_role("button", name="Close", exact=True)
            if close.count():
                close.click()
        except Exception:  # noqa: BLE001 — cleanup never masks the verdict
            pass
    clone = started["cell_id"]
    clone_run = api.track(started["run_id"])

    stacks = api.main_stacks()
    assert len(stacks) == stacks_before + 1, "the clone did not get a stack of its own"
    clone_index = api.stack_index_of(clone)
    assert clone_index == source_index + 1, (
        f"the clone's stack is at {clone_index}, not right of the source ({source_index})")
    assert [c["name"] for c in stacks[clone_index]["cells"]] == [source_name]
    assert api.stack_index_of(cell) == source_index

    api.wait_idle(clone_run, timeout=240)
    clone_paths = api.cell_paths(clone)
    plain = lambda paths: {p for p in paths if not p.startswith("_runs/")}  # noqa: E731
    assert plain(clone_paths) == plain(source_paths), (clone_paths, source_paths)
    for path in plain(source_paths):
        assert api.cell_file(clone, path) == api.cell_file(cell, path), path
    assert not under(api.run_folder(run), clone_paths), (
        "the source run's folder crossed into the clone")
    assert under(api.run_folder(clone_run), clone_paths), (
        "the clone's run has no folder in the clone")

    assert api.run_state(run).get("is_active"), "the source run ended"
    assert under(api.run_folder(run), api.cell_paths(cell)) == under(api.run_folder(run), source_paths)
    assert {r["run_id"] for r in api.runs_on(cell)} == {run}
    assert {r["run_id"] for r in api.runs_on(clone)} == {clone_run}
    clone_view = CellView(view.page, clone, view.ev, view.name)
    api.wait_until(lambda: clone_view.run_light_count() == 1, timeout=30,
                   label="the page to attach the clone's run (run_started)")
    view.shot("cloned")


# ══════════════════════════════════════════════════════════════════════════
# 4. stopping one of two runs from the status pane
# ══════════════════════════════════════════════════════════════════════════

def test_stopping_one_run_from_the_status_pane_leaves_the_other_live(
        provider_model, api, cell_view, runners):
    """Stop in the chat run's drilldown ends THAT run only: the annotation
    run stays live with its folder intact, the chat run's folder is swept
    from the cell (a late user write remains refused), its surfaces are in
    its run Logs, and the light counts 1."""
    cell, note_run, chat_run, rows = two_agent_runs(
        api, runners, provider_model, "live-stop-one")
    chat_folder, note_folder = api.run_folder(chat_run), api.run_folder(note_run)
    chat_pointer = api.run_path(chat_run, CHAT_POINTER)
    note_files = under(note_folder, api.cell_paths(cell))
    assert note_files, "the annotation run's folder holds its run card"
    # Run UI is never user-editable, including while the run is live.
    with pytest.raises(ApiError) as live_refused:
        api.write_file(cell, chat_pointer, json.dumps(api.cell_file(cell, chat_pointer)))
    assert live_refused.value.code == 409, live_refused.value

    view = open_cell(cell_view, cell)
    api.wait_until(lambda: view.run_light_count() == 2, timeout=30,
                   label="the run light to count both runs")
    view.click_stop(chat_run)
    api.wait_finished(chat_run, timeout=180)
    assert api.run_state(note_run).get("is_active"), "the Stop ended the other run too"

    api.wait_until(lambda: not under(chat_folder, api.cell_paths(cell)),
                   timeout=60, label="the stopped run's folder to be swept")
    assert not under(chat_folder, api.state_files(cell)), (
        "the canvas still holds files of the stopped run")
    with pytest.raises(ApiError) as refused:
        api.write_file(cell, chat_pointer, "{}")
    assert refused.value.code == 409, refused.value
    logged = api.run_log_paths(chat_run)
    assert chat_pointer in logged and api.run_path(chat_run, CHAT_TRANSCRIPT) in logged, (
        f"the stopped run's surfaces are not in its Logs: {sorted(logged)}")
    assert under(note_folder, api.cell_paths(cell)) == note_files, (
        "the other run's folder changed")
    assert {r["run_id"] for r in api.runs_on(cell)} == {note_run}
    api.wait_until(lambda: view.run_light_count() == 1, timeout=30,
                   label="the run light to count the remaining run")
    view.shot("one-stopped")


# ══════════════════════════════════════════════════════════════════════════
# 5. a web run beside an agent run — the tab's toolbar: Capture and New tab
# ══════════════════════════════════════════════════════════════════════════

def test_web_and_agent_runs_share_a_cell_and_the_tab_toolbar_captures_and_opens_tabs(
        provider_model, api, cell_view, runners):
    """The shipped web runner and a chat run on one cell. A run's row in the
    Status view carries at most its open-file button: Capture and New tab
    are the tab's own toolbar buttons. With ``tab-1.webview`` open, its Capture reaches
    the web run as a press on that tab (the run logs capture #1), and its
    New tab writes ``tab-2.webview`` into the web run's folder and reveals
    it as the active tab. Headless Chromium has no native guest, so the
    webview falls back to its non-native view (still in-document: the
    renderer is ``embed: native``), where a capture has no guest to read and
    is refused — no capture folder lands here; the press reaching the run,
    the file and the reveal are the contract."""
    web = runners("web", None, "Web live", generator="web")
    agent = runners("default", provider_model, "Chat live", surface="chat")
    cell = api.new_cell("live-web-and-agent", [NOTES])
    web_run = api.start_runner(cell, web)
    agent_run = api.start_runner(cell, agent)
    tab1 = api.run_path(web_run, "tab-1.webview")
    api.wait_file(cell, tab1, timeout=120, run_id=web_run)
    api.wait_idle(agent_run, timeout=240)
    rows = {r["run_id"]: r for r in api.runs_on(cell)}
    assert set(rows) == {web_run, agent_run}

    view = open_cell(cell_view, cell)
    api.wait_until(lambda: view.run_light_count() == 2, timeout=30,
                   label="the run light to count both runs")
    for run in (web_run, agent_run):
        assert set(view.row_buttons(run)) <= {'status-run-open-ui'}, (run, view.row_buttons(run))
    view.shot("web-runs")
    view.close_runs()

    view.open_path(tab1)
    api.wait_until(lambda: view.tab_selected(tab1), timeout=20,
                   label="tab-1.webview to be the active tab")
    view.shot("web-tab-1")

    # The tab's toolbar, inside tab-1's renderer host: Capture, then New tab.
    view.click_tab_button(tab1, "Capture")
    api.wait_until(
        lambda: any("capture #1" in str(event)
                    for event in api.run_events(web_run).get("events", [])),
        timeout=60, label="the web run to take tab-1's Capture press", run_id=web_run)
    view.shot("web-captured")
    view.click_tab_button(tab1, "New tab")
    tab2 = api.run_path(web_run, "tab-2.webview")
    api.wait_file(cell, tab2, timeout=60, run_id=web_run)
    api.wait_until(lambda: view.showing(tab2) and view.tab_selected(tab2),
                   timeout=30, label="tab-2.webview revealed as the active tab")
    view.shot("web-tab-2")
    assert api.run_state(agent_run).get("is_active"), "the agent run ended"
    assert {r["run_id"] for r in api.runs_on(cell)} == {web_run, agent_run}


def test_live_web_tab_phone_mode_GN04(api, cell_view, runners):
    """The shipped web runner's tab toolbar carries a Desktop / Phone /
    Tablet toggle. Pressing Phone reaches the run as the ``device`` action
    on that tab: the daemon rewrites ``tab-1.webview`` with ``device:
    "phone"`` and ``viewport: {"width": 412}``, and the renderer letterboxes
    the page at 412 px (headless Chromium has no native guest, so it is the
    fallback frame here; the shell's user agent and emulation are the
    desktop app's)."""
    web = runners("web", None, "Web phone live", generator="web")
    cell = api.new_cell("live-web-phone", [NOTES])
    web_run = api.start_runner(cell, web)
    tab1 = api.run_path(web_run, "tab-1.webview")
    api.wait_file(cell, tab1, timeout=120, run_id=web_run)

    def artifact() -> dict:
        value = api.cell_file(cell, tab1)
        return json.loads(value) if isinstance(value, str) else value

    assert artifact().get("device") == "desktop", artifact()
    view = open_cell(cell_view, cell)
    view.open_path(tab1)
    api.wait_until(lambda: view.tab_selected(tab1), timeout=20,
                   label="tab-1.webview to be the active tab")
    view.page.locator(f"{view.sel} [data-file-path='{tab1}'] [role='group'][aria-label='Device'] button",
                      has_text="Phone").first.click(timeout=15000)
    api.wait_until(lambda: artifact().get("device") == "phone"
                   and artifact().get("viewport") == {"width": 412},
                   timeout=60, label="the web run to rewrite tab-1 as a phone", run_id=web_run)
    frame = view.page.locator(f"{view.sel} [data-file-path='{tab1}'] iframe").first
    api.wait_until(lambda: round(frame.bounding_box()["width"]) == 412, timeout=20,
                   label="the page letterboxed at the phone's width")
    view.shot("web-phone")
