# SPDX-License-Identifier: MIT
"""Shared runtime for the containerized harnesses (codex / goose / opencode /
antigravity / qwen-code / cline / pi / claude-code).

These harnesses run inside their own Docker container in EVERY mode — the
container is both the installation story (each image ships its CLI + its ACP
adapter + the mcp-remote stdio proxy) and the exec boundary (their shells
never touch the host). This module owns the machinery they share:

  * a **per-run container cache** — one :class:`_env_shared.DockerEnvironment`
    per backend, provisioned off-loop (``asyncio.to_thread`` — a first-run
    image build takes minutes and must never starve ``run_agent``'s stop
    watchdog), reused across chat turns in the one generator process, torn
    down by ``session.run``'s run-level ``finally`` plus a defensive ``atexit``
    (a oneshot run is its own process, so nothing leaks).
  * :func:`gateway_bridge` — the per-run MCP bridge the ACP adapters reach
    (via ``session/new.mcpServers``). Docker-gateway bind + ``url_container``
    for a container consumer, loopback bind + ``url_host`` for a host-locus
    one (``bind_host="127.0.0.1"``); stateless streamable-http. Cached per
    (flags, extra-entry names, bind) so chat turns share one server, and
    stopped with the run by :func:`close_all`.
  * :func:`leg_bridge` — a terminal leg's own bridge (every TTY runner's,
    reached through its config seam), stopped when the leg ends.
  * workspace attachment staging and container file seeding helpers (the
    terminal surfaces' seed/finalize; the engine-driven turns ride the
    attached-stdio ACP transport in ``acp_client`` instead).

Docker being unavailable is a HARD, clearly-worded failure for these backends
("backend X requires Docker") — the container *is* the harness; there is no
text-only fallback.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
import atexit
import contextlib
import os
import shlex
from pathlib import Path, PurePosixPath

import gen
from .harnesses import HARNESS

_HERE = os.path.dirname(os.path.abspath(__file__))

# WHERE each explicitly-named artifact lives upstream, and which Dockerfile ARG
# carries its version — keyed by the harness whose image installs it.
#
# This half is the GENERATOR's to know: which registry ships codex is a fact
# about codex, not a choice. The other half — WHICH version — is the RUNNER's
# (``gen.packages()``), so moving a harness forward never means editing
# generator code. Resolution of "latest" reaches a registry, so it is gated on
# the run's ``update_packages`` grant.
_IMAGE_ARTIFACTS = {name: dict(row.artifacts) for name, row in HARNESS.items() if row.image}


# Plumbing an image carries for Collimate itself rather than a harness: the
# in-container MCP proxy. It stays on its Dockerfile pin unless a runner names
# it — floating it would move every harness's tool path at once, untested.
_PINNED_PLUMBING = frozenset({"mcp-remote", "pi-mcp-sdk"})


def _requested_versions(build_dir: str) -> dict:
    """``{BUILD_ARG: version}`` for the artifacts this image installs. A Docker
    harness keeps itself current: its CLI and ACP adapter (every artifact but
    the ``_PINNED_PLUMBING``) are ``latest`` unless the runner pins them
    (``packages: {codex: "0.150.0"}``),
    resolved at most once an hour per source and folded into the image tag,
    so a new upstream release is built on the next run. When the run may not
    fetch (``update_packages`` off) or the registry is unreachable, the last
    version this machine resolved stands in; only a never-resolved source
    falls to the canonical adapter fallback (other artifacts retain Dockerfile ARG defaults)."""
    artifacts = _IMAGE_ARTIFACTS.get(build_dir) or {}
    if not artifacts:
        return {}
    named = gen.packages()
    from _env_shared import git_account
    from _env_shared.upstream import LATEST, resolve_requests

    # The runner's own entries win (a pin, or an unknown name worth logging);
    # everything else this image installs floats. A host-side ACP adapter the
    # runner names (claude-agent-acp for claude's chat leg) is resolved by
    # host_adapters, not built here — not an unknown name for this image.
    from .adapter_artifacts import ADAPTERS

    requests = {name: LATEST for name in artifacts if name not in _PINNED_PLUMBING}
    requests.update({k: v for k, v in named.items()
                     if k in artifacts or not (k in ADAPTERS and ADAPTERS[k].host_install)})

    resolved = resolve_requests(
        artifacts, requests, allow_network=gen.grant().update_packages, log=safe_log,
        github_token=git_account.release_token(safe_log) if gen.grant().update_packages else None,
    )
    # Explicit pins and resolved latest/cache values win. Only an unresolved
    # adapter consumes the same fallback as its host preparation.
    for name, metadata in artifacts.items():
        if name in ADAPTERS:
            resolved.setdefault(metadata['arg'], ADAPTERS[name].fallback_version)
    return resolved


def safe_log(msg: str) -> None:
    try:
        _secret_log(msg)
    except Exception:  # noqa: BLE001 — bridge-free context
        pass


# backend name → provisioned DockerEnvironment (one per generator process).
_ENVS: dict = {}


def cached_env(name: str):
    """The container environment this run holds for backend ``name``, or
    ``None`` when it has not provisioned one (see :func:`ensure_env`)."""
    return _ENVS.get(name)
# (flags, extra names, bind host) → McpBridge | None — the streamable-http
# bridge cache (gateway-bound by default so in-container harnesses can reach
# it; loopback-bound for a host-locus consumer). close_all stops them.
_BRIDGES: dict = {}
# The endpoints of every terminal leg's own bridge (leg_bridge) this run
# started. Each leg stops its bridge; the endpoints are remembered until
# close_all, for run_bridge_urls.
_LEG_BRIDGE_URLS: set = set()
_ATEXIT_REGISTERED = False


def _require_docker(name: str, build) -> "object":
    """Run ``build()`` (a DockerEnvironment factory) and convert any failure
    into the one uniform, user-facing error these backends promise."""
    try:
        return build()
    except Exception as e:  # noqa: BLE001 — one uniform "needs Docker" surface
        raise RuntimeError(
            f"backend {name!r} requires Docker, which is unavailable: {e}"
        ) from e


def image_spec(name: str, build_dir: str) -> dict:
    """The image kwargs — tag, build contexts, ``BASE`` build arg — for the
    image of harness ``build_dir``, ready to splat into
    :class:`_env_shared.DockerEnvironment`.

    Both tags are CONTENT-ADDRESSED, and the layered one folds in the base's
    digest, so a base rebuild re-tags everything above it. That is the only
    thing that makes a Dockerfile edit — a harness version bump, an added tool
    — reach a machine that already has the image: the host builds when it lacks
    the tag, so identity must move with the content. Under a fixed tag
    (``collimate-goose:latest``) the image stays frozen at whatever that
    machine happened to build first. The resolved base tag rides along as the
    ``BASE`` build arg so the layered ``FROM ${BASE}`` picks up THIS base
    instead of the Dockerfile's own default."""
    from _env_shared import layered_image_spec

    return layered_image_spec(name, os.path.join(_HERE, _image_for(build_dir).folder),
                              build_args=_requested_versions(build_dir))


def container_env(
    name: str,
    *,
    build_dir: str,
    env_vars: "dict | None" = None,
    volumes: "dict | None" = None,
    readonly_files: "dict | None" = None,
    mount: str = "/workspace",
) -> "object":
    """The cached :class:`DockerEnvironment` for backend ``name``, provisioning
    it on first call (SYNC + blocking — callers on the event loop go through
    :func:`ensure_env`). ``build_dir`` names the harness whose image to build
    (``codex``, ``qwen-code``, ``pi``, ...: its adapter's ``image`` context,
    ``harnesses/<package>/image``; the ``_IMAGE_ARTIFACTS`` keys). The workspace is
    bind-mounted rw at ``mount`` (default ``/workspace``) and the image is layered on the shared
    ``collimate-linux-small`` base (:func:`image_spec`), exactly like the
    terminal modes.

    Every container always gets the ``host.docker.internal → host-gateway``
    mapping: the MCP bridge hands containers ``host.docker.internal`` URLs,
    and plain dockerd on Linux does not inject that alias (Docker Desktop
    does) — without it the harness would treat the unreachable bridge as
    "no tools" and silently drop the run's granted surface."""
    env = _ENVS.get(name)
    if env is not None:
        if _env_alive(env) and env.mount == mount:
            return env
        # A dockerd restart, a `docker stop`/prune or a reaper took the
        # container out from under the run. Handing back the cached env would
        # make every later exec a raw 409 for the rest of the run; drop it and
        # place a new one (the workspace and harness home are host binds, so
        # the session reloads from them).
        _ENVS.pop(name, None)
        try:
            env.close()
        except Exception:  # noqa: BLE001 — the host reaps what this leaves
            pass
        safe_log(f"the {name} container stopped — starting a new one")

    workspace_links, account_volumes = _split_bindings(volumes or {}, mount=mount)

    def _build():
        from _env_shared import DockerEnvironment
        return DockerEnvironment(
            **image_spec(_image_for(build_dir).name_prefix.removeprefix("collimate-"), build_dir),
            mount=mount, mount_mode="rw",
            environment=dict(env_vars or {}),
            volumes=account_volumes, readonly_files=dict(readonly_files or {}),
            extra_hosts={"host.docker.internal": "host-gateway"},
            working_dir=mount,
            name_prefix=_image_for(build_dir).name_prefix, kind=_image_for(build_dir).kind,
        )

    env = _require_docker(name, _build)
    try:
        stage_workspace_links(env, workspace_links, mount=mount)
        require_writable_bindings(name, env, volumes or {}, mount=mount)
    except BaseException:
        try:
            env.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    register_env(name, env)
    return env


def _split_bindings(volumes: dict, *, mount: str = "/workspace") -> tuple[dict, dict]:
    """Link workspace homes; keep host-authorized account stores as mounts.

    A login mounted below a home targets the home's workspace path before
    Docker starts. The later home symlink then cannot remove an active mount.
    The host still validates every external source against this run's leases.
    """
    if not volumes:
        return {}, {}
    root = Path(gen.workspace_path()).resolve(strict=True)
    links, mounts = {}, {}
    for source, spec in volumes.items():
        path = Path(source).resolve(strict=True)
        if path.is_relative_to(root):
            links[str(path)] = dict(spec)
        else:
            mounts[str(path)] = dict(spec)
    homes = sorted(links.items(), key=lambda item: len(item[1]["bind"]), reverse=True)
    for spec in mounts.values():
        target = PurePosixPath(spec["bind"])
        for source, home in homes:
            destination = PurePosixPath(home["bind"])
            if target.is_relative_to(destination):
                relative = Path(source).relative_to(root).as_posix()
                spec["bind"] = str(PurePosixPath(mount) / relative /
                                   target.relative_to(destination))
                break
    return links, mounts


def stage_workspace_links(env, links: dict, *, mount: str = "/workspace") -> None:
    """Expose staged homes through the container's single workspace mount.

    The source must resolve within this run's workspace. Destinations are
    paths in the disposable container; no extra host directory is mounted.
    Policy files use host-materialized read-only mounts.
    """
    if not links:
        return
    root = Path(gen.workspace_path()).resolve(strict=True)
    prepared = []
    for source, spec in links.items():
        path = Path(source).resolve(strict=True)
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ValueError(f"harness workspace link is outside this run's workspace: {source}") from None
        target = str(spec.get("bind") or "")
        if (not target.startswith("/") or target == "/" or ".." in PurePosixPath(target).parts
                or "\x00" in target or spec.get("mode", "rw") != "rw"):
            raise ValueError(f"invalid harness workspace link: {target!r}")
        if target == mount or target.startswith(mount.rstrip("/") + "/"):
            raise ValueError("harness link destination must be outside the workspace mount")
        prepared.append((str(PurePosixPath(mount) / relative.as_posix()), target))
    for source, target in sorted(prepared, key=lambda pair: len(PurePosixPath(pair[1]).parts)):
        code, output = env.exec(
            ["sh", "-ec", 'mkdir -p -- "$(dirname -- "$2")"; rm -rf -- "$2"; ln -s -- "$1" "$2"',
             "sh", source, target], user="root",
        )
        if code != 0:
            raise RuntimeError(f"cannot prepare harness workspace link {target!r}: {output!r}")


def require_writable_bindings(name: str, env, links: dict, *, mount: str = "/workspace") -> None:
    """Fail NOW, by name, when the harness cannot write its own binds.

    Every harness image runs as ``coder`` (uid 1000) against host directories
    created 0700 by the host user (the run workspace, the per-cell harness
    home). A bind carries host ownership through unchanged, so for a host
    user other than uid 1000 every write is denied and the symptom is an
    empty session store or a failed edit, never an error. Asked from INSIDE
    the container (``test -w`` as the image's user) rather than by comparing
    uids, because Docker Desktop's file sharing maps ownership and makes a
    non-1000 host work. A probe that cannot run at all says nothing."""
    targets = [mount] + [
        str(spec.get("bind")) for src, spec in links.items()
        if isinstance(spec, dict) and spec.get("bind")
        and spec.get("mode", "rw") == "rw" and os.path.isdir(str(src))
    ]
    try:
        code, out = env.exec(
            ["sh", "-c", 'for d in "$@"; do [ -w "$d" ] || echo "$d"; done',
             "sh", *targets])
        if code != 0:
            return
    except Exception:  # noqa: BLE001 — no verdict, no refusal
        return
    denied = [line for line in out.splitlines() if line.strip()]
    if denied:
        raise RuntimeError(
            f"backend {name!r}: the container's `coder` (uid 1000) cannot "
            f"write {', '.join(denied)} — the host directories behind them "
            f"are owned by uid {os.geteuid()}, so every session and file "
            "write would be denied silently. Run Collimate as a uid-1000 "
            "user, or rebuild the harness image with a `coder` whose uid "
            "matches this host's.")


def _env_alive(env) -> bool:
    """Whether a cached env's container is still running. Only a definite
    answer counts as dead — the container is gone (NotFound) or not
    ``running``; a lookup that fails for any other reason (no daemon access
    from this process) keeps the env."""
    try:
        container = env.container
    except Exception as e:  # noqa: BLE001 — docker.errors.NotFound, by name
        return type(e).__name__ != "NotFound"
    if container is None:
        return True
    return str(getattr(container, "status", "running") or "running") == "running"


def register_env(name: str, env) -> None:
    """Track ``env`` for :func:`close_all` (session.run's run-level ``finally`` +
    atexit). A leg may retire its own environment through close_env;
    failed closes remain registered for this run-level backstop."""
    _ENVS[name] = env
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(close_all)
        _ATEXIT_REGISTERED = True


def _image_for(name):
    from .harnesses import HARNESS, image
    return image(HARNESS[name])


async def ensure_env(name: str, **kwargs) -> "object":
    """:func:`container_env` off-loop: provisioning (image pull/build +
    ``containers.run``) blocks for seconds — minutes on a first build — and
    must never freeze the event loop the stop watchdog polls on."""
    return await asyncio.to_thread(container_env, name, **kwargs)


def close_env(name: str, env) -> None:
    """Close one leg's own environment, evicting only that exact instance.

    Keep a failing close registered for the run-level backstop. A replacement
    under the same name must never be removed by an older leg's teardown.
    """
    env.close()
    if _ENVS.get(name) is env:
        _ENVS.pop(name)


def close_all() -> None:
    """Tear down every cached container, ACP session, and MCP bridge + forget
    sessions. Idempotent, never raises — called from session.run's run-level
    ``finally`` and from atexit."""
    # ACP adapter children die FIRST — an adapter outliving its container
    # would spin on a dead exec socket during the container remove.
    try:
        from .acp_run import close_acp_sessions_sync
        close_acp_sessions_sync()
    except Exception:  # noqa: BLE001 — teardown is best-effort by contract
        pass
    envs = list(_ENVS.items())
    _ENVS.clear()
    for _name, env in envs:
        try:
            env.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort by contract
            pass
    bridges = list(_BRIDGES.values())
    _BRIDGES.clear()
    _LEG_BRIDGE_URLS.clear()
    for bridge in bridges:
        try:
            if bridge is not None:
                bridge.stop()
        except Exception:  # noqa: BLE001
            pass


def gateway_bridge(
    *, extra_entries: "list | None" = None,
    bind_host: "str | None" = None,
    cache_scope: tuple = (),
):
    """The per-run MCP bridge an ACP adapter connects back to (via
    ``session/new.mcpServers``), or ``None`` when the bridge is unavailable.
    Handler tools are always served. Stateless streamable-http; container consumers
    use ``bridge.url_container``, host-locus consumers ``bridge.url_host``
    (pass ``bind_host="127.0.0.1"`` for a run that needs no docker). Cached
    per (extra names, bind, event loop, caller scope) so a chat reuses one server;
    ``close_all`` stops every cached bridge with the containers. Never raises
    — a missing bridge just means the run goes on without those tools. A
    terminal leg takes its own bridge from :func:`leg_bridge` instead."""
    from .mcp_bridge import start_bridge
    from .workspace_tools import with_handler_tools

    extra = with_handler_tools(extra_entries or [])
    # The loop is part of the key: the bridge bounces its main_loop tools onto
    # the loop it was started on, so a second loop in one process (a
    # cross-loop reprovision) must not be handed the first loop's bridge.
    loop = asyncio.get_running_loop()
    key = (
        tuple(row[0] for row in extra),
        bind_host or "",
        loop,
        cache_scope,
    )
    if key in _BRIDGES:
        return _BRIDGES[key]
    bridge = None
    try:
        bridge = start_bridge(
            loop=loop,
            bind_host=bind_host,
            extra_entries=extra,
        )
    except Exception as e:  # noqa: BLE001 — never break a plain text turn
        safe_log(f"MCP bridge unavailable ({e}) — workspace tools off for this run")
    # Held for reuse; close_all stops it. A None (failed) bridge is cached on
    # purpose: its causes (mcp/uvicorn missing, no docker gateway to bind) are
    # deterministic, so retrying would only fail and re-log
    # every turn. close_all and run_bridge_urls skip it.
    _BRIDGES[key] = bridge
    return bridge


@contextlib.asynccontextmanager
async def leg_bridge(*, extra_entries: "list | None" = None, bind_host: "str | None" = None):
    """A terminal leg's own MCP bridge — the one lifetime rule every TTY
    runner follows. Yields the bridge (gateway-bound: a container leg's
    harness reaches it as ``bridge.url_container``; with ``bind_host``
    ``127.0.0.1``, loopback-bound for a leg on this machine, which reaches it
    as ``bridge.url_host``), or ``None`` when it could not start; starting
    never raises. Handler tools are always served. The bridge is stopped when
    the ``async with`` exits, whatever ends the leg (the TTY closing, Stop,
    a failed seed), so the grant's tools stop being served
    on a live capability URL with the leg rather than at run end. The stop is
    best-effort and runs off the loop (it joins the server thread). The
    engine-driven ACP turns share the cached :func:`gateway_bridge`
    instead."""
    from .mcp_bridge import start_bridge
    from .workspace_tools import with_handler_tools

    extra = with_handler_tools(extra_entries or [])
    bridge = None
    try:
        bridge = start_bridge(loop=asyncio.get_running_loop(), bind_host=bind_host,
                              extra_entries=extra)
    except Exception as e:  # noqa: BLE001 — never break the terminal
        safe_log(f"MCP bridge unavailable ({e}) — tools off for this terminal")
    if bridge is not None:
        _LEG_BRIDGE_URLS.update((bridge.url_container, bridge.url_host))
    try:
        yield bridge
    finally:
        if bridge is not None:
            try:
                await asyncio.to_thread(bridge.stop)
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                safe_log(f"MCP bridge stop failed: {e}")


def run_bridge_urls() -> frozenset:
    """Every endpoint of this run's own MCP bridges (container and host URLs
    alike): the cached ones and each terminal leg's, stopped or not. A
    harness session that saved one of them under a name of its own holds
    only Collimate's tools — a stopped leg's endpoint serves nothing at
    all."""
    urls = {url for bridge in _BRIDGES.values() if bridge is not None
            for url in (bridge.url_container, bridge.url_host)}
    return frozenset(urls | _LEG_BRIDGE_URLS)


# Where a chat attachment lives in the cell — the SAME folder the
# chat renderer uploads into, so every attachment the model is told about is
# a real cell file at the path it is told (never a staged copy elsewhere).
ATTACH_DIR = "attachments"


def stage_attachments(attachments) -> list:
    """Materialize bytes-only attachments into the cell under
    ``attachments/`` and return ``[(cell-relative path, media type)]`` —
    the ACP engine turns read workspace files, so a path in the prompt is their
    multimodal/document intake (the Gemini-CLI ``@path`` pattern,
    generalized).
    An entry that already carries a cell ``path`` is referenced as-is when
    the file exists (a chat attachment the renderer uploaded). Names are
    kept (collision-suffixed against existing files, never renumbered).
    Best-effort: an unwritable file is logged and skipped.

    The media type travels WITH its path because staging can skip: a caller that
    re-derived the types by zipping the full attachment list against this
    (shorter) one would shift every pairing after the first skip — an image
    losing its ``-i`` flag, a document gaining one."""
    from .agent_events import iter_attachments

    staged: list = []
    taken: set = set()
    for i, att in enumerate(attachments or []):
        if isinstance(att, dict):
            path = str(att.get("path") or "")
            if path and gen.has_file(path):
                # The cell holds it as the renderer's upload DRAFT — but the
                # harness reads the WORKSPACE DIRECTORY, and the draft's
                # workspace copy is transient: the host clears the draft on
                # the turn's first commit and the live mirror then deletes
                # the pulled copy. Write the bytes we already hold to the
                # same path (idempotent) so the file is a tracked cell write
                # that the commit carries and the directory keeps.
                for data, _mt, _name in iter_attachments([att]):
                    try:
                        gen.update_file(path, data)
                    except Exception as e:  # noqa: BLE001
                        safe_log(f"could not materialize attachment {path} ({e})")
                staged.append((path, str(att.get("media_type") or "")))
                taken.add(path)
                continue
        for data, mt, name in iter_attachments([att]):
            safe = os.path.basename(name).strip() or f"attachment-{i + 1}"
            path = _free_attachment_path(safe, taken)
            try:
                gen.update_file(path, data)
                staged.append((path, mt))
                taken.add(path)
            except Exception as e:  # noqa: BLE001
                safe_log(f"could not stage attachment {safe} ({e})")
    return staged


def _free_attachment_path(name: str, taken: set) -> str:
    """``attachments/<name>``, suffixed ``-2``/``-3``… while the path exists in
    the cell or was claimed earlier in this batch."""
    stem, ext = os.path.splitext(name)
    stem = stem or name
    candidate = f"{ATTACH_DIR}/{name}"
    n = 2
    while candidate in taken or gen.has_file(candidate):
        candidate = f"{ATTACH_DIR}/{stem}-{n}{ext}"
        n += 1
    return candidate


# The harness context file a terminal leg plants in the workspace (AGENTS.md,
# GEMINI.md, QWEN.md, .goosehints — each CLI's auto-read instructions file):
# our block is fenced so a user's OWN file of that name is extended, never
# replaced, and the leg's teardown strips exactly our block again — the cell
# must not keep harness scaffolding the user never wrote.
#
# This is now the FALLBACK path only (shared/docs/agent-runtime.md, instruction context): a harness whose own
# instruction seam Collimate owns gets the session context there instead
# (``<backend>.write_context_seam`` — opencode's ``instructions`` config file,
# goose's global hints, ``$CODEX_HOME/AGENTS.md``, the per-cell
# ``~/.gemini/config/rules/collimate-context.md`` / ``~/.qwen/QWEN.md``).
# The workspace file survives only where no such seam exists: cline, pi —
# and as the terminal legs' fallback when a harness's own rules
# path turns out not to be read (antigravity's is unverified live).
CONTEXT_BEGIN = "<!-- collimate:context-begin -->"
CONTEXT_END = "<!-- collimate:context-end -->"


def write_context_seam(env, path: str, text: str, *, user: str = "coder") -> bool:
    """Plant the session context in a harness-owned instruction file at
    ``path`` (shared/docs/agent-runtime.md, instruction context) and report whether the nonempty write landed (exit 0).

    Unlike :func:`inject_context_file` this writes the file WHOLE and
    unfenced: every caller's ``path`` is a Collimate-owned location — the
    container's own config dir (opencode, goose) or the per-cell harness home
    (codex/antigravity/qwen) — never the workspace and never a
    subscription-mounted real home, so there is no user content to preserve.
    It is rewritten on every session open (a reopen's ``session/load``
    included) and REMOVED when the run carries no system prompt: the per-cell
    home (and the cached container's config dir) outlives the leg — every leg
    of a run shares it — and a stale seam from an earlier leg would keep
    instructing this one."""
    body = (text or "").strip()
    if not body:
        try:
            env.exec(["sh", "-c", f"rm -f {shlex.quote(path)}"], user=user)
        except Exception:  # noqa: BLE001 — nothing to clear is the same outcome
            pass
        return False
    ok = write_container_file(env, path, body + "\n",
                              mkdir=os.path.dirname(path) or None, user=user)
    if not ok:
        # A stale seam from an earlier run must not keep instructing this one
        # while the caller falls back to its fold / workspace context file.
        try:
            env.exec(["sh", "-c", f"rm -f {shlex.quote(path)}"], user=user)
        except Exception:  # noqa: BLE001 — best-effort, like the blank branch
            pass
        safe_log(f"could not write the context seam {path}; falling back")
    return ok


def inject_context_file(env, path: str, text: str, *, user: str = "coder") -> None:
    """Add (or refresh) Collimate's fenced block in the workspace context file
    at ``path``: an existing user file keeps its own content ahead of the
    block; a previous block of ours is replaced, not stacked. An existing
    file that cannot be read whole is left as it is, with no block, and
    logged: it is written back only from its whole content."""
    try:
        existing = env.read_file(path).decode("utf-8", "replace")
    except FileNotFoundError:
        existing = ""
    except (OSError, RuntimeError) as exc:  # never overwrite what was not read
        safe_log(f"could not read {path} ({exc}); the session context was not added to it")
        return
    base = strip_context_block(existing)
    block = f"{CONTEXT_BEGIN}\n{text.strip()}\n{CONTEXT_END}\n"
    merged = (base.rstrip("\n") + "\n\n" + block) if base.strip() else block
    write_container_file(env, path, merged,
                         mkdir=os.path.dirname(path) or None, user=user)


def remove_context_file(env, path: str, *, user: str = "coder") -> None:
    """Strip Collimate's fenced block from ``path``; delete the file when
    nothing else is left (it was ours alone). Best-effort — and when the
    container is already gone (a leg whose TTY died), the same edit is made
    on the HOST side of the bind mount: ``/workspace/<name>`` is the run's
    workspace directory, which ``gen`` reaches without a container."""
    try:
        existing = env.read_file(path).decode("utf-8", "replace")
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 — not read in the container (gone): host side
        _remove_context_file_host(path)
        return
    if not existing or CONTEXT_BEGIN not in existing:
        return
    rest = strip_context_block(existing)
    try:
        if rest.strip():
            write_container_file(env, path, rest, user=user)
        else:
            env.exec(["sh", "-c", f"rm -f {shlex.quote(path)}"])
    except Exception:  # noqa: BLE001
        _remove_context_file_host(path)


def _remove_context_file_host(path: str) -> None:
    """The host-side twin of :func:`remove_context_file` for a
    ``/workspace/…`` path: edit the cell file through ``gen``."""
    rel = path[len("/workspace/"):] if path.startswith("/workspace/") else ""
    if not rel:
        return
    try:
        if not gen.has_file(rel):
            return
        raw = gen.read_file(rel)
        existing = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if CONTEXT_BEGIN not in existing:
            return
        rest = strip_context_block(existing)
        if rest.strip():
            gen.update_file(rel, rest)
        else:
            gen.delete_file(rel)
    except Exception as e:  # noqa: BLE001
        safe_log(f"could not remove the harness context file {rel} ({e})")


def strip_context_block(text: str) -> str:
    """``text`` without Collimate's fenced block (and the blank line that
    separated it from the user's own content)."""
    start = text.find(CONTEXT_BEGIN)
    if start < 0:
        return text
    end = text.find(CONTEXT_END, start)
    if end < 0:
        return text[:start].rstrip("\n") + ("\n" if text[:start].strip() else "")
    end += len(CONTEXT_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    head = text[:start].rstrip("\n")
    tail = text[end:]
    if head and tail:
        return head + "\n" + tail
    return (head + "\n") if head else tail


_FILE_CHUNK_CHARS = 48 * 1024


def _large_file_write(env, path: str, content: str, *, mkdir=None,
                      user="coder", what=None, root=False) -> bool:
    """Append bounded encoded chunks, then atomically publish a fresh inode.

    Failed setup/append/decode leaves the destination untouched. Both temporary
    files are allocated in its directory and removed on every return path.
    """
    import base64
    parent = os.path.dirname(path) or "."
    setup = (f"install -d -m 755 {shlex.quote(parent)} && " if root else
             f"mkdir -p {shlex.quote(mkdir)} && " if mkdir else "")
    template = shlex.quote(parent + "/.collimate-write.XXXXXX")
    encoded = decoded = None

    def execute(script):
        code, output = env.exec(["sh", "-c", script], user=user)
        if code != 0:
            raise RuntimeError(f"{what or 'file write'} failed in the container: {output.strip()[:400]}")
        return output.strip()

    try:
        encoded = execute(setup + f"mktemp {template}")
        decoded = execute(f"mktemp {template}")
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        for offset in range(0, len(payload), _FILE_CHUNK_CHARS):
            chunk = shlex.quote(payload[offset:offset + _FILE_CHUNK_CHARS])
            execute(f"printf '%s' {chunk} >> {shlex.quote(encoded)}")
        execute(f"base64 -d {shlex.quote(encoded)} > {shlex.quote(decoded)} && "
                f"chmod 644 {shlex.quote(decoded)} && "
                f"mv -fT {shlex.quote(decoded)} {shlex.quote(path)}")
        return True
    except Exception:
        if what is not None:
            raise
        return False
    finally:
        temps = [value for value in (encoded, decoded) if value]
        if temps:
            try:
                env.exec(["sh", "-c", "rm -f " + " ".join(map(shlex.quote, temps))], user=user)
            except Exception:
                pass


def _file_script(path: str, content: str, *, mkdir: "str | None" = None) -> str:
    """The sh fragment that writes ``content`` to ``path`` (base64 through the
    shell, destinations quoted — caller-supplied names can't corrupt or
    execute inside the command), creating ``mkdir`` first when given."""
    import base64
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    if len(payload) > _FILE_CHUNK_CHARS:
        raise ValueError("large file writes require a chunked container writer")
    b64 = shlex.quote(payload)
    pre = f"mkdir -p {shlex.quote(mkdir)} && " if mkdir else ""
    return f"{pre}echo {b64} | base64 -d > {shlex.quote(path)}"


def write_container_file(
    env, path: str, content: str, *, mkdir: "str | None" = None,
    user: str = "coder",
) -> bool:
    """Write ``content`` to ``path`` inside the container, best-effort: for
    context the harness reads (instruction seams, staged docs, its own
    settings), never for a file its commands or web policy rests on — that
    is :func:`write_policy_file` or :func:`write_root_file`. Returns whether
    the write landed (exit 0); a non-zero exit is reported, never raised."""
    if len(content.encode("utf-8")) > _FILE_CHUNK_CHARS * 3 // 4:
        return _large_file_write(env, path, content, mkdir=mkdir, user=user)
    code, _ = env.exec(["sh", "-c", _file_script(path, content, mkdir=mkdir)], user=user)
    return code == 0


def enforce_in_container(env, script: str, *, what: str, user: str = "root") -> str:
    """Run ``script`` (sh) in the container — as root unless ``user`` names
    another — and return its stdout; raise, naming ``what``, when it fails.

    The seam for the policy a harness runs under. As root, a file written
    here is root-owned where the harness (``coder``) cannot rewrite it; as
    ``coder``, it is a file the harness may rewrite during the leg but must
    start each leg from. Unlike :func:`write_container_file`, a failure is
    never swallowed — a policy that silently did not land would leave the
    harness unrestricted."""
    code, text = env.exec(["sh", "-c", script], user=user)
    if code != 0:
        raise RuntimeError(f"{what} failed in the container: {text.strip()[:400]}")
    return text


def root_file_script(path: str, content: str) -> str:
    """The sh fragment that writes ``content`` to ``path`` as a root-owned,
    world-readable file (its directory created root-owned 0755), for
    :func:`enforce_in_container`. Any file already at ``path`` is removed
    first, so the result is a fresh root-owned inode whoever owned the old
    one and whatever still holds it open."""
    import base64
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    if len(payload) > _FILE_CHUNK_CHARS:
        raise ValueError("large file writes require a chunked container writer")
    b64 = shlex.quote(payload)
    q = shlex.quote(path)
    parent = shlex.quote(os.path.dirname(path) or "/")
    return (f"install -d -m 755 {parent} && rm -f {q}"
            f" && echo {b64} | base64 -d > {q} && chmod 644 {q}")


def write_root_file(env, path: str, content: str, *, what: str) -> None:
    """Write ``content`` to ``path`` as a root-owned file the harness cannot
    rewrite, raising (naming ``what``) when it does not land — the writer
    for the config a harness's commands level rests on."""
    if len(content.encode("utf-8")) > _FILE_CHUNK_CHARS * 3 // 4:
        _large_file_write(env, path, content, what=what, user="root", root=True)
        return
    enforce_in_container(env, root_file_script(path, content), what=what)


def write_policy_file(env, path: str, content: str, *, what: str,
                      mkdir: "str | None" = None) -> None:
    """Write ``content`` to ``path`` as the harness's own user (``coder``),
    raising (naming ``what``) when it does not land — for a file the harness
    may rewrite itself during a leg (goose's permission store, codex's
    config.toml) but whose content each leg must start from."""
    if len(content.encode("utf-8")) > _FILE_CHUNK_CHARS * 3 // 4:
        _large_file_write(env, path, content, mkdir=mkdir, what=what)
        return
    enforce_in_container(env, _file_script(path, content, mkdir=mkdir),
                         what=what, user="coder")
