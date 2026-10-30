"""Instance-scoped container reaping (api/instance.py + the two boot sweeps).

The scenario under test is two Collimate servers live at once on one dockerd:
the installed app and an unpackaged dev run, separate data dirs, separate
ports. Both sweep containers at boot by a predicate the OTHER one matches too
(the ``com.collimate.run_id`` label in main.py, the ``collimate-sandbox-``
name prefix in sandbox.py), so each sweep has to be filtered by an owner
stamp or it force-removes the other server's LIVE containers.

Three layers, none of which touches a docker daemon:

  * ``instance_id`` — derived from the data dir (the process CWD), stable
    within a process, distinct between two data dirs;
  * ``owns_container`` — the one shared ownership rule, truth table, plus
    the ``COLLIMATE_REAP_ALL_INSTANCES`` opt-in that waives it for a host
    that knows it has no second server (or whose data dir is thrown away,
    like the live-test harness, so nothing would ever claim its stamp);
  * the two reapers — startup_cleanup's pure selection helper against fake docker-py
    containers, and sandbox.reap_orphan_containers against a faked
    ``subprocess`` (the seam test_pytest_stop_all.py uses for the run-scoped
    reaper).
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import types
from datetime import datetime, timezone

import pytest

import instance


@pytest.fixture(autouse=True)
def fresh_instance_id(monkeypatch):
    """Drop the cached id and the cached env answer around every test.

    Both freeze their first answer for the life of the process, which is what
    a real server wants (reapers and stampers must agree) but would make these
    tests order-dependent: whichever ran first would pin the id to its own tmp
    cwd — and the opt-in to its own environment — for all the others. The
    opt-in variable itself is cleared too: a developer who exports it in
    their shell (the docstring recommends it for a one-Collimate machine)
    must not see the default-mode tests fail; the tests that want it on set
    it explicitly.
    """
    monkeypatch.delenv(instance.REAP_ALL_ENV, raising=False)
    instance._cached_id = None
    instance._cached_reap_all = None
    yield
    instance._cached_id = None
    instance._cached_reap_all = None


# ---------------------------------------------------------------------------
# instance_id
# ---------------------------------------------------------------------------

def test_instance_id_is_stable_within_a_process(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = instance.instance_id()
    assert first == instance.instance_id()
    assert len(first) == 12 and all(c in "0123456789abcdef" for c in first)


def test_instance_id_differs_between_data_dirs(tmp_path, monkeypatch):
    """The stable app and the dev run differ ONLY by data dir, so that is the
    input the id has to key off — otherwise both servers claim each other's
    containers."""
    stable = tmp_path / "Collimate"
    dev = tmp_path / "Collimate Dev"
    stable.mkdir()
    dev.mkdir()

    monkeypatch.chdir(stable)
    stable_id = instance.instance_id()

    instance._cached_id = None  # a second process, not a second chdir
    monkeypatch.chdir(dev)
    dev_id = instance.instance_id()

    assert stable_id != dev_id


def test_instance_id_survives_a_restart_in_the_same_data_dir(tmp_path, monkeypatch):
    """A reaper cleans up after a PREVIOUS process of the same server, so the
    id must be reproducible from the data dir alone — no pid, no boot token."""
    monkeypatch.chdir(tmp_path)
    before = instance.instance_id()
    instance._cached_id = None
    assert instance.instance_id() == before


def test_instance_id_is_not_resolved_at_import_time(tmp_path, monkeypatch):
    """main.py's module level loads before cli_main chdirs into the data dir;
    an import-time id would therefore be the launcher's cwd for both servers."""
    monkeypatch.chdir(tmp_path)
    reloaded = importlib.reload(instance)
    try:
        assert reloaded._cached_id is None
    finally:
        # Reload rebinds the module object every importer of `instance` still
        # holds by name, so put the original back for the rest of the session.
        sys.modules["instance"] = instance


# ---------------------------------------------------------------------------
# owns_container
# ---------------------------------------------------------------------------

MINE = "abc123abc123"
THEIRS = "def456def456"


def test_owns_container_claims_its_own_stamp():
    assert instance.owns_container({instance.INSTANCE_LABEL: MINE}, MINE) is True
    assert instance.owns_container(MINE, MINE) is True


def test_owns_container_claims_unstamped_legacy_containers():
    """No live server running this code creates an unlabelled container, so an
    unlabelled one is from an older build — safe to sweep, and refusing to
    would mean those orphans never clear."""
    assert instance.owns_container(None, MINE) is True
    assert instance.owns_container({}, MINE) is True
    assert instance.owns_container({"com.collimate.run_id": "r1"}, MINE) is True
    # `docker ps --format '{{.Label "..."}}'` prints an empty field, not a
    # missing one, for a container without the label.
    assert instance.owns_container("", MINE) is True


def test_owns_container_refuses_another_instances_container():
    assert instance.owns_container({instance.INSTANCE_LABEL: THEIRS}, MINE) is False
    assert instance.owns_container(THEIRS, MINE) is False


def test_owns_container_refuses_malformed_input():
    """Anything we can't read is treated as somebody else's: a wrong keep
    costs a stale container, a wrong reap kills a live generator."""
    assert instance.owns_container(["not", "a", "mapping"], MINE) is False
    assert instance.owns_container(42, MINE) is False
    assert instance.owns_container({instance.INSTANCE_LABEL: 7}, MINE) is False
    # No identity of our own → claim nothing.
    assert instance.owns_container({instance.INSTANCE_LABEL: MINE}, "") is False


# ---------------------------------------------------------------------------
# COLLIMATE_REAP_ALL_INSTANCES — the opt-in back to the global sweep
# ---------------------------------------------------------------------------

def test_reap_all_instances_is_off_unless_asked(monkeypatch):
    """Off by default because the failure it re-enables (killing a live peer's
    containers) is worse than the one it fixes (a stale container survives)."""
    monkeypatch.delenv(instance.REAP_ALL_ENV, raising=False)
    assert instance.reap_all_instances() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_reap_all_instances_accepts_hand_typed_truths(monkeypatch, value):
    """A human types this into a shell or a launcher entry, so `true` has to
    work as well as `1` — silently ignoring it would look like a broken flag."""
    monkeypatch.setenv(instance.REAP_ALL_ENV, value)
    instance._cached_reap_all = None
    assert instance.reap_all_instances() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_reap_all_instances_rejects_everything_else(monkeypatch, value):
    monkeypatch.setenv(instance.REAP_ALL_ENV, value)
    instance._cached_reap_all = None
    assert instance.reap_all_instances() is False


def test_reap_all_instances_is_frozen_after_the_first_read(monkeypatch):
    """Same reason instance_id() freezes: every reaper in one process must
    answer this the same way, and a sweep already in flight must not have the
    rule changed under it by an os.environ edit."""
    monkeypatch.delenv(instance.REAP_ALL_ENV, raising=False)
    assert instance.reap_all_instances() is False
    monkeypatch.setenv(instance.REAP_ALL_ENV, "1")
    assert instance.reap_all_instances() is False


def test_owns_container_with_reap_all_claims_everything():
    """The flag is the pre-label sweep verbatim — that sweep never read a
    label at all — so it has to beat the foreign stamp AND the guards that
    exist only to stop us mistaking a foreign container for our own."""
    assert instance.owns_container(
        {instance.INSTANCE_LABEL: THEIRS}, MINE, reap_all=True) is True
    assert instance.owns_container(THEIRS, MINE, reap_all=True) is True
    assert instance.owns_container(["not", "a", "mapping"], MINE, reap_all=True) is True
    # No identity of our own is no obstacle either: with ownership waived,
    # there is nothing left to prove.
    assert instance.owns_container({instance.INSTANCE_LABEL: THEIRS}, "", reap_all=True) is True


# ---------------------------------------------------------------------------
# main.py's startup reaper (pure selection half)
# ---------------------------------------------------------------------------

BOOT_TS = 1_700_000_000.0


def _created(ts: float) -> str:
    """Docker's RFC3339-with-nanoseconds ``Created`` string for ``ts``."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f000Z"
    )


def _container(cid: str, *, created: float, labels):
    """A stand-in for a docker-py Container as ``containers.list(all=True)``
    returns it: a full inspect payload under ``.attrs``."""
    return types.SimpleNamespace(
        id=cid,
        attrs={"Created": _created(created), "Config": {"Labels": labels}},
    )


@pytest.fixture
def reaper(tmp_path, monkeypatch):
    """startup_cleanup's pure selection helper, imported in a clean cwd."""
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("main", None)
    importlib.import_module("main")
    from startup_cleanup import orphan_generator_container_ids
    return orphan_generator_container_ids


def test_startup_reaper_spares_the_other_servers_live_containers(reaper):
    """The whole point: the dev run's generator containers are older than the
    installed app's boot and carry run-id labels of the same shape, so only
    the owner stamp keeps the app's boot sweep off them."""
    ours = _container("own1", created=BOOT_TS - 60,
                      labels={"com.collimate.run_id": "r1",
                              instance.INSTANCE_LABEL: MINE})
    theirs = _container("their1", created=BOOT_TS - 60,
                        labels={"com.collimate.run_id": "r2",
                                instance.INSTANCE_LABEL: THEIRS})
    assert reaper([ours, theirs], BOOT_TS, MINE) == ["own1"]


def test_startup_reaper_takes_own_and_legacy_orphans(reaper):
    ours = _container("own1", created=BOOT_TS - 60,
                      labels={"com.collimate.run_id": "r1",
                              instance.INSTANCE_LABEL: MINE})
    legacy = _container("legacy1", created=BOOT_TS - 60,
                        labels={"com.collimate.run_id": "r0"})
    assert reaper([ours, legacy], BOOT_TS, MINE) == ["own1", "legacy1"]


def test_startup_reaper_keeps_the_created_before_boot_cutoff__F0056(reaper):
    """The delayed sweep can land after this server's own first run placed a
    container; ownership must not override the boot-timestamp guard."""
    fresh = _container("own_live", created=BOOT_TS + 5,
                       labels={"com.collimate.run_id": "r1",
                               instance.INSTANCE_LABEL: MINE})
    assert reaper([fresh], BOOT_TS, MINE) == []


def test_startup_reaper_skips_unreadable_payloads(reaper):
    """No Config → we can't read the stamp, so we can't prove it's ours."""
    sparse = types.SimpleNamespace(
        id="sparse1", attrs={"Created": _created(BOOT_TS - 60), "Labels": {}},
    )
    unparseable = _container("bad_ts", created=BOOT_TS - 60,
                             labels={instance.INSTANCE_LABEL: MINE})
    unparseable.attrs["Created"] = "not-a-timestamp"
    assert reaper([sparse, unparseable], BOOT_TS, MINE) == []


def test_startup_reaper_with_reap_all_takes_the_other_servers_orphans(reaper):
    """COLLIMATE_REAP_ALL_INSTANCES restores the pre-stamp sweep: on a host
    that knows it has no peer — or whose own stamps are unclaimable, like the
    live-test harness's per-session tmp data dir — a foreign orphan is just an
    orphan, and refusing it would strand it forever."""
    theirs = _container("their1", created=BOOT_TS - 60,
                        labels={"com.collimate.run_id": "r2",
                                instance.INSTANCE_LABEL: THEIRS})
    sparse = types.SimpleNamespace(
        id="sparse1", attrs={"Created": _created(BOOT_TS - 60), "Labels": {}},
    )
    assert reaper([theirs, sparse], BOOT_TS, MINE, reap_all=True) == [
        "their1", "sparse1"]


def test_startup_reaper_with_reap_all_still_keeps_the_boot_cutoff(reaper):
    """Only the ownership guard is waived. The created-before-boot cutoff
    defends against a race entirely inside THIS server (the delayed sweep
    landing after our own first run placed a container), so the opt-in has no
    bearing on it."""
    fresh = _container("live1", created=BOOT_TS + 5,
                       labels={"com.collimate.run_id": "r2",
                               instance.INSTANCE_LABEL: THEIRS})
    assert reaper([fresh], BOOT_TS, MINE, reap_all=True) == []


# ---------------------------------------------------------------------------
# sandbox.reap_orphan_containers
# ---------------------------------------------------------------------------

def _fake_sandbox_docker(monkeypatch, ps_stdout: str) -> list[list[str]]:
    """Point sandbox's `subprocess` at a recorder returning ``ps_stdout``.

    TimeoutExpired comes along because the reaper's own ``except`` clause
    resolves it through this same module attribute.
    """
    import sandbox

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "ps" in cmd:
            return types.SimpleNamespace(stdout=ps_stdout, returncode=0)
        return types.SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(sandbox, "subprocess", types.SimpleNamespace(
        run=fake_run, TimeoutExpired=subprocess.TimeoutExpired,
    ))
    monkeypatch.setattr(sandbox, "instance_id", lambda: MINE)
    return calls


def test_sandbox_reaper_removes_only_its_own_and_legacy_daemons(monkeypatch):
    """Both servers' sandbox daemons share the ``collimate-sandbox-`` name
    prefix, so the listing has to carry the owner label and the sweep has to
    honour it — otherwise a boot kills the other server's live chat daemons."""
    import sandbox

    calls = _fake_sandbox_docker(monkeypatch, "\n".join([
        f"own1 {MINE}",
        "legacy1",                 # pre-label container: no second field
        f"their1 {THEIRS}",
        f"own2 {MINE}",
    ]) + "\n")

    sandbox.reap_orphan_containers()

    ps_cmd = calls[0]
    assert "ps" in ps_cmd
    assert "name=^collimate-sandbox-" in ps_cmd
    # One `docker ps` carries both the id and the stamp — no per-container
    # inspect on the startup path.
    assert any(instance.INSTANCE_LABEL in part and "{{.ID}}" in part
               for part in ps_cmd)

    rm_cmd = calls[1]
    assert rm_cmd[:3] == ["docker", "rm", "-f"]
    assert set(rm_cmd[3:]) == {"own1", "legacy1", "own2"}
    assert "their1" not in rm_cmd


def test_sandbox_reaper_removes_nothing_when_every_daemon_is_foreign(monkeypatch):
    """With only the other server's daemons around, the reaper must not even
    issue a `docker rm` — a bare `rm -f` with no ids would error, and any id
    here would be a live container."""
    import sandbox

    calls = _fake_sandbox_docker(monkeypatch, f"their1 {THEIRS}\ntheir2 {THEIRS}\n")

    sandbox.reap_orphan_containers()

    assert len(calls) == 1 and "ps" in calls[0]


def test_sandbox_reaper_with_reap_all_removes_every_daemon(monkeypatch):
    """End to end through the env var, since that is the only way an operator
    (or the live-test conftest) reaches this: every collimate-sandbox-*
    container goes, foreign stamp included."""
    import sandbox

    monkeypatch.setenv(instance.REAP_ALL_ENV, "1")
    instance._cached_reap_all = None
    calls = _fake_sandbox_docker(monkeypatch, "\n".join([
        f"own1 {MINE}",
        "legacy1",
        f"their1 {THEIRS}",
    ]) + "\n")

    sandbox.reap_orphan_containers()

    rm_cmd = calls[1]
    assert rm_cmd[:3] == ["docker", "rm", "-f"]
    assert set(rm_cmd[3:]) == {"own1", "legacy1", "their1"}


def test_sandbox_reaper_swallows_a_vanished_data_dir(monkeypatch):
    """instance_id() reads os.getcwd(), which raises once the directory the
    process started in is deleted (a harness tmp dir, an unmounted volume).
    This reaper runs on the startup path and promises to swallow failures, so
    that read has to sit inside the try — not in front of it."""
    import sandbox

    calls = _fake_sandbox_docker(monkeypatch, "")

    def boom():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(sandbox, "instance_id", boom)

    sandbox.reap_orphan_containers()  # must not raise

    assert calls == []  # and nothing was asked of docker
