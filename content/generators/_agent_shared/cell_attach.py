# SPDX-License-Identifier: MIT
"""Helpers for delivering discovered cell MCP declarations and skills to a
harness: keystore-secret resolution (:func:`resolve_decl_secrets`, awaited
once per turn in run_agent; :func:`resolve_secrets`, the sync pass), the
container localhost rewrite (:func:`rewrite_for_container`), and the container
skills root (:func:`container_skills_root`). The MCP config mapping itself
lives in ``acp_client.build_mcp_servers``.

Networking rule, applied by the container-locus mapper: a container-bound
harness can't reach the host's ``localhost``/``127.0.0.1`` — those rewrite
to ``host.docker.internal`` (the ``extra_hosts`` gateway the CLI containers
already map for the workspace bridge). A host-locus harness keeps the URL
as-is (it shares the host network). One parser decides both which URL is a
host loopback URL (:func:`is_container_loopback`, the SSRF gate's exemption)
and what the rewrite changes, so the two cannot disagree.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from .cell_caps import SKILLS_DIR, account_ref

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


def _loopback_parts(url: str):
    """``(parts, port)`` of a host loopback URL, else ``None`` — the one
    parse :func:`is_container_loopback` and :func:`rewrite_for_container`
    share."""
    try:
        parts = urlsplit(url or "")
        port = parts.port               # raises on a malformed port
    except ValueError:
        return None
    if (parts.scheme.lower() in ("http", "https")
            and (parts.hostname or "") in _LOOPBACK_HOSTS
            and parts.username is None and parts.password is None):
        return parts, port
    return None


def is_container_loopback(url: str) -> bool:
    """Whether ``url`` is a host loopback URL a container harness reaches as
    ``host.docker.internal``: an http(s) URL whose parsed host is
    ``localhost`` or ``127.0.0.1``, with no userinfo. A URL carrying
    userinfo is never one — ``http://localhost:x@169.254.169.254/`` names
    the metadata address, not loopback — and neither is one whose port does
    not parse."""
    return _loopback_parts(url) is not None


def rewrite_for_container(url: str) -> str:
    """A host loopback URL (:func:`is_container_loopback`) with its host
    replaced by ``host.docker.internal`` (port, path, query and fragment
    kept) for a harness running inside a container; any other URL passes
    through unchanged."""
    found = _loopback_parts(url)
    if found is None:
        return url or ""
    parts, port = found
    netloc = "host.docker.internal" + (f":{port}" if port is not None else "")
    return urlunsplit(parts._replace(netloc=netloc))


def container_skills_root() -> str:
    """The path, relative to the container's ``/workspace`` mount, at which a
    container-bound harness reads the runner's ``_skills`` config folder.

    The CLI containers bind-mount the whole run workspace at ``/workspace``, and
    the host stages config folders UNDER that workspace at
    ``.collrun/config/folders/<name>/…`` — so the staged ``_skills`` tree
    is already visible inside the container without any extra copy or mount.
    This derives the workspace-relative path from ``gen.config_folder_path`` so
    the terminal/CLI system-prompt skill index can point the harness's own file
    tools at the real ``SKILL.md`` files (see :func:`cell_caps.skills_index`).

    Best-effort: returns ``""`` in a bridge-free context (no config folders to
    point at)."""
    try:
        import gen

        rel = os.path.relpath(gen.config_folder_path(SKILLS_DIR), gen.workspace_path())
        return rel.replace("\\", "/")
    except Exception:  # noqa: BLE001 — bridge-free / dev context
        return ""


def resolve_secrets(mapping: dict, fetch_key) -> dict:
    """Resolve a ``{name: '${account:provider}' | literal}`` map to
    ``{name: value}``.

    Two input shapes converge here: a ``${account:provider}`` reference is
    resolved via ``fetch_key(provider) -> str|None`` (a SYNC accessor — a test
    stub, or a pre-resolved lookup; NEVER the async ``account_secret`` directly,
    which would stringify a coroutine), and a plain literal passes through
    unchanged. The literal path is what makes :func:`resolve_decl_secrets`
    work: it pre-resolves refs to literals ONCE (awaited) in run_agent, so the
    sync MCP mappers (build_mcp_servers, cell_mcp_toolsets) see literals
    and never touch the keystore again. A ref that resolves empty/None drops
    its key (the server just won't get that credential — visible, not silently
    wrong)."""
    out: dict = {}
    for name, ref in (mapping or {}).items():
        provider = account_ref(ref)
        if provider is None:
            if ref:                        # a pre-resolved literal — pass through
                out[str(name)] = str(ref)
            continue
        try:
            val = fetch_key(provider) if fetch_key else None
        except Exception:  # noqa: BLE001 — a missing key just omits the entry
            val = None
        if val:
            out[str(name)] = str(val)
    return out


async def resolve_decl_secrets(decls, api_key) -> list:
    """Return copies of ``decls`` whose ``env``/``headers`` keystore refs are
    resolved to LITERAL secrets — awaited ONCE per provider up front, so the
    sync per-harness mappers never have to (the async ``account_secret`` can't be
    called from a sync mapper). ``api_key`` is the async ``account_secret``; a
    provider that yields nothing drops its key. Decls are shallow-copied via
    ``dataclasses.replace`` so the originals (and their still-ref form used by
    the unit tests) are untouched."""
    from dataclasses import replace

    cache: dict = {}

    async def _get(provider: str):
        if provider not in cache:
            try:
                cache[provider] = await api_key(provider)
            except Exception:  # noqa: BLE001
                cache[provider] = None
        return cache[provider]

    async def _resolve(mapping: dict) -> dict:
        out: dict = {}
        for name, ref in (mapping or {}).items():
            provider = account_ref(ref)
            if provider is None:
                if ref:
                    out[str(name)] = str(ref)
                continue
            val = await _get(provider)
            if val:
                out[str(name)] = str(val)
        return out

    resolved: list = []
    for d in decls or []:
        resolved.append(replace(
            d,
            env=await _resolve(d.env),
            headers=await _resolve(d.headers),
        ))
    return resolved
