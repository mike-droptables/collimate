# SPDX-License-Identifier: MIT
"""Offline SDK fixtures; the common root conftest owns native_bridge."""
from __future__ import annotations

import sys

import pytest
from contextlib import contextmanager
from copy import deepcopy

from tests._workspace import BUILTINS, GENERATORS, RENDERERS

# The two renderer trees: the seedable renderer cells and the app's built-in
# cells. Whole-tree tests iterate both.
RENDERER_ROOTS = (RENDERERS, BUILTINS)

# The shipped generators' modules (``_agent_shared``, ``_env_shared``,
# ``_web_shared`` and the packages) import as top-level names, as in a run.
sys.path.insert(0, str(GENERATORS))

# The git engine's fixtures (fake remote, fake ssh, git spy, SSH keys), shared by the
# engine and Git generator tests.
from tests._git_fixtures import fake_remote, fake_ssh, git_spy, ssh_keys  # noqa: E402,F401


@pytest.fixture
def no_machine_harness_probe(monkeypatch):
    """Parser tests never execute the developer's installed CLI for --version.

    Tests of the probe/placement contract explicitly provide their own probe.
    """
    from collimate import host_harness
    monkeypatch.setattr(host_harness, "probe", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _private_cache_root(tmp_path_factory, monkeypatch):
    """Every test gets its own cache root (``XDG_CACHE_HOME``): the agent
    engine keeps session state, resume records and adapter prefixes there,
    and a test must neither read nor write the machine's real one."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))


@pytest.fixture
def host_free_run(tmp_path):
    """Run the test inside a host-free run whose grant is unrestricted and
    where a human can answer approval cards (``approvals=[]``: can ask, no
    scripted answers). For code that reads this run's facts —
    ``gen.grant()``, ``gen.can_ask()`` — without the test passing them."""
    from collimate import native
    from collimate.testing import make_test_bridge

    workspace = tmp_path / "_host_free_run"
    workspace.mkdir()
    bridge = make_test_bridge(workspace, approvals=[])
    native.set_bridge(bridge)
    try:
        yield bridge
    finally:
        native._clear_bridge()
        bridge.close()


@pytest.fixture
def approval_run(tmp_path, monkeypatch):
    """``approval_run(approvals)`` installs a fresh host-free run whose chat
    session is being served — as inside a ``gen.serve_chat`` turn, the only
    place an approval card is shown and answered — and whose scripted human
    answers ``gen.ask`` cards from ``approvals`` (see
    :func:`collimate.testing.run_generator`). Returns the run's bridge, whose
    ``stream_events`` and ``phases`` are live. Each call replaces the run."""
    from collimate import native
    from collimate.testing import make_test_bridge

    made: list = []

    def install(approvals):
        workspace = tmp_path / f"_approval_run_{len(made)}"
        workspace.mkdir()
        bridge = make_test_bridge(workspace, approvals=approvals)
        native.set_bridge(bridge)
        monkeypatch.setattr(native, "_serving_chat", True)
        # The inbox serve_chat registered can show the card (its .chat surface).
        monkeypatch.setattr(native, "_inbox_shows_cards", [True])
        made.append(bridge)
        return bridge

    try:
        yield install
    finally:
        native._clear_bridge()
        for bridge in made:
            bridge.close()


@pytest.fixture
def bridge_hook(monkeypatch):
    """Intercept the bridge :func:`collimate.testing.run_generator` builds
    internally so a test can patch host-facing methods on it.

    Usage::

        bridge_hook["setup"] = lambda b: setattr(b, "update_cell", update)
        res = run_generator("generators/x/x.py", ...)
        bridge = bridge_hook["bridge"]     # the instance the run used
    """
    import collimate.testing as ct

    holder: dict = {}
    orig = ct.make_test_bridge

    def wrapper(workspace, **kw):
        bridge = orig(workspace, **kw)
        holder["bridge"] = bridge
        setup = holder.get("setup")
        if setup is not None:
            setup(bridge)
        return bridge

    monkeypatch.setattr(ct, "make_test_bridge", wrapper)
    return holder


@pytest.fixture(autouse=True)
def isolated_agent_secret_registry():
    """Agent credentials are process-scoped; each test models a fresh run."""
    import sys
    module = sys.modules.get('_agent_shared.secret_hygiene')
    if module is not None:
        module.clear()
    yield
    module = sys.modules.get('_agent_shared.secret_hygiene')
    if module is not None:
        module.clear()


@pytest.fixture
def cache_root(request, monkeypatch, tmp_path):
    """Root backend homes and login stores inside an owned host-free cell."""
    from tests._agent_run import in_cell
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    in_cell(request, monkeypatch, tmp_path / "run-ws", "cell-1")
    return tmp_path


@pytest.fixture
def account_runtime(tmp_path, monkeypatch):
    """Create a module-selected account policy without installing one globally."""
    @contextmanager
    def install(snapshot, *, selectable=False):
        import gen
        from _agent_shared import login_store, harness_homes
        login_store.close_account_credentials()
        harness_homes.close_harness_homes()
        root = tmp_path / "account-runtime"
        root.mkdir()
        monkeypatch.setattr(gen, "account", lambda role: snapshot.copy() if selectable else deepcopy(snapshot))
        monkeypatch.setattr(gen, "materialize_account", lambda role: {"path": str(root), "lease_id": "test-lease"})
        monkeypatch.setattr(gen, "release_account", lambda *args: {"committed": True})

        def select_plan(plan):
            from collimate.account_taxonomy import PLAN_PROVIDERS
            if plan not in PLAN_PROVIDERS:
                return
            login_store.close_account_credentials()
            snapshot["service"] = PLAN_PROVIDERS[plan]
            snapshot["metadata"] = {"harness": snapshot["metadata"]["harness"], "plan": plan}

        try:
            yield root, select_plan
        finally:
            login_store.close_account_credentials()
            harness_homes.close_harness_homes()
    return install
