"""Per-server-instance identity — the stamp that keeps two concurrently
running Collimate servers from reaping each other's containers.

Two servers can now be live on one machine at once: the installed app
(user-data dir ``Collimate``) and an unpackaged dev run (``Collimate Dev``),
each with its own ephemeral port and its own data dir, but both talking to
the ONE dockerd on the host. Every *non-run-scoped* reaper in this codebase
sweeps by a predicate that BOTH servers match — the ``com.collimate.run_id``
label (main.py's startup reaper) or the ``collimate-sandbox-`` name prefix
(sandbox.reap_orphan_containers) — so without an owner stamp, booting one
server force-removes the other server's LIVE generator containers out from
under its running generators. Stamping every container we create with the id
of the server that created it, and teaching those reapers to skip containers
stamped by somebody else, is what lets the pair coexist.

Why the id is derived from the data dir and not from the pid, the port, or a
random per-boot token: a startup reaper's entire job is to clean up after a
PREVIOUS process of the SAME server (a crash, a force-stop, a container the
generator's own ``finally`` never reached), so ownership has to survive a
restart. The data dir is exactly the grain we want — it is precisely what
distinguishes the stable app from the dev run — and it needs no plumbing to
reach the container-creation sites, because the data dir IS the process CWD
by ``main.cli_main``'s contract (every persistent path in the app is
CWD-relative).

The protection is symmetric, and therefore only complete once BOTH servers
carry the label. A peer running a pre-label build — most realistically a
stable app installed from a PyPI release while this source tree runs the dev
profile — creates containers with no stamp at all, and a labelled server
still reaps those as legacy orphans (see ``owns_container``). That is by
design rather than an oversight: until this label existed, an unstamped
container was, by the time any server booted, always an orphan, and the only
way to spare a pre-label peer's live containers would be to stop reaping real
orphans entirely — which would leave every crash's leftovers to accumulate
forever. Upgrade the older side and the pair is safe.

The one escape hatch is ``COLLIMATE_REAP_ALL_INSTANCES=1``, which restores
the pre-label global sweep — see ``reap_all_instances``.
"""
from __future__ import annotations

import hashlib
import os

# The docker label every container this server creates carries. Read by the
# two instance-scoped reapers; a container WITHOUT it predates this code (see
# owns_container) and is not evidence of a second live server.
INSTANCE_LABEL = "com.collimate.instance"

# Opt back in to the pre-label behaviour: reap every matching container no
# matter who stamped it. See reap_all_instances().
REAP_ALL_ENV = "COLLIMATE_REAP_ALL_INSTANCES"

# Resolved on first call and cached for the life of the process — see
# instance_id() for why it must not be resolved any earlier than that.
# Tests that need a second identity reset this to None after chdir'ing.
_cached_id: str | None = None

# Same lazy-and-then-frozen treatment for the env opt-in, for the same reason
# the id gets it: every reaper in one process must answer this question the
# same way, and a test (or a caller that edits os.environ) must not be able to
# change the rule out from under a sweep that is already running. Tests reset
# this to None after setting the variable.
_cached_reap_all: bool | None = None


def instance_id() -> str:
    """This server's stable 12-hex-char id, derived from its data dir.

    Lazily resolved and then frozen, and that laziness is load-bearing: the
    data dir only becomes the CWD inside ``main.cli_main``, which runs well
    AFTER main.py's module level has imported half the app. Computing this at
    import time would therefore bake in the launcher's CWD (the same one for
    both servers), silently handing the stable app and the dev run identical
    ids. Every real caller runs long after the chdir — the reapers from the
    lifespan, the label stampers from a live run — so first-call resolution
    always sees the right directory.

    Freezing the first answer also matters for correctness, not just speed: a
    reaper must judge containers against the SAME id the stampers used, and a
    stray chdir later in the process must not be able to change that.
    """
    global _cached_id
    if _cached_id is None:
        # surrogateescape: a data dir with undecodable bytes in its name must
        # still hash rather than raise — this runs on the startup path.
        digest = hashlib.sha1(
            os.path.abspath(os.getcwd()).encode("utf-8", "surrogateescape")
        ).hexdigest()
        _cached_id = digest[:12]
    return _cached_id


def reap_all_instances() -> bool:
    """Has the operator asked for the pre-label GLOBAL sweep back?

    Ownership is keyed to the data dir, which quietly assumes the data dir
    outlives the process. Two setups where it does not, and where the default
    therefore strands containers instead of protecting them:

      * the live-test harness (api/tests_live/conftest.py) spawns its uvicorn
        in a fresh pytest tmp dir every session, so each run's containers are
        stamped with an id that NO later server will ever claim — a harness
        killed with SIGKILL (the teardown's last resort) leaves containers
        that nothing on the machine is willing to reap;
      * a single-user machine that only ever runs one Collimate at a time,
        where the cross-instance guard buys nothing and the old "sweep every
        orphan you find" behaviour is simply more useful.

    Both opt in with ``COLLIMATE_REAP_ALL_INSTANCES=1``. It is deliberately
    off by default: the failure it re-enables (killing a live peer's
    containers) is worse than the one it fixes (a stale container survives),
    so it has to be a choice made by whoever knows there is no peer.

    Truthiness is lenient — ``1``/``true``/``yes``/``on``, case and whitespace
    insensitive — because this is typed by hand into a shell or a launcher
    entry, and silently ignoring ``true`` would look like the flag is broken.
    """
    global _cached_reap_all
    if _cached_reap_all is None:
        _cached_reap_all = (
            os.environ.get(REAP_ALL_ENV, "").strip().lower()
            in ("1", "true", "yes", "on")
        )
    return _cached_reap_all


def owns_container(labels: dict | str | None, mine: str, reap_all: bool = False) -> bool:
    """May this server reap a container carrying ``labels``?

    ``labels`` is whatever the caller's docker listing hands back: a label
    mapping (docker-py's ``Container.labels``) or the single label value
    printed by ``docker ps --format '{{.Label "com.collimate.instance"}}'``,
    which is the empty string when the container has no such label.

    Two cases say yes:

      * the stamp equals ``mine`` — our own container, from a previous
        process of this same server, so reaping it is the whole point;
      * the stamp is absent — a legacy container created before this label
        existed. No live server running THIS code can produce one, so an
        unstamped container is necessarily left over from an older build and
        is safe to sweep; keeping it would mean those orphans never clear.

    Everything else — a stamp belonging to another instance, or a shape we
    don't recognise — says no. The asymmetry is deliberate: wrongly reaping
    kills a container a live generator is using, while wrongly keeping one
    costs a stale container until its owner next boots.

    ``reap_all`` (the callers pass ``reap_all_instances()``) short-circuits
    the whole rule to yes. It is not "also claim foreign stamps": it is the
    pre-label sweep verbatim, which never read a label at all, so it has to
    win over the unreadable-shape and missing-``mine`` guards too — those
    exist only to keep us from *mistaking* a foreign container for our own,
    and under this flag there is nothing to mistake.
    """
    if reap_all:
        return True
    if not mine:
        # No identity of our own means we cannot prove ownership of anything,
        # so claim nothing. (instance_id() never returns empty; this guards a
        # caller that passed a missing value.)
        return False
    if labels is None:
        return True
    if isinstance(labels, dict):
        stamped = labels.get(INSTANCE_LABEL)
    elif isinstance(labels, str):
        stamped = labels
    else:
        return False
    if stamped is None:
        return True
    if not isinstance(stamped, str):
        return False
    stamped = stamped.strip()
    if not stamped:
        # `docker ps --format '{{.Label "..."}}'` prints an empty field for a
        # container without the label, so empty means absent, not "owned by
        # the server whose id is the empty string".
        return True
    return stamped == mine
