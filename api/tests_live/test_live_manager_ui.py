# SPDX-License-Identifier: MIT
"""The live product-path suite for the Key manager, the Capabilities manage
view's runners and the runners panel.

Each test drives ONE flow end to end against a real server and the shipped
SPA in a real headless Chromium — the clicks a user makes, on the DOM the app
renders. No harness is involved, so nothing here is gated on a provider: the
only preconditions are the ones the session fixtures check (the built SPA,
the vendored generators, python-playwright with a Chromium).

What is asserted is the contract of the rework:

* the Key manager's add flow fixes an account's type and service BEFORE the
  detail form, Verify tests the open form without saving it and explains
  where the credential comes from when it fails, and Save returns to the list;
* runners are made and kept in the Capabilities manage view: Add runner asks
  which Capabilities cell keeps the runner (its generator's cell by default),
  binds it to a generator, then edits it across Config / Advanced / Files /
  Security, with each account role rendered as a key-icon row on the Config
  tab; each runner's card edits, duplicates and deletes it;
* the runners panel only runs: a click selects a runner, Enter (or a
  double-click) posts the run, and nothing in it edits a runner.

A cell's panel session is saved in the channel, so a panel a test leaves open
re-mounts on every later page: every panel locator is scoped to the test's
own cell and each test closes its panel when it is done. Likewise whether
Capabilities is shown is channel state: a test that shows it hides it again.

Button locators are EXACT. The top bar's channel scrubber carries a
"Next cell" control, so a substring match on "Next" would hit it too.

Screenshots land in the evidence dir; set ``COLLIMATE_LIVE_SHOTS`` to a
directory to receive copies of the ones that document the reworked screens.
"""
from __future__ import annotations

import os
import shutil
import time
import urllib.parse
import uuid

import pytest

from .conftest import LiveApi

pytestmark = pytest.mark.live

#: The service the supported-account scenario adds. It has an API-key method
#: whose check is a real HTTP call, so a fake key fails with the service's
#: message under the catalog's instruction.
SUPPORTED_SERVICE = "OpenAI"


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════

def shot(page, evidence, name: str) -> str:
    """A screenshot in the evidence dir, copied to ``COLLIMATE_LIVE_SHOTS``
    when that is set."""
    path = evidence.shot(page, name)
    target = os.environ.get("COLLIMATE_LIVE_SHOTS")
    if path and target:
        os.makedirs(target, exist_ok=True)
        shutil.copy(path, os.path.join(target, f"{name}.png"))
    return path


def settles(pred, timeout: float = 10.0, label: str = "the condition"):
    """Poll ``pred`` until it is true — for the state reads that do not
    auto-wait (``is_enabled``, counts, evaluated text)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {label}")


def open_key_manager(page):
    """Settings (its shortcut opens the Commands pane on its settings view) →
    the Key manager section row."""
    page.keyboard.press("ControlOrMeta+Comma")
    settings = page.locator('[data-command-pane][data-pane-view="settings"]')
    settings.wait_for()
    settings.get_by_role("button", name="Key manager", exact=True).click()
    page.get_by_role("button", name="Add account").wait_for()


def account_ids(api: LiveApi) -> dict[str, str]:
    return {a["name"]: a["id"] for a in api.request("GET", "/api/accounts")["accounts"]}


def delete_accounts(api: LiveApi, *names: str):
    ids = account_ids(api)
    for name in names:
        if name in ids:
            api.request("DELETE", f"/api/accounts/{urllib.parse.quote(ids[name])}")


def runner_entry(api: LiveApi, runner_id: str) -> dict | None:
    return next((r for r in api.request("GET", "/api/runners") if r["id"] == runner_id), None)


class RunnersPanel:
    """One cell's runners panel: every locator is scoped to it."""

    def __init__(self, page, view):
        self.page = page
        self.view = view
        self.root = page.locator(f"[data-pick-surface][data-pick-cell='{view.cell_id}']")

    def open(self):
        self.view.open_runners()
        return self

    def close(self):
        """Best effort: the panel session is channel state, so an open panel
        would otherwise follow every later test onto its page."""
        try:
            close = self.root.get_by_role("button", name="Close", exact=True)
            if close.count():
                close.click()
                self.root.wait_for(state="detached", timeout=10000)
        except Exception:  # noqa: BLE001 — cleanup never masks the verdict
            pass

    def row(self, name: str):
        return self.root.get_by_role("option").filter(has_text=name)


class ManageView:
    """The Capabilities manage view: the front card of the Capabilities stack.
    Every locator is scoped to it."""

    def __init__(self, page):
        self.page = page
        self.root = page.locator("[data-capabilities-manage]")
        self.pill = page.locator("button[title^='Capabilities: ']")

    def open(self):
        """Commands → Show Capabilities: the stack comes onto the canvas with
        its manage card in front (the top-bar pill would front the card it
        last showed). A shown stack is hidden first."""
        if self.pill.get_attribute("aria-pressed") == "true":
            self.pill.click()
            settles(lambda: self.pill.get_attribute("aria-pressed") == "false", label="Capabilities to hide")
        self.page.get_by_role("button", name="Commands", exact=True).click()
        self.page.get_by_role("combobox", name="Command").fill("Show Capabilities")
        self.page.get_by_role("option").filter(has_text="Show Capabilities").first.click()
        # Its Hide button shows only while the manage card is the front face.
        self.root.get_by_role("button", name="Hide Capabilities", exact=True).wait_for()
        self.runners().wait_for()
        return self

    def close(self):
        """Best effort: hide Capabilities again, so the next test's canvas
        starts as this one's did."""
        try:
            if self.pill.get_attribute("aria-pressed") == "true":
                self.pill.click()
                settles(lambda: self.pill.get_attribute("aria-pressed") == "false", label="Capabilities to hide")
        except Exception:  # noqa: BLE001 — cleanup never masks the verdict
            pass

    def runners(self):
        """The Runners group of the list."""
        return self.root.locator("[data-capability-group='runner']")

    def card(self, name: str):
        """One runner's card (exact: a copy's card is '<name> copy runner')."""
        return self.root.locator(f"article[aria-label='{name} runner']")

    def editor(self):
        return self.root.locator("[data-runner-editor]")

    def search(self, text: str):
        self.root.get_by_label("Search capabilities").fill(text)


def open_runners_panel(page, cell_view, api: LiveApi, name: str) -> RunnersPanel:
    """A fresh cell with its runners panel open."""
    view = cell_view(api.new_cell(name))
    return RunnersPanel(page, view).open()


# ══════════════════════════════════════════════════════════════════════════
# Key manager
# ══════════════════════════════════════════════════════════════════════════

def test_key_manager_adds_a_custom_account_through_type_then_detail(page, api, evidence):
    """Add account → Custom → Next → the detail opens with the type fixed, no
    Verify and no Advanced settings (nothing is known about a custom service),
    and the Account label last → Save returns to the list."""
    name = f"Deploy target {uuid.uuid4().hex[:6]}"
    try:
        open_key_manager(page)
        page.get_by_role("button", name="Add account").click()
        group = page.get_by_role("group", name="Account type")
        nxt = page.get_by_role("button", name="Next", exact=True)
        assert nxt.is_disabled(), "Next is gated on the account type"
        group.get_by_role("button", name="Custom", exact=True).click()
        settles(nxt.is_enabled, label="Next to enable")
        nxt.click()

        # The detail: a read-only strip carrying the choice. A custom entry's
        # credentials are its secret custom fields, so no auth method is
        # offered, and nothing can be verified or tuned about it.
        page.get_by_text("Custom", exact=True).first.wait_for()
        assert page.get_by_label("Account label").count() == 1
        assert page.get_by_label("Name").count() == 0
        assert page.get_by_role("button", name="Verify").count() == 0
        assert page.get_by_text("Advanced settings").count() == 0
        assert page.get_by_label("Service").count() == 0, "the service is fixed at the add stage"
        assert page.get_by_label("Authentication").count() == 0
        assert page.get_by_label("Capabilities").count() == 0, "capabilities belong to model entries"
        page.get_by_label("Custom type").fill("deployment")
        page.get_by_role("button", name="Add field").click()
        page.get_by_label("Field name 1").fill("token")
        page.get_by_label("Store field 1 as a secret").check()
        page.get_by_label("Secret for field 1").fill("deploy-token-123")
        # The label comes last, under the fields that make up the account.
        label = page.get_by_label("Account label")
        assert label.bounding_box()["y"] > page.get_by_label("Secret for field 1").bounding_box()["y"]
        label.fill(name)
        shot(page, evidence, "key-manager-detail")

        page.get_by_role("button", name="Save", exact=True).click()
        page.get_by_role("button", name="Add account").wait_for()      # back on the list
        card = page.locator(f"section[aria-label='{name} account']")
        card.wait_for()
        assert "Custom" in card.inner_text()
        saved = next(a for a in api.request("GET", "/api/accounts")["accounts"] if a["name"] == name)
        assert saved["kind"] == "custom" and saved["capabilities"] == []
        assert saved["metadata"]["custom_type"] == "deployment"
        assert saved["secret_fields"] == ["token"] and saved["configured"] is True
    finally:
        delete_accounts(api, name)


def test_key_manager_supported_account_verify_explains_a_bad_key(page, api, evidence):
    """A supported service with a fake key: Verify fails with the service's
    message under the catalog's instruction, and Save still stores it."""
    name = f"OpenAI probe {uuid.uuid4().hex[:6]}"
    services = api.request("GET", "/api/accounts")["services"]
    openai = next(s for s in services if s["name"] == SUPPORTED_SERVICE)
    try:
        open_key_manager(page)
        page.get_by_role("button", name="Add account").click()
        page.get_by_role("group", name="Account type").get_by_role("button", name="Supported service").click()
        nxt = page.get_by_role("button", name="Next", exact=True)
        service = page.get_by_label("Service")
        service.wait_for()
        assert nxt.is_disabled(), "Next is gated on the service"
        labels = service.evaluate("s => [...s.options].slice(1).map(o => o.textContent)")
        assert labels == sorted(labels, key=str.casefold), f"services are alphabetical: {labels}"
        service.select_option(label=SUPPORTED_SERVICE)
        settles(nxt.is_enabled, label="Next to enable")
        nxt.click()

        page.get_by_text(f"Supported service · {SUPPORTED_SERVICE}").wait_for()
        auth = page.get_by_label("Authentication")
        auth_labels = auth.evaluate("s => [...s.options].map(o => o.textContent)")
        assert auth_labels == sorted(auth_labels, key=str.casefold), auth_labels
        page.get_by_label("API key").fill("sk-not-a-real-key")
        # Verify sits above Advanced settings and only tests the open form.
        verify = page.get_by_role("button", name="Verify")
        assert verify.bounding_box()["y"] < page.get_by_text("Advanced settings").bounding_box()["y"]
        page.get_by_label("Account label").fill(name)
        verify.click()
        status = page.get_by_role("status")
        status.wait_for(timeout=60000)
        line = status.inner_text()
        assert "Verified" not in line, f"a fake key must not verify: {line!r}"
        page.get_by_text(openai["help"]["api_key"]).wait_for()
        assert name not in account_ids(api)
        shot(page, evidence, "key-manager-verify-failed")

        page.get_by_role("button", name="Save", exact=True).click()
        page.locator(f"section[aria-label='{name} account']").wait_for()
        saved = next(a for a in api.request("GET", "/api/accounts")["accounts"] if a["name"] == name)
        assert saved["service"] == openai["id"] and saved["auth_type"] == "api_key"
        assert saved["capabilities"] == openai["capabilities"]
    finally:
        delete_accounts(api, name)


def test_key_manager_plan_login_is_pasted_and_verified_offline(page, api, evidence):
    """A ChatGPT-plan account: the instruction under Authentication says what
    to paste, the App select names the file, the pasted document is verified
    without a network call, and Save keeps it. Nothing signs in."""
    name = f"ChatGPT plan {uuid.uuid4().hex[:6]}"
    services = api.request("GET", "/api/accounts")
    store = services["plan_logins"]["codex"]["store"]
    try:
        open_key_manager(page)
        page.get_by_role("button", name="Add account").click()
        page.get_by_role("group", name="Account type").get_by_role("button", name="Supported service").click()
        page.get_by_label("Service").select_option(label=SUPPORTED_SERVICE)
        page.get_by_role("button", name="Next", exact=True).click()
        auth = page.get_by_label("Authentication")
        auth.select_option("cli")
        page.get_by_text("paste the contents of the login file").wait_for()   # the instruction, under the method
        for verb in ("Sign in", "Sign out", "Pull login"):
            assert page.get_by_role("button", name=verb).count() == 0, f"no {verb}"
        document = page.get_by_label("Login document")
        page.get_by_label("App").select_option("codex")
        page.get_by_text(store).wait_for()                                   # the hint names the file
        document.fill('{"OPENAI_API_KEY": "not-a-login"}')
        page.get_by_role("button", name="Verify").click()
        status = page.get_by_role("status")
        status.wait_for(timeout=30000)
        assert "not a chatgpt login" in status.inner_text()
        assert name not in account_ids(api), "Verify saves nothing"
        document.fill('{"tokens": {"refresh_token": "live-fake-refresh"}}')
        page.get_by_role("button", name="Verify").click()
        page.get_by_role("status").filter(has_text="Verified").wait_for(timeout=30000)
        assert "live-fake-refresh" not in page.content().replace("live-fake-refresh", "", 1), "the document is in the field only"
        shot(page, evidence, "key-manager-plan-verified")
        page.get_by_label("Account label").fill(name)
        page.get_by_role("button", name="Save", exact=True).click()
        page.locator(f"section[aria-label='{name} account']").wait_for()
        saved = next(a for a in api.request("GET", "/api/accounts")["accounts"] if a["name"] == name)
        assert saved["auth_type"] == "cli" and saved["metadata"]["harness"] == "codex"
        assert saved["configured"] is True and saved["secret_fields"] == ["credential_json"]
    finally:
        delete_accounts(api, name)


# ══════════════════════════════════════════════════════════════════════════
# Capabilities runners + the runners panel
# ══════════════════════════════════════════════════════════════════════════

def test_manage_view_add_runner_keeps_it_in_its_generators_cell_then_edits_config(page, api, evidence):
    """Add runner → 'Keep it in' (its generator's cell by default, or any
    other Capabilities cell) → Next → Generator → Next → the Config tab lists
    the account roles as key-icon rows → name it → Save → the list returns on
    the new runner's card, and the runner lives in its generator's cell."""
    generators = api.request("GET", "/api/runner_generators")
    with_roles = [g for g in generators if g.get("account_requirements")]
    if not with_roles:
        pytest.skip("no generator in the seeded channel declares an account role")
    generator = with_roles[0]
    name = f"Role runner {uuid.uuid4().hex[:6]}"
    runner_id = None
    manage = ManageView(page)
    try:
        manage.open()
        manage.runners().get_by_role("button", name="Add runner", exact=True).click()
        keep = manage.root.get_by_role("group", name="Keep it in")
        keep.wait_for()
        default = keep.get_by_role("radio", name="Its generator’s cell")
        assert default.is_checked(), "a new runner lives in its generator's cell by default"
        outline = api.request("GET", f"/api/channels/{api.chq}/outline")
        settles(lambda: keep.get_by_role("radio").count() == 1 + len(outline["capabilities_cells"]),
                label="every Capabilities cell to be offered")
        shot(page, evidence, "manage-runner-keep-it-in")
        manage.root.get_by_role("button", name="Next", exact=True).click()

        editor = manage.editor()
        nxt = editor.get_by_role("button", name="Next", exact=True)
        nxt.wait_for()
        assert nxt.is_disabled(), "Next needs the generator"
        editor.get_by_label("Generator").select_option(label=generator["name"])
        settles(nxt.is_enabled, label="Next to enable")
        nxt.click()

        tabs = editor.get_by_role("tab")
        tabs.first.wait_for()
        assert tabs.evaluate_all("ts => ts.map(t => t.textContent.trim())") == ["Config", "Advanced", "Files", "Security"]
        assert editor.get_by_role("tab", name="Config").get_attribute("aria-selected") == "true"
        for role, requirement in generator["account_requirements"].items():
            label = requirement.get("label") or role
            editor.get_by_role("button", name=f"{label} requirement").wait_for()
        assert editor.get_by_role("button", name="Push into config").count() == 0
        names = editor.get_by_role("button").evaluate_all("bs => bs.map(b => b.textContent.trim())")
        assert names[-2:] == ["Cancel", "Save"], names
        save = editor.get_by_role("button", name="Save", exact=True)
        assert save.is_disabled(), "a runner is saved with a name"
        editor.get_by_label("Name", exact=True).fill(name)
        shot(page, evidence, "runner-editor-config")

        save.click()
        manage.root.get_by_role("status").filter(has_text=f"Runner ‘{name}’ added.").wait_for()
        manage.card(name).wait_for()
        entry = next(r for r in api.request("GET", "/api/runners") if r["name"] == name)
        runner_id = entry["id"]
        assert entry["generator"] == generator["id"]
        home = next(row["cell_id"] for row in api.request("GET", "/api/generators") if row["name"] == generator["id"])
        assert entry["cell_id"] == home, "the runner lives in its generator's cell"
        shot(page, evidence, "manage-runner-added")
    finally:
        manage.close()
        if runner_id:
            api.request("DELETE", f"/api/runners/{urllib.parse.quote(runner_id)}")


def _valid_runner(api: LiveApi, name: str) -> dict:
    """A runner the catalog reports as runnable, made from the first generator
    whose Default validates as-is; skips when none does."""
    for generator in api.request("GET", "/api/runner_generators"):
        draft = generator["default"]
        runner = {**draft["runner"], "id": f"live-{uuid.uuid4().hex[:8]}", "name": name, "generator": generator["id"]}
        api.request("POST", "/api/runners", {"runner": runner, "files": draft["files"]})
        entry = runner_entry(api, runner["id"])
        if entry and not entry.get("invalid"):
            return entry
        api.request("DELETE", f"/api/runners/{urllib.parse.quote(runner['id'])}")
    pytest.skip("no generator's Default yields a runnable runner without an account")


def test_manage_view_runner_card_edits_duplicates_then_deletes(page, api, evidence):
    """A runner's card in the manage view: Edit opens the editor in place of
    the list and Save writes it back; Duplicate adds a "<name> copy" runner
    with the same settings, in the same cell; Delete asks in a strip on the
    card first, and its Delete removes the runner and its card."""
    name = f"Doomed runner {uuid.uuid4().hex[:6]}"
    entry = _valid_runner(api, name)
    copy_id = None
    manage = ManageView(page)
    try:
        manage.open()
        manage.search(name)
        card = manage.card(name)
        card.wait_for()
        assert card.locator("[data-runner-status]").get_attribute("data-runner-status") != "broken"

        # Edit: the editor replaces the list; Save brings the list back.
        card.get_by_role("button", name="Edit", exact=True).click()
        editor = manage.editor()
        editor.wait_for()
        assert editor.get_by_label("Name", exact=True).input_value() == name
        editor.get_by_label("Description").fill("Edited in Capabilities")
        editor.get_by_role("button", name="Save", exact=True).click()
        manage.root.get_by_role("status").filter(has_text=f"Runner ‘{name}’ saved.").wait_for()
        settles(lambda: (runner_entry(api, entry["id"]) or {}).get("description") == "Edited in Capabilities",
                label="the edit to be saved")
        card.wait_for()
        assert "Edited in Capabilities" in card.inner_text()

        # Duplicate: a new runner, same settings and cell, "<name> copy".
        card.get_by_role("button", name="Duplicate", exact=True).click()
        manage.card(f"{name} copy").wait_for(timeout=30000)
        copied = next(r for r in api.request("GET", "/api/runners") if r["name"] == f"{name} copy")
        copy_id = copied["id"]
        assert copied["id"] != entry["id"]
        assert copied["generator"] == entry["generator"] and copied["settings"] == entry["settings"]
        assert copied["cell_id"] == entry["cell_id"]
        shot(page, evidence, "manage-runner-duplicated")

        # Delete: the confirm strip on the card deletes nothing until its Delete.
        card.get_by_role("button", name="Delete", exact=True).click()
        card.get_by_text(f"Delete ‘{name}’?", exact=False).wait_for()
        assert runner_entry(api, entry["id"]) is not None, "the confirm strip deletes nothing yet"
        shot(page, evidence, "manage-runner-delete-confirm")
        card.get_by_role("button", name="Delete", exact=True).last.click()
        card.wait_for(state="detached")
        assert runner_entry(api, entry["id"]) is None
        assert runner_entry(api, copy_id) is not None, "deleting the original keeps its copy"
    finally:
        manage.close()
        for rid in (entry["id"], copy_id):
            if rid and runner_entry(api, rid):
                api.request("DELETE", f"/api/runners/{urllib.parse.quote(rid)}")


def test_runners_panel_run_posts_the_run_for_the_selected_runner(page, api, cell_view, evidence):
    """The runners panel only runs: no mode strip and no editor. Enter on the
    selected row dispatches POST /api/channels/{ch}/runs for that runner; the
    request is intercepted so nothing actually runs."""
    name = f"Runnable {uuid.uuid4().hex[:6]}"
    entry = _valid_runner(api, name)
    posted: list[dict] = []
    panel = None

    def intercept(route, request):
        posted.append(request.post_data_json or {})
        route.fulfill(status=400, content_type="application/json",
                      body='{"detail": "live suite: run intercepted"}')

    try:
        page.route("**/api/channels/*/runs", intercept)
        panel = open_runners_panel(page, cell_view, api, "panel-run")
        panel.root.get_by_label("Search runners").fill(name)
        row = panel.row(name)
        row.wait_for()
        settles(lambda: row.get_attribute("aria-selected") == "true", label="the match to be selected")
        assert panel.root.get_by_role("group", name="Mode").count() == 0, "the panel has no Edit or Add mode"
        for verb in ("Edit", "Add", "Duplicate", "Delete"):
            assert panel.root.get_by_role("button", name=verb, exact=True).count() == 0, f"no {verb} in the panel"
        page.keyboard.press("Enter")
        settles(lambda: bool(posted), timeout=20, label="Enter in Run mode to post the run")
        body = posted[0]
        assert body.get("runner") == entry["id"], body
        assert body.get("cell_id") == panel.view.cell_id, body
        assert not body.get("new_cell"), body
        panel.root.get_by_role("alert").filter(has_text="run intercepted").wait_for()
        shot(page, evidence, "panel-run-rejected")
    finally:
        page.unroute("**/api/channels/*/runs")
        if panel:
            panel.close()
        api.request("DELETE", f"/api/runners/{urllib.parse.quote(entry['id'])}")
