"""Per-run optional dependencies — manifest ``optional_dependencies`` → the
``uv run --with`` tail both launch paths build.

A generator declares which distributions a config VALUE pulls in; the host
resolves the active group from the run's already-merged config and installs
exactly that group. The host stays generator-agnostic (it never learns what
``engine-a`` means), so everything here is pinned with synthetic manifests —
plus canonical SDK discovery that checks every declared group without naming a
shipped generator.

Semantics pinned:
  * the selected value's group ⇒ one ``--with`` per requirement, declaration
    order, deduped; the merged config value (tag settings ⊕ runner edits) is
    what decides, not the draft default;
  * a value with NO group, a missing selector key, an absent selector file, or
    a malformed block ⇒ no extras at all;
  * ``update_packages`` on the grant ⇒ ``--upgrade-package <name>`` per named
    requirement (``-P`` is the only flag uv honours past its satisfied-check);
    off ⇒ no ``-P`` anywhere;
  * the native spawn and the ``docker run`` tail build the SAME argv — a grant
    that only moves where the run lands can't change what it installs;
  * the ``spinning_up`` sub-message names the install (a cold group downloads
    before the generator can report anything and otherwise reads as a hang).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from set_yaml import overlay_set_yaml_values
from process_manager.optional_deps import (
    install_phase_message,
    requirement_name,
    resolve_optional_requirements,
    uv_run_argv,
)

# A synthetic converter fixture: one dropdown, one group per interesting value.
CONFIG_REL = "fixture-config.set_yaml"

FIXTURE_CONFIG = """\
fields:
  - key: backend
    type: dropdown
    label: Engine
    options:
      - bundled
      - engine-a
      - engine-b
    default: bundled
"""

MANIFEST = {
    "id": "fixture-converter",
    "entrypoint": "fixture.py",
    "draft_files": [CONFIG_REL],
    "main_config": CONFIG_REL,
    "optional_dependencies": {
        "selector": {"file": CONFIG_REL, "key": "backend"},
        "groups": {
            "engine-a": ["engine-a[bridge]"],
            "engine-b": ["engine-b", "bridge"],
        },
    },
}


def _seeded(**values) -> dict:
    """The ``{rel: content}`` a run is seeded with — built through the SAME
    comment-preserving overlay create_run uses, so the resolver
    is fed the bytes the generator will actually read."""
    content = overlay_set_yaml_values(FIXTURE_CONFIG, values) if values else FIXTURE_CONFIG
    return {CONFIG_REL: content}


# ===========================================================================
# Group resolution
# ===========================================================================

def test_selected_group_resolves_to_its_requirements():
    assert resolve_optional_requirements(MANIFEST, _seeded(backend="engine-a")) == [
        "engine-a[bridge]"
    ]
    assert resolve_optional_requirements(MANIFEST, _seeded(backend="engine-b")) == [
        "engine-b", "bridge",
    ]


def test_draft_default_decides_when_no_overlay():
    """No overlay ⇒ the draft's own default (bundled, no group) ⇒ nothing."""
    assert resolve_optional_requirements(MANIFEST, _seeded()) == []


def test_value_without_a_group_installs_nothing():
    assert resolve_optional_requirements(MANIFEST, _seeded(backend="bundled")) == []


def test_unknown_value_installs_nothing():
    """A value the manifest never named (a stale overlay, a newer dropdown)
    must not fall back to some other group."""
    assert resolve_optional_requirements(MANIFEST, _seeded(backend="unknown-engine")) == []


def test_absent_selector_key_installs_nothing():
    """The selector points at a key this config file doesn't declare."""
    manifest = dict(MANIFEST)
    manifest["optional_dependencies"] = {
        "selector": {"file": CONFIG_REL, "key": "harness"},
        "groups": {"engine-a": ["engine-a[bridge]"]},
    }
    assert resolve_optional_requirements(manifest, _seeded(backend="engine-a")) == []


def test_absent_selector_file_installs_nothing():
    assert resolve_optional_requirements(MANIFEST, {"other.set_yaml": FIXTURE_CONFIG}) == []
    assert resolve_optional_requirements(MANIFEST, {}) == []
    assert resolve_optional_requirements(MANIFEST, None) == []


def test_no_declaration_installs_nothing():
    assert resolve_optional_requirements({"id": "web"}, _seeded(backend="engine-a")) == []
    assert resolve_optional_requirements(None, _seeded(backend="engine-a")) == []


@pytest.mark.parametrize("block", [
    "not-a-mapping",
    {"groups": {"engine-a": ["x"]}},                     # no selector
    {"selector": {"file": CONFIG_REL, "key": "backend"}},  # no groups
    {"selector": {"key": "backend"}, "groups": {"engine-a": ["x"]}},
    {"selector": {"file": CONFIG_REL, "key": "backend"}, "groups": ["engine-a"]},
    {"selector": {"file": CONFIG_REL, "key": "backend"},
     "groups": {"engine-a": "engine-a[bridge]"}},     # group not a list
])
def test_malformed_declaration_degrades_to_nothing(block):
    """A wrong declaration must not take the run down — it just adds nothing."""
    manifest = dict(MANIFEST, optional_dependencies=block)
    assert resolve_optional_requirements(manifest, _seeded(backend="engine-a")) == []


def test_group_entries_are_deduped_and_ordered():
    manifest = dict(MANIFEST, optional_dependencies={
        "selector": {"file": CONFIG_REL, "key": "backend"},
        "groups": {"engine-a": ["b", "a", " b ", "", 7, None, "a"]},
    })
    assert resolve_optional_requirements(manifest, _seeded(backend="engine-a")) == ["b", "a"]


BOOL_CONFIG = """\
fields:
  - key: engine
    type: dropdown
    label: Engine
    options:
      - on
      - off
    default: off
"""


def test_yaml11_boolean_value_matches_a_boolean_group_key():
    """``.set_yaml`` is YAML 1.1: a bare ``off`` dropdown value loads as False,
    and so does a bare ``off:`` group key. The normalization is symmetric, so
    the pair still matches."""
    assert yaml.safe_load(BOOL_CONFIG)["fields"][0]["default"] is False
    groups = yaml.safe_load("off: [legacy-engine]")
    assert list(groups) == [False]  # the gotcha this test exists for
    manifest = {"optional_dependencies": {
        "selector": {"file": CONFIG_REL, "key": "engine"},
        "groups": groups,
    }}
    assert resolve_optional_requirements(manifest, {CONFIG_REL: BOOL_CONFIG}) == [
        "legacy-engine"
    ]


def test_quoting_must_match_between_config_and_group_key():
    """The flip side: a QUOTED group key is the string "off", which a bare
    (boolean) config value is not. Spell both sides the same way."""
    manifest = {"optional_dependencies": {
        "selector": {"file": CONFIG_REL, "key": "engine"},
        "groups": yaml.safe_load('{"off": [legacy-engine]}'),
    }}
    assert resolve_optional_requirements(manifest, {CONFIG_REL: BOOL_CONFIG}) == []


# ===========================================================================
# Requirement name (what ``-P`` takes)
# ===========================================================================

@pytest.mark.parametrize("req,name", [
    ("engine-a[bridge]", "engine-a"),
    ("fixture-lib[bridge]", "fixture-lib"),
    ("fixture-extra", "fixture-extra"),
    ("fixture-research>=0.10", "fixture-research"),
    ("bridge==1.94.0", "bridge"),
    ("pillow ; python_version < '3.14'", "pillow"),
    ("./local-wheel", ""),
])
def test_requirement_name_strips_extras_and_specifiers(req, name):
    assert requirement_name(req) == name


# ===========================================================================
# argv — one builder, both launch paths
# ===========================================================================

def test_uv_run_argv_without_extras_is_todays_launch():
    assert uv_run_argv("fixture.py") == ["uv", "run", "fixture.py"]
    assert uv_run_argv("fixture.py", upgrade=True) == ["uv", "run", "fixture.py"]


def test_uv_run_argv_one_with_per_requirement():
    assert uv_run_argv("fixture.py", requirements=["engine-b", "bridge"]) == [
        "uv", "run", "--with", "engine-b", "--with", "bridge", "fixture.py",
    ]


def test_uv_run_argv_upgrade_names_each_distribution_once():
    argv = uv_run_argv(
        "fixture.py",
        requirements=["engine-a[bridge]", "bridge==1.94.0", "bridge"],
        upgrade=True,
    )
    assert argv == [
        "uv", "run",
        "--with", "engine-a[bridge]", "--with", "bridge==1.94.0", "--with", "bridge",
        "--upgrade-package", "engine-a", "--upgrade-package", "bridge",
        "fixture.py",
    ]


def test_uv_run_argv_no_upgrade_flags_when_grant_is_off():
    argv = uv_run_argv("fixture.py", requirements=["engine-a[bridge]"])
    assert "--upgrade-package" not in argv
    assert "-P" not in argv
    assert "--refresh" not in argv  # measured: swallowed by uv's satisfied-check


class _FakeManager:
    """The two attributes the native spawn reads off a manager."""

    def __init__(self, requirements, update_packages):
        from runner_permissions import RunnerPermissions
        self.app_dir = "/home/u/.collimate/tmp/app_x"
        self.optional_requirements = list(requirements)
        self.permissions = RunnerPermissions.from_definition({
            "docker": "none", "internet": "full", "update_packages": update_packages,
        })


def _docker_spec(requirements, upgrade):
    from process_manager.docker_runner import RunnerSpec
    return RunnerSpec(
        image="collimate-runner:py3.13",
        container_name="collimate-run-RID",
        run_id="RID",
        app_dir="/home/u/.collimate/tmp/app_x",
        workspace_dir="/home/u/.collimate/tmp/ws_x",
        sdk_src="/home/u/sdk/src",
        entrypoint="fixture.py",
        env={},
        network="bridge",
        requirements=requirements,
        upgrade_packages=upgrade,
    )


async def _native_argv(monkeypatch, requirements, *, update_packages):
    from process_manager.backends import NativeSubprocessBackend

    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await NativeSubprocessBackend().spawn(
        _FakeManager(requirements, update_packages),
        env={}, entrypoint="fixture.py", manifest={}, preexec=None,
    )
    return captured


@pytest.mark.parametrize("update_packages", [False, True])
async def test_both_launch_paths_build_the_same_uv_tail(monkeypatch, update_packages):
    from process_manager.docker_runner import build_run_argv

    reqs = ["engine-b", "bridge"]
    native = await _native_argv(monkeypatch, reqs, update_packages=update_packages)
    docker = build_run_argv(_docker_spec(reqs, update_packages))

    expected = uv_run_argv("fixture.py", requirements=reqs, upgrade=update_packages)
    assert native["argv"] == expected
    assert native["cwd"] == "/home/u/.collimate/tmp/app_x"
    # The container tail is the image followed by the identical uv argv.
    assert docker[-len(expected) - 1:] == ["collimate-runner:py3.13", *expected]


async def test_native_spawn_without_extras_is_unchanged(monkeypatch):
    captured = await _native_argv(monkeypatch, [], update_packages=True)
    assert captured["argv"] == ["uv", "run", "fixture.py"]


def test_docker_tail_without_extras_is_unchanged():
    from process_manager.docker_runner import build_run_argv
    argv = build_run_argv(_docker_spec([], False))
    assert argv[-4:] == ["collimate-runner:py3.13", "uv", "run", "fixture.py"]


async def test_update_packages_off_keeps_upgrade_flags_off_on_both_paths(monkeypatch):
    from process_manager.docker_runner import build_run_argv

    reqs = ["engine-a[bridge]"]
    native = await _native_argv(monkeypatch, reqs, update_packages=False)
    docker = build_run_argv(_docker_spec(reqs, False))
    for argv in (native["argv"], docker):
        assert "--with" in argv
        assert "--upgrade-package" not in argv


# ===========================================================================
# Threading + surfacing
# ===========================================================================

def test_manager_carries_the_resolved_requirements(tmp_path, monkeypatch):
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED, RunnerPermissions

    from models import Channel
    from state import state
    channel = Channel(name="Optional dependencies")
    monkeypatch.setattr(state, "channels", [channel])
    pm = ProcessManager(
        channel=str(channel.id), run_id="r", app_dir=str(tmp_path),
        workspace_dir=str(tmp_path), entrypoint="fixture.py",
        optional_requirements=["fixture-extra"],
        permissions=UNRESTRICTED,
    )
    assert pm.optional_requirements == ["fixture-extra"]
    # Absent ⇒ empty, and the grant's default (update_packages True) alone
    # never adds a flag with nothing to upgrade.
    bare = ProcessManager(
        channel=str(channel.id), run_id="r2", app_dir=str(tmp_path),
        workspace_dir=str(tmp_path), entrypoint="fixture.py",
        permissions=RunnerPermissions.from_definition(None),
    )
    assert bare.optional_requirements == []
    assert uv_run_argv(
        "fixture.py", requirements=bare.optional_requirements,
        upgrade=bare.permissions.update_packages,
    ) == ["uv", "run", "fixture.py"]


def test_create_run_threads_the_tag_overlays_group(client, monkeypatch):
    """create_run's own seam: the manifest it already read ⊕ the merged seeded
    config decide the manager's requirement list, so the spawn path re-reads
    nothing. ``start`` is stubbed — this pins the threading, not the install."""
    from conftest import install_runner
    from process_manager import ProcessManager

    captured: dict = {}

    async def fake_start(self):
        captured["requirements"] = list(self.optional_requirements)

    monkeypatch.setattr(ProcessManager, "start", fake_start)

    relay, _gen = install_runner(
        generator="optdeps_probe",
        draft_files={CONFIG_REL: FIXTURE_CONFIG},
        gen_manifest_extra=yaml.safe_dump(
            {"optional_dependencies": MANIFEST["optional_dependencies"]}, sort_keys=False,
        ),
        definition={"settings": {CONFIG_REL: {"backend": "engine-a"}}},
    )
    cell_id = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    r = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={
            "runner": relay, "cell_id": cell_id,
        },
    )
    assert r.status_code == 200, r.text
    from run_spawn import wait_spawned
    wait_spawned(r.json()["run_id"])
    assert captured["requirements"] == ["engine-a[bridge]"]


def test_phase_message_names_the_install():
    assert install_phase_message([]) == ""
    assert install_phase_message(None, upgrade=True) == ""
    assert install_phase_message(["engine-b", "bridge"]) == "installing engine-b, bridge"
    assert install_phase_message(["fixture-extra"], upgrade=True) == "installing/updating fixture-extra"


# ===========================================================================
# Canonical SDK declarations, independent of shipped package names
# ===========================================================================


def _check_optional_groups(manifest, package_dir):
    block = manifest.get("optional_dependencies")
    if block is None:
        return
    assert isinstance(block, dict)
    selector = block["selector"]
    groups = block["groups"]
    assert isinstance(selector, dict) and isinstance(groups, dict)
    relative, key = selector["file"], selector["key"]
    assert isinstance(relative, str) and isinstance(key, str)
    source = (package_dir / relative).read_text(encoding="utf-8")
    from collimate.config_values import parse_set_yaml, resolve_form_values
    _fields, resolved = resolve_form_values(parse_set_yaml(source))
    assert key in resolved
    for value, requirements in groups.items():
        assert isinstance(requirements, list)
        assert all(isinstance(requirement, str) and requirement.strip() for requirement in requirements)
        selected = overlay_set_yaml_values(source, {key: value})
        expected = list(dict.fromkeys(requirement.strip() for requirement in requirements))
        assert resolve_optional_requirements(manifest, {relative: selected}) == expected


def test_canonical_sdk_optional_declarations_resolve_all_groups__F1915():
    from collimate.definitions import discover_definitions
    from collimate._host.packaging.layout import scan_cells
    from genesis import GENERATORS_DIR

    packages = []
    for cell in scan_cells(Path(GENERATORS_DIR)):
        paths = [path.relative_to(cell.path).as_posix()
                 for path in cell.path.rglob("*") if path.is_file()]
        discovery = discover_definitions(paths)
        assert not discovery.problems, discovery.problems
        packages.extend((cell.path, package) for package in discovery.packages
                        if package.kind == "generator")
    assert packages, "canonical SDK generator discovery must execute"
    for cell_dir, package in packages:
        declaration = cell_dir / package.definition_path
        manifest = yaml.safe_load(declaration.read_text(encoding="utf-8"))
        _check_optional_groups(manifest, declaration.parent)


def test_nonempty_optional_declaration_control_cannot_pass_vacuously__F1915(tmp_path, monkeypatch):
    (tmp_path / CONFIG_REL).write_text(FIXTURE_CONFIG)
    _check_optional_groups(MANIFEST, tmp_path)
    monkeypatch.setitem(_check_optional_groups.__globals__, "resolve_optional_requirements", lambda *args: [])
    with pytest.raises(AssertionError):
        _check_optional_groups(MANIFEST, tmp_path)


def test_app_optional_dependency_selection_uses_public_sdk_parser__F1225(monkeypatch):
    from collimate import config_values
    from process_manager.optional_deps import resolve_optional_requirements

    calls = []

    def parse(text):
        calls.append(text)
        return {'fields': [{'key': 'mode', 'default': 'light'}]}

    monkeypatch.setattr(config_values, 'parse_set_yaml', parse)
    manifest = {'optional_dependencies': {'selector': {'file': 'main.set_yaml', 'key': 'mode'},
                                         'groups': {'light': ['fixture-light']}}}
    assert resolve_optional_requirements(manifest, {'main.set_yaml': 'original'}) == ['fixture-light']
    assert calls == ['original']
