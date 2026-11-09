# SPDX-License-Identifier: MIT
"""Resolve a requested artifact version to a CONCRETE one.

A runner says *which version* of a named artifact it wants — ``latest`` or a
pin. The generator says *where that artifact lives upstream*. This module joins
the two: given a source (``npm:@openai/codex``, ``npm:opencode-ai``,
``github:jesseduffield/lazygit``) and a request, it returns the exact version
to install.

Why resolve at all, rather than letting the Dockerfile say ``@latest``?
Because an image's identity is a hash of its build inputs. ``@latest`` is
stable as a *string* while the thing it names moves, so a hash over it would
claim an identity the image doesn't have — the same lie a floating install
told before, just relocated. Resolving first means a new upstream release
changes the build args, which changes the tag, which is what makes "make sure
I have the latest" actually rebuild — and, equally important, what makes an
UNCHANGED upstream a cache hit that costs nothing.

Resolution reaches the network, so callers gate it on the run's
``update_packages`` grant and fall back to the pinned default when it is off
(see :func:`resolve_requests`). Every failure degrades to ``None`` — a
resolution problem must leave the Dockerfile's own default in charge, never
fail a run.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

# A version request that means "whatever upstream currently ships".
LATEST = "latest"

_TIMEOUT = 15
_GITHUB_API = "https://api.github.com"
_SEMVERISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~-]*$")


class UpstreamError(RuntimeError):
    """Resolution failed (network, unknown source kind, malformed response)."""


def _github_headers(token: "str | None") -> dict[str, str]:
    """Headers for a GitHub REST request, authenticated when ``token`` is set."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "collimate"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, headers: "dict | None" = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {
        "Accept": "application/json",
        "User-Agent": "collimate-upstream-resolver",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpstreamError(f"{url}: {e}") from e


def latest_version(source: str, github_token: "str | None" = None) -> str:
    """The current version at ``source`` (``<kind>:<ref>``).

    Kinds: ``npm`` (the registry's ``latest`` dist-tag), ``pypi`` (the JSON
    API's ``info.version``), ``github`` (the newest release's tag, leading
    ``v`` stripped so it composes with a URL template; asked with
    ``github_token`` when the run binds a github.com token, which lifts the
    anonymous rate limit, and asked once more anonymously when GitHub answers
    the token with 401/403). Raises :class:`UpstreamError` on anything
    unexpected."""
    kind, _, ref = (source or "").partition(":")
    kind, ref = kind.strip().lower(), ref.strip()
    if not kind or not ref:
        raise UpstreamError(f"malformed upstream source {source!r}")
    if kind == "npm":
        # /<pkg>/latest works for scoped names too (the slash is legal here).
        data = _get_json(f"https://registry.npmjs.org/{ref}/latest")
        version = str(data.get("version") or "")
    elif kind == "pypi":
        data = _get_json(f"https://pypi.org/pypi/{ref}/json")
        version = str((data.get("info") or {}).get("version") or "")
    elif kind == "github":
        url = f"{_GITHUB_API}/repos/{ref}/releases/latest"
        try:
            data = _get_json(url, _github_headers(github_token))
        except UpstreamError as e:
            # A token GitHub rejects (revoked, or an organisation's SSO) must
            # not cost the lookup: public releases answer anonymously.
            if github_token and isinstance(e.__cause__, urllib.error.HTTPError) and e.__cause__.code in (401, 403):
                data = _get_json(url, _github_headers(None))
            else:
                raise
        version = str(data.get("tag_name") or "").lstrip("v")
    else:
        raise UpstreamError(f"unknown upstream kind {kind!r} in {source!r}")
    if not version or not _SEMVERISH.match(version):
        raise UpstreamError(f"{source}: implausible version {version!r}")
    return version


# The last version each source resolved to, host-wide: a run that cannot
# reach the registry (offline, rate-limited) reuses it and lands on the image
# tag this machine last built, instead of falling to the Dockerfile default —
# a tag it may never have built and cannot build offline either.
_RECORD_TTL = 3600.0


def record_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "collimate", "upstream-versions.json")


def _read_record() -> dict:
    try:
        with open(record_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_record(record: dict) -> None:
    path = record_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass   # the record is a convenience, never a gate


def remember_resolution(source: str, version: str) -> None:
    record = _read_record()
    entry = dict(record.get(source) or {})
    entry.update({"checked_at": time.time(), "version_seen": version})
    record[source] = entry
    _write_record(record)




def last_resolution(source: str) -> tuple:
    """``(version_seen, checked_at)`` for ``source``, or ``("", 0.0)``."""
    entry = _read_record().get(source) or {}
    version = str(entry.get("version_seen") or "")
    try:
        checked = float(entry.get("checked_at") or 0.0)
    except (TypeError, ValueError):
        checked = 0.0
    return version, checked


def resolve_requests(
    artifacts: dict, requests: dict, *, allow_network: bool, log=None,
    github_token: "str | None" = None,
) -> dict:
    """``{build_arg: concrete_version}`` for the artifacts a run should install.

    ``artifacts`` is the generator's declaration — ``{name: {"source":
    "<kind>:<ref>", "arg": "<BUILD_ARG>"}}`` — i.e. where each named thing
    lives and which Dockerfile ARG carries its version. ``requests`` is what
    the RUNNER asked for: ``{name: "latest" | "<version>"}``.

    A pinned request is used verbatim and never touches the network. A
    ``latest`` request resolves upstream when ``allow_network`` (the run's
    ``update_packages`` grant) permits — at most once an hour per source, the
    answer being remembered in :func:`record_path` — and every successful
    answer is remembered; when the registry cannot be reached, or the grant
    forbids fetching, the last remembered version stands in, so the run lands
    on an image this machine has built before. Only a source never resolved
    on this machine is omitted, leaving the Dockerfile's own ARG default.
    Unknown names are reported and skipped — a runner naming an artifact this
    generator doesn't build is a mistake worth seeing, not a silent no-op.
    ``github_token`` (the run's Git account when it is a github.com token,
    :func:`git_account.release_token`) authenticates ``github:`` sources."""
    out: dict = {}
    for name, want in (requests or {}).items():
        decl = (artifacts or {}).get(name)
        if not decl:
            if log:
                log(
                    f"[upstream] runner asked for {name!r}, which this generator "
                    f"does not install — known: {', '.join(sorted(artifacts or {})) or 'none'}"
                )
            continue
        arg = str(decl.get("arg") or "")
        if not arg:
            continue
        want = str(want or "").strip()
        if want and want.lower() != LATEST:
            # A pinned version is interpolated into a Dockerfile RUN line
            # (``npm install -g pkg@${VERSION}``), i.e. into a shell. Runners
            # are authorable by a run holding authoring>=write_runners, so
            # validate here and not only in lint: a version carrying shell
            # metacharacters would otherwise be command injection into an
            # image build.
            if not _SEMVERISH.match(want):
                if log:
                    log(
                        f"[upstream] {name}: refusing version {want!r} — not a "
                        "plain version string"
                    )
                continue
            out[arg] = want
            continue
        source = str(decl.get("source") or "")
        seen, checked = last_resolution(source)
        if not allow_network:
            if seen:
                if log:
                    log(f"[upstream] {name}: 'latest' requested but this run may not fetch upstream "
                        f"(permissions.update_packages is off) — using the last resolved {seen}")
                out[arg] = seen
            elif log:
                log(
                    f"[upstream] {name}: 'latest' requested but this run may not "
                    "fetch upstream (permissions.update_packages is off) — "
                    "using the pinned default"
                )
            continue
        if seen and time.time() - checked < _RECORD_TTL:
            if log:
                log(f"[upstream] {name}: latest is {seen} (resolved within the hour)")
            out[arg] = seen
            continue
        try:
            version = latest_version(source, github_token=github_token)
        except UpstreamError as e:
            if seen:
                if log:
                    log(f"[upstream] {name}: could not resolve latest ({e}) — using the last resolved {seen}")
                out[arg] = seen
            elif log:
                log(f"[upstream] {name}: could not resolve latest ({e}) — using the pinned default")
            continue
        remember_resolution(source, version)
        if log:
            log(f"[upstream] {name}: latest is {version}")
        out[arg] = version
    return out


__all__ = ["LATEST", "UpstreamError", "latest_version", "resolve_requests"]
