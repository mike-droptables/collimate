"""Per-run optional dependencies — the extras the SELECTED config value needs.

Some generators carry soft dependencies: a config choice (an agent backend, a
converter engine) decides which distributions the run actually imports, and
naming all of them in the entrypoint's PEP 723 header would make every run pay
to resolve every branch. The generator declares the mapping in its manifest:

    optional_dependencies:
      selector: { file: "<a draft .set_yaml>", key: "<field key>" }
      groups:
        <value>: ["<requirement>", …]

The host resolves the group from the run's ALREADY-MERGED config (the same
seeded ``.set_yaml`` content that lands in ``.collrun/config/files/``) and
hands ``uv run`` one ``--with`` per requirement — which composes with the
inline header rather than replacing it. It never learns what a group means:
only which key decides, and what strings that value maps to. A value with no
group, an absent selector file, or a missing key all resolve to no extras.

FRESHNESS is a separate axis from identity. ``--with`` requirements are
unpinned, and uv treats an already-resolvable env as satisfied — so a group
installed months ago stays at that version until something re-resolves it.
``--upgrade-package`` (``-P``) is the only flag that does (measured:
``--refresh``/``--refresh-package`` are swallowed by the satisfied-check), and
it is exactly the ``update_packages`` grant's meaning, so it rides only when
the run holds it. Off-grant runs get whatever the cache already has.
"""
from __future__ import annotations

import re

# The leading distribution name of a requirement string — what ``-P`` takes.
# Stops at an extras bracket, a version specifier, a URL/direct reference, or
# an environment marker.
_REQ_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def _norm(value) -> str:
    """The comparison form of a config value / group key.

    ``.set_yaml`` is YAML 1.1, where a bare ``on``/``off``/``yes``/``no``
    dropdown value — and a group key spelled the same way — both load as
    Python bools. Normalizing both sides through here keeps such a pair
    matching instead of silently missing.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value).strip()


def resolve_optional_requirements(manifest: dict | None, config_files: dict | None) -> list[str]:
    """The requirement strings this run's config selects — ``[]`` for most runs.

    ``config_files`` is ``{rel: content}`` of the run's seeded config files
    (create_run's ``seeded_files``), so the
    values read here are exactly the ones the generator will see. Malformed
    declarations degrade to no extras: a generator whose manifest block is
    wrong must not take the run down with it.
    """
    block = (manifest or {}).get("optional_dependencies")
    if not isinstance(block, dict):
        return []
    selector = block.get("selector")
    groups = block.get("groups")
    if not isinstance(selector, dict) or not isinstance(groups, dict):
        return []
    rel = selector.get("file")
    key = selector.get("key")
    if not isinstance(rel, str) or not isinstance(key, str):
        return []
    content = (config_files or {}).get(rel)
    if not isinstance(content, str):
        return []

    from collimate.config_values import parse_set_yaml, resolve_form_values
    _fields, resolved = resolve_form_values(parse_set_yaml(content))
    if key not in resolved:
        return []
    wanted = _norm(resolved[key])
    if not wanted:
        return []

    reqs = next(
        (v for k, v in groups.items() if _norm(k) == wanted and isinstance(v, list)),
        None,
    )
    if reqs is None:
        return []
    # dict.fromkeys: dedupe, keep declaration order (the argv is user-visible).
    return list(dict.fromkeys(
        r.strip() for r in reqs if isinstance(r, str) and r.strip()
    ))


def requirement_name(requirement: str) -> str:
    """The distribution name of a requirement string (``smolagents[litellm]``
    → ``smolagents``), or ``""`` when it doesn't start with one."""
    m = _REQ_NAME_RE.match(requirement.strip())
    return m.group(0) if m else ""


def uv_run_argv(
    entrypoint: str, *, requirements=(), upgrade: bool = False,
) -> list[str]:
    """The ``uv run`` argv for a generator entrypoint — the ONE builder both
    launch paths use (native subprocess and the ``docker run`` tail), so a
    grant that only changes where the run lands can't change what it installs.

    ``-P`` no-ops silently (rc=0) without network, so it is a best-effort
    refresh of the named distributions, never a guarantee.
    """
    argv = ["uv", "run"]
    for req in requirements:
        argv += ["--with", req]
    if upgrade:
        for name in dict.fromkeys(requirement_name(r) for r in requirements):
            if name:
                argv += ["--upgrade-package", name]
    argv.append(entrypoint)
    return argv


def install_phase_message(requirements, *, upgrade: bool = False) -> str:
    """The ``spinning_up`` sub-message naming the extras — ``""`` when there
    are none. A cold group is a multi-second download that happens before the
    generator can report anything, so the phase says what it is waiting on
    instead of looking like a hang."""
    reqs = list(requirements or [])
    if not reqs:
        return ""
    verb = "installing/updating" if upgrade else "installing"
    return f"{verb} {', '.join(reqs)}"
