"""Shared setup for the demo tests.

The timelines are imported straight from ``playground/demo/generators`` by
path; bytecode writing is off so the tree the build vendors never grows a
``__pycache__``. The app tests import the app's ``api`` package, which
resolves its imports from ``api/`` as a top-level path (the same way
``api/tests`` does), and boot a fresh app per test on bundled trees staged
the way the build stages them, holding a generators tree the test names
(``app_client``).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True

API_DIR = Path(__file__).resolve().parents[3] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

# The app's own module cache must not leak between app instances: these are
# the module roots api/tests/conftest.py drops for the same reason, including
# their children so extracted routers and manager/session services bind the
# current app instance rather than a previous test's state.
APP_MODULES = (
    "main", "state", "database", "genesis", "process_manager", "channel_config", "routes", "sessions",
)


def _repo_root() -> Path:
    import collimate
    return Path(collimate.__file__).resolve().parents[3]


def _build_vendor():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "build" / "vendor.py"
    spec = importlib.util.spec_from_file_location("build_vendor", path)
    vendor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vendor)
    return vendor


def stage_bundled(target: Path, generators_dir: Path) -> Path:
    """The bundled trees under ``target`` exactly as the build stages them
    into the staged api (build/vendor.py ``stage_bundled``):
    ``collimate_content/`` holding ``generators_dir``, the repo's renderers
    and the demo suggestion, and ``collimate_builtins/``. Returns the
    content tree."""
    return _build_vendor().stage_bundled(target, generators_dir, _repo_root())


def create_demo_channel(client, name: str, runners: list):
    """``POST /api/channels`` from the demo suggestion, as boot.py sends it."""
    return client.post("/api/channels", params={"name": name}, json={
        "suggested": {"id": "demo", "tutorial": False, "runners": runners}})


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """``app_client(generators_dir)`` → a TestClient on a fresh app, cwd a
    temp dir, whose bundled trees are staged as the build stages them
    (:func:`stage_bundled`, holding ``generators_dir``) in a directory put
    first on ``sys.path``, as pyodide has the staged api: the app finds them
    through its bundled default and no suggested source is named, so
    :func:`create_demo_channel` creates the demo channel as the demo does
    (``client.content`` is the content tree). The TestClient's ``Host:
    testserver`` is trusted the way api/tests/conftest.py trusts it."""
    from fastapi.testclient import TestClient

    clients: list = []

    def make(generators_dir: Path) -> TestClient:
        monkeypatch.chdir(tmp_path)
        bundled = tmp_path / f"bundled-{len(clients) + 1}"
        content = stage_bundled(bundled, generators_dir)
        monkeypatch.syspath_prepend(str(bundled))
        for name in tuple(sys.modules):
            if any(name == root or name.startswith(root + ".") for root in APP_MODULES):
                sys.modules.pop(name, None)
        genesis = importlib.import_module("genesis")
        assert (genesis.GENERATORS_DIR, genesis.RENDERERS_DIR, genesis.BUILTIN_DIR) == (
            str(content / "generators"), str(content / "renderers"), str(bundled / "collimate_builtins"))
        main = importlib.import_module("main")
        from auth_middleware import origin_guard
        monkeypatch.setattr(origin_guard, "trusted_hosts", origin_guard.trusted_hosts | {"testserver"})
        client = TestClient(main.app)
        client.__enter__()
        clients.append(client)
        client.content = content
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def refusal_overlay_module(name, shim, monkeypatch):
    """Canonical ``api/<name>.py`` against the confined build's deadline patch:
    ``verifier_pool`` executes from its canonical bytes with the build's
    patches applied, then ``name`` from its own, so it binds the patched
    verdict constructor exactly as the staged tree does."""
    import importlib.util
    import types
    root = Path(__file__).resolve().parents[3]
    path = root / 'playground/build/patches.py'
    spec = importlib.util.spec_from_file_location('refusal_build_patches', path)
    patches = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, patches)
    spec.loader.exec_module(patches)
    monkeypatch.setitem(sys.modules, 'collimate_playground.verification', shim)
    applied = 0
    for module_name in dict.fromkeys(('verifier_pool', name)):
        source_path = root / 'api' / (module_name + '.py')
        source = source_path.read_text()
        for patch in patches.PATCHES:
            if patch.path == 'api/' + module_name + '.py':
                assert source.count(patch.anchor) == 1
                source = source.replace(patch.anchor, patch.replacement)
                applied += 1
        module = types.ModuleType(module_name)
        module.__file__ = str(source_path)
        monkeypatch.setitem(sys.modules, module_name, module)
        exec(compile(source, str(source_path), 'exec'), module.__dict__)
    assert applied, 'the build no longer patches the verdict constructor'
    return module
