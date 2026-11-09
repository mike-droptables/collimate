"""Pytest fixtures for api/tests/test_pytest_*.py.

The api package bootstraps heavy module-level state (database paths,
genesis seeding, keystore). All of it keys off the current working
directory, so each test that wants a clean slate gets a fresh temp cwd
and a fresh module import.

Existing standalone scripts (``test_channel_ops.py`` etc.) do the same
thing manually in their ``main()``; we encapsulate it once here for
pytest-discovered tests.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

# The root pyproject.toml collects test_*.py for both suites; these are the
# standalone harness scripts test_pytest_standalone.py runs as subprocesses.
collect_ignore = ["test_audit_regressions.py", "test_channel_ops.py", "test_new_sdk_flow.py"]

API_DIR = Path(__file__).resolve().parent.parent


def live_suite(name: str):
    """A module of the server's live suite (api/tests_live), imported under
    the package name ``api_tests_live``: when both suites run in one process
    the SDK's live suite holds the top-level name ``tests_live``."""
    import importlib.util
    package = "api_tests_live"
    if package not in sys.modules:
        root = API_DIR / "tests_live"
        spec = importlib.util.spec_from_file_location(package, root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{package}.{name}")

# Test scratch goes to the repo's git-ignored ``tmp/``, not the machine's
# /tmp (often a small RAM-backed tmpfs). Pytest still numbers its runs and
# keeps only the last three there, so concurrent suites stay safe.
_TEMPROOT = API_DIR.parent / "tmp"
_TEMPROOT.mkdir(exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_TEMPROOT))
# The account registry too, so a server's startup lease sweep (or any account
# path a test reaches without the ``registry`` fixture) never locks, reads or
# sweeps the developer's own registry and its runtime folder.
os.environ["COLLIMATE_ACCOUNTS_CONFIG"] = str(_TEMPROOT / "test-accounts" / "accounts.json")
# The default channel location too (fresh_cwd gives each test its own): the
# server's own default is <data dir>/Channels, the CWD's, so a test that never
# changes directory would otherwise make api/Channels in the checkout.
os.environ["COLLIMATE_CHANNELS"] = str(_TEMPROOT / "test-channels")


@pytest.fixture(autouse=True, scope="session")
def _trust_testclient_host():
    """A tokenless server answers only loopback Host names; Starlette's
    TestClient sends ``Host: testserver``. Trust that one name for tests."""
    from collimate_server.auth_middleware import origin_guard
    origin_guard.trusted_hosts.add("testserver")
    yield
    origin_guard.trusted_hosts.discard("testserver")


@pytest.fixture(autouse=True, scope="session")
def _never_signal_every_process():
    """``os.killpg(1)`` is ``kill(-1)``: every process of the user, the
    desktop session included. A mock's ``pid`` indexes as 1, and a cleanup
    path handed a fake process once logged the developer out mid-suite.
    Refuse such a target in every worker and fail the test instead."""
    import os
    real_kill, real_killpg = os.kill, os.killpg

    def guarded(name, real):
        def call(pid, sig, *rest):
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
                raise AssertionError(f"os.{name}({pid!r}, {sig}) would signal every process of the user")
            return real(pid, sig, *rest)
        return call

    os.kill, os.killpg = guarded("kill", real_kill), guarded("killpg", real_killpg)
    yield
    os.kill, os.killpg = real_kill, real_killpg


@pytest.fixture(autouse=True)
def _no_machine_harness_probe(monkeypatch):
    """The host-harness probe searches the developer's real PATH and install
    roots and runs whatever CLI it finds for ``--version`` — so a key check's
    wording would depend on what this machine has installed, and the suite
    would execute the user's own claude/codex. Default it to "not on this
    machine"; a test that cares sets its own probe over this one."""
    try:
        from collimate import host_harness
    except ImportError:  # the SDK is optional for some slices of the suite
        return
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: None)


def sdk_grant(perms):
    """The SDK ``Grant`` a host grant must arrive as, built field by field from
    the app's ``RunnerPermissions`` (the SDK checks every field on
    construction). A test compares what the run parses out of
    ``COLLIMATE_PERMISSIONS`` with this, never with a second parse of the
    same JSON, so a value the SDK would floor on the way in fails the test."""
    from collimate.permissions import AXES, Grant
    return Grant(**{axis: getattr(perms, axis) for axis in AXES})


@pytest.fixture
def fresh_cwd(tmp_path, monkeypatch):
    """Chdir into a clean temp directory so bootstrap writes under it, with
    its default channel location at ``tmp_path/'Channels'``.

    Nothing else touches the real user's ~/.collimate, ~/Collimate or cwd.
    (``tmp_path/'Channels'`` is also the server's own default here, the data
    dir's ``Channels``; the variable keeps it so with or without the chdir.)
    The schema-init cache in ``database._initialised_dbs`` and the channel
    index (``channel_paths``) are keyed by path and data dir, so distinct
    ``tmp_path``s naturally get distinct entries — each test sees a fresh,
    uninitialised channel DB.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIMATE_CHANNELS", str(tmp_path / "Channels"))
    yield tmp_path


@pytest.fixture(autouse=True)
def _own_channel_location(tmp_path, monkeypatch):
    """Each test's default channel location is its own (fresh_cwd's is the
    same ``tmp_path/'Channels'``): two data dirs of different tests never
    meet in one location and refuse each other."""
    monkeypatch.setenv("COLLIMATE_CHANNELS", str(tmp_path / "Channels"))


@pytest.fixture(autouse=True)
def _no_startup_flags(monkeypatch):
    """``--channels-dir`` and ``--claim-channels`` are per process
    (``channel_paths.configure_startup``); a test that sets them leaves the
    next one clean."""
    import collimate_server.channel_paths as channel_paths
    monkeypatch.setattr(channel_paths, "_startup", {"channels_dir": None, "claim": False})


def channel_home(channel=None, *, name: str = "Test") -> str:
    """Make the folder of ``channel`` (a ``Channel``, an id, or None for a
    new id) in the data dir's default channel location with its empty
    ``channel.db``, as a channel's creation does, establishing the data
    dir's locations first when nothing did; answers the id. For a test that
    puts a channel in the live state or opens a channel DB without going
    through ``POST /api/channels``."""
    import uuid as _uuid
    import collimate_server.channel_paths as channel_paths
    from collimate_server.database import get_db_connection
    if not channel_paths.is_established():
        channel_paths.establish()
    identity = str(getattr(channel, "id", None) or channel or _uuid.uuid4())
    if channel_paths.home(identity) is None:
        channel_paths.begin_create(identity, getattr(channel, "name", None) or name)
        get_db_connection(identity).close()
        channel_paths.finish_create(identity)
    return identity


@pytest.fixture(autouse=True)
def _channel_homes_on_demand(request, monkeypatch):
    """Most tests put channels straight into the live state, or open a
    channel DB by a made-up id, without a channel's creation: give such an
    id its folder (:func:`channel_home`) the first time anything resolves
    it, as creation would have. A test marked ``real_channel_homes`` (the
    folder, location and deletion tests) sees the resolver as the server
    does: an id nothing created is unavailable."""
    if request.node.get_closest_marker("real_channel_homes"):
        return
    import collimate_server.channel_paths as channel_paths
    resolve = channel_paths.channel_dir

    def channel_dir(channel_id):
        try:
            return resolve(channel_id)
        except channel_paths.ChannelNotFound as missing:
            if missing.reason != "it has no channel folder":
                raise
            identity = missing.channel_id
            if Path.cwd() == API_DIR or Path.cwd() == API_DIR.parent:
                raise
            channel_home(identity)
            return resolve(identity)

    monkeypatch.setattr(channel_paths, "channel_dir", channel_dir)


def add_capabilities_cell(channel, cell) -> None:
    """Append ``cell`` to ``channel``'s Capabilities stack (ensured first)
    and write its files as its accepted revision. Does not persist the
    layout."""
    from collimate_server.cell_placement import capabilities_stack
    from collimate_server.database import get_db_connection
    from collimate_server.genesis import ensure_capabilities_stack
    from collimate_server.persistence import initialize_files

    ensure_capabilities_stack(channel)
    capabilities_stack(channel).cells.append(cell)
    conn = get_db_connection(str(channel.id))
    try:
        initialize_files(conn, str(cell.id), cell.files)
        conn.commit()
    finally:
        conn.close()


def install_handler(channel, *, identity="document", ext=".doc", accepted=True):
    """Add a synthetic handler cell (``<identity>/choice.handler`` on ``ext``,
    with a verifier and one skill under ``derived/``) to ``channel``'s
    Capabilities stack. ``accepted`` declares the derived folder's hash and
    stores it as an accepted build, as Review and accept does. Answers
    ``(cell, manifest, derived)``."""
    import yaml
    from collimate.handlers import derived_hash
    from collimate_server.handlers import save_derived_skill
    from collimate_server.models import Cell, VirtualFile

    derived = {"verify.ts": b"export default () => ({valid:true});",
               "skills/doc/SKILL.md": b"# Write a document"}
    manifest = {"id": identity, "name": identity, "description": "Test type", "guidance": "A title",
                "types": [{"ext": ext}], "examples": []}
    if accepted:
        manifest["derived"] = derived_hash(derived)
        save_derived_skill(manifest["id"], manifest["derived"], derived)
    cell = Cell(name=identity, files=[VirtualFile(name=f"{identity}/choice.handler", content=yaml.safe_dump(manifest)),
                *(VirtualFile(name=f"{identity}/derived/{name}", content=body.decode()) for name, body in derived.items())])
    add_capabilities_cell(channel, cell)
    return cell, manifest, derived


@pytest.fixture
def fake_run():
    """Arm a fake active run on a cell id (``fake_run(cell_id)``), as the run
    registry's guards see one; unregistered on teardown."""
    from types import SimpleNamespace
    from collimate_server.run_registry import active_managers

    run_id = "test-run-guard"

    def _arm(cell_id: str):
        active_managers[run_id] = SimpleNamespace(cell_id=cell_id)

    yield _arm
    active_managers.pop(run_id, None)


def install_test_generator(
    *,
    name: str = "test_echo",
    manifest_extra: str = "",
    entrypoint_body: str = "print('test_echo ran')\n",
    draft_files: dict[str, str] | None = None,
    channel=None,
) -> str:
    """Inject a self-contained generator into the active channel.

    Tests that need a working generator should call this rather than
    relying on whatever ``genesis`` happens to seed — the seed catalog
    is owned by the SDK and shifts independently of the API test suite.
    Each caller declares the manifest + entrypoint it actually needs.

    Every generator is single-shaped now (exactly one input cell, rewritten
    in place) — there is no input-type field to declare.

    Returns the generator id (== ``name``) so tests can pass it as
    ``generator_id`` / ``generator_name`` in API calls. The cell joins the
    channel's Capabilities stack; its files land at
    ``{name}/choice.generator``, ``{name}/{name}.py``, plus any
    ``draft_files`` entries the test requested.
    """
    import uuid as _uuid
    from collimate_server.state import state, persist_state
    from collimate_server.models import VirtualFile, Cell

    manifest_lines = [
        f"id: {name}",
        f"name: {name}",
        f'summary: "Test generator {name}."',
        f"entrypoint: {name}.py",
    ]
    if draft_files:
        manifest_lines.append("draft_files:")
        for rel in draft_files:
            manifest_lines.append(f"  - {rel}")
    if manifest_extra:
        manifest_lines.append(manifest_extra.rstrip())
    manifest = "\n".join(manifest_lines) + "\n"

    files = [
        VirtualFile(
            name=f"{name}/choice.generator",
            content=manifest,
            language="yaml",
        ),
        VirtualFile(
            name=f"{name}/{name}.py",
            content=entrypoint_body,
            language="python",
        ),
    ]
    for rel, content in (draft_files or {}).items():
        files.append(VirtualFile(
            name=f"{name}/{rel}",
            content=content,
            language="markdown",
        ))

    src_cell = Cell(
        id=_uuid.uuid4(),
        name="TestGenerators",
        files=files,
    )
    # Install into the given channel (for channel-isolation tests) or the
    # active one by default.
    ch = channel or next(c for c in state.channels if c.id == state.active_channel_id)
    add_capabilities_cell(ch, src_cell)
    persist_state()
    return name


def install_runner(*, generator="runner_echo", runner=None,
                   entrypoint_body="print('runner_echo ran')\n", draft_files=None,
                   gen_manifest_extra="", config_folders=None, definition=None,
                   files=None, channel=None):
    """Install one native generator and its complete saved runner definition.

    Returns (runner_id, generator_id). ``definition`` overrides editable runner
    fields once while constructing the fixture; execution accepts no overrides.
    ``files`` maps supplemental paths (no main config) to text or bytes.
    """
    import base64
    import yaml
    import collimate_server.runners as runners
    from collimate_server.state import state
    ch = channel or next(c for c in state.channels if c.id == state.active_channel_id)
    drafts = dict(draft_files or {})
    main = next((rel for rel in drafts if rel.endswith(".set_yaml")), "settings.set_yaml")
    drafts.setdefault(main, "fields: []\n")
    extra = f"main_config: {main}\n" + gen_manifest_extra
    if config_folders:
        extra += "\n" + yaml.safe_dump({"config_folders": [{"name": name, "description": name} for name in config_folders]})
    install_test_generator(name=generator, entrypoint_body=entrypoint_body,
                           draft_files=drafts, manifest_extra=extra, channel=ch)
    from collimate_server.cell_placement import capabilities_stack_cells
    gi = runners.generator_index(capabilities_stack_cells(ch))[generator]
    identity = (runner or generator).lower().replace("_", "-")
    data = runners.config_template(gi, "default")["runner"]
    data.update(definition or {})
    data.update(id=identity, generator=generator)
    data["settings"] = {main: {**gi["defaults"], **data.get("settings", {}).get(main, {})}}
    payload = [{"name": n, "content": base64.b64encode(v).decode() if isinstance(v, bytes) else v,
                **({"encoding":"base64"} if isinstance(v, bytes) else {})} for n,v in (files or {}).items()]
    runners.save_runner(ch, data, payload, create=True)
    return identity, generator


@pytest.fixture
def test_generator(client):
    """Install ``test_echo`` into the active channel and return its id.

    Use this in any test that needs a real, lookup-able generator.
    Avoid baking in dependencies on ``genesis``-seeded generators — the
    seed catalog is owned by the SDK and changes independently.
    """
    return install_test_generator()


def install_test_renderer(
    *,
    renderer_id: str = "test_md",
    name: str = "Test Markdown",
    extensions: list[str] | None = None,
    supports_diff: bool = False,
    embed: str = "iframe",
    main_content: str = "// test renderer source\n",
) -> str:
    """Inject a self-contained renderer into the active channel.

    Writes a ``view.renderer`` and a ``main.tsx`` under
    ``{renderer_id}/`` in a cell on the channel's Capabilities stack, so
    ``/api/renderers`` discovers it through the same path the SDK-seeded
    renderers use. Returns ``renderer_id``.
    """
    import uuid as _uuid
    from collimate_server.state import state, persist_state
    from collimate_server.models import VirtualFile, Cell

    extensions = extensions or [".md"]
    manifest_lines = [
        f"id: {renderer_id}",
        f"name: {name}",
        "version: 0.0.0",
        f"embed: {embed}",
        "entrypoint: main.tsx",
        f"supports_diff: {str(supports_diff).lower()}",
        "extensions:",
        *(f"  - \"{ext}\"" for ext in extensions),
    ]
    manifest = "\n".join(manifest_lines) + "\n"

    src_cell = Cell(
        id=_uuid.uuid4(),
        name="TestRenderers",
        files=[
            VirtualFile(
                name=f"{renderer_id}/view.renderer",
                content=manifest,
                language="yaml",
            ),
            VirtualFile(
                name=f"{renderer_id}/main.tsx",
                content=main_content,
                language="typescript",
            ),
        ],
    )
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    add_capabilities_cell(ch, src_cell)
    persist_state()
    return renderer_id


@pytest.fixture
def test_renderer(client):
    """Install ``test_md`` into the active channel and return its id."""
    return install_test_renderer()


_API_MODULE_ROOTS = (
    "main", "state", "database", "genesis", "process_manager", "channel_config", "cell_metadata", "routes", "sessions",
    "git_host", "repository_leases", "git_store", "git_materialize", "git_harvest",
)


def reset_api_modules():
    """Refresh cwd-bound app state and every owned package descendant.

    A package-only pop leaves its children cached, so the next app would
    reuse old ProcessManager mixins and router bindings. Keep unrelated
    imports intact; all three app fixture variants share this boundary.
    """
    roots = tuple(f"collimate_server.{root}" for root in _API_MODULE_ROOTS)
    for name in tuple(sys.modules):
        if any(name == root or name.startswith(root + ".") for root in roots):
            module = sys.modules.pop(name, None)
            parent, _, child = name.rpartition(".")
            # ``from collimate_server import x`` reads the package attribute
            # before sys.modules, so the attribute goes with the entry.
            if getattr(sys.modules.get(parent), child, None) is module:
                delattr(sys.modules[parent], child)


@pytest.fixture
def api_app(fresh_cwd, monkeypatch):
    """Fresh FastAPI app + state for each test.

    api.main and api.state carry process-global singletons; we invalidate
    them so the temp cwd is picked up. The TestClient's lifespan runs
    bootstrap (genesis seeding, db create) inside the fixture.
    """
    # Startup sweeps and actual account materialization must share this
    # fixture's storage; changing tempfile globally would lengthen IPC paths.
    import collimate_server.accounts as accounts
    runtime = fresh_cwd / "account-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)

    reset_api_modules()

    main = importlib.import_module("collimate_server.main")
    try:
        yield main.app
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture
def channel_client(api_app):
    """A live client and the UUID returned by real channel creation.

    The native app no longer auto-seeds a starter channel (it opens to the
    channel-creation view). Positive API tests address the created UUID and
    keep Starting only as its display label.
    """
    from fastapi.testclient import TestClient

    with TestClient(api_app) as c:
        response = c.post("/api/channels", params={"name": "Starting"})
        assert response.status_code == 200, response.text
        created_id = response.json()["id"]
        c.channel_id = created_id
        yield c, created_id


@pytest.fixture
def client(channel_client):
    return channel_client[0]


def create_suggested_channel(client, name: str, *, tutorial: bool = False) -> str:
    """Create (and make active) a channel from the Standard suggestion of
    the catalog's default source, the bundled content (the shipped generators
    and renderers), with no runner drafts; answers its id."""
    response = client.post("/api/channels", params={"name": name},
                           json={"suggested": {"id": "standard", "tutorial": tutorial}})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
def created_channel_id(channel_client):
    """The registered UUID captured from the shared client's create response."""
    return channel_client[1]


@pytest.fixture
def fds_above_select_ceiling():
    """This process's next descriptors numbered 1024 and above, where
    ``select.select`` raises: the desktop shell starts the server with every
    slot below its own highest held at /dev/null (serverStdio), so the host's
    sockets live there. Holds /dev/null open until the next free number is
    past the ceiling; raises the soft open-files limit when it is in the way."""
    import resource
    import socket

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = 1024 + 256
    if soft < need:
        if hard != resource.RLIM_INFINITY and hard < need:
            pytest.skip(f"open-files hard limit {hard} is below {need}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
    held = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY)
            held.append(fd)
            if fd >= 1024:
                break
        with socket.socket() as probe:
            assert probe.fileno() >= 1024
        yield
    finally:
        for fd in held:
            os.close(fd)
        if soft < need:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
