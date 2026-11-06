# SPDX-License-Identifier: MIT
"""Cell-carried capabilities — skills + MCP declarations discovered from a
runner's ``_skills`` / ``_mcp`` config folders.

A runner tag provides capabilities to the Agent generators through the two
declared config folders (a ``.generator`` definition ``config_folders``). The host
stages them to ``.collrun/config/folders/{_skills,_mcp}/…`` BEFORE input
hydration, and the agent reads them with ``gen.config_folder("_skills")`` /
``gen.config_folder("_mcp")``. INPUT cell files can no longer arm a
capability — only the (trusted, runner-provided) config folders can:

  * ``_skills`` config folder → ``<name>/SKILL.md`` — the Agent Skills format,
    verbatim: YAML frontmatter (``name``, ``description``, plus any
    harness-specific keys — the frontmatter passes through as a SUPERSET,
    claude-code consumes the full contract, other harnesses use
    name/description and ignore the rest), then the markdown body. Sibling
    files (references, ``scripts/``) ride along in the folder; running a
    bundled script rides the existing exec ladder, never a new grant.

  * ``_mcp`` config folder → ``<name>.yaml`` (or ``.yml`` / ``.json``) — one
    MCP server declaration::

        transport: http | stdio
        url: https://...                 # http — hosted/remote is the primary case
        command: ["npx", "some-server"]  # stdio
        env:     { FOO: "${account:openai.api_key}" }   # keystore refs ONLY
        headers: { Authorization: "${account:notion.api_key}" }  # secret holds 'Bearer ...'
        allowed_tools: [a, b]            # optional allow-list

    ``env`` / ``headers`` values must be ``${account:<role>.<secret_name>}``
    references (the WHOLE value — a prefix like ``Bearer `` makes it a
    literal) — runner cells are versioned and exportable, so a secret literal
    in a folder file would leak with every share; a literal value drops that
    key with a warning. Consent and per-harness delivery are the engine's job
    (``cell_skills`` / ``cell_mcp`` config + the attachment seams); this
    module only parses, bounds, and fingerprints.

Pure by design: :func:`discover_cell_capabilities` takes the two config-folder
mappings (``{relpath: str | bytes}``, as ``gen.config_folder`` returns) so it
unit-tests with synthetic fixtures and never imports ``gen`` itself. Malformed
files warn and skip — discovery NEVER fails a turn.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import yaml

# The two config-folder slot names (must match the agent manifest's
# ``config_folders`` declaration). Used only for readable warning paths + the
# digest key prefix; discovery consumes the folder MAPPINGS, not these names.
SKILLS_DIR = "_skills"
MCP_DIR = "_mcp"

# Bounds — a runaway folder must not balloon the prompt or the config files.
_MAX_SKILLS = 50
_MAX_MCP = 20
_SKILL_BODY_CAP = 64 * 1024          # bytes of SKILL.md kept (body truncated)
_DESC_CAP = 500

_ACCOUNT_RE = re.compile(r"^\$\{account:([A-Za-z0-9_.-]+)\}$")
_MCP_EXTS = (".yaml", ".yml", ".json")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: str                          # folder identity within _skills (_skills/<x>)
    frontmatter: dict = field(default_factory=dict)   # superset pass-through


@dataclass
class McpDecl:
    name: str
    transport: str                    # "http" | "stdio"
    url: str = ""
    command: list = field(default_factory=list)
    env: dict = field(default_factory=dict)           # values: keystore refs
    headers: dict = field(default_factory=dict)       # values: keystore refs
    allowed_tools: list = field(default_factory=list)


@dataclass
class CellCaps:
    skills: list = field(default_factory=list)
    mcp: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    digest: str = ""

    @property
    def empty(self) -> bool:
        return not self.skills and not self.mcp


def account_ref(value) -> "str | None":
    """The keystore provider name inside a ``${account:...}`` reference, or
    ``None`` when ``value`` isn't one."""
    m = _ACCOUNT_RE.match(str(value or "").strip())
    return m.group(1) if m else None


def _parse_frontmatter(text: str) -> tuple:
    """``(frontmatter_dict, body)`` from a SKILL.md. No frontmatter → ``({},
    whole text)``; unparseable YAML → ``(None, body)`` so the caller can warn."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)    # first terminator only — the body may
    if len(parts) < 2:                # itself contain --- rules
        return {}, text
    raw = parts[0][3:]                # strip the leading ---
    body = parts[1].lstrip("\n")
    try:
        fm = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001 — malformed frontmatter
        return None, body
    return (fm if isinstance(fm, dict) else {}), body


def _skill_from(rel: str, text: str, warnings: list) -> "Skill | None":
    """Build a :class:`Skill` from a ``<folder>/SKILL.md`` config-folder entry
    (``rel`` is relative to the ``_skills`` folder root)."""
    folder = rel.split("/", 1)[0]
    fm, body = _parse_frontmatter(text[:_SKILL_BODY_CAP])
    if fm is None:
        warnings.append(f"{SKILLS_DIR}/{rel}: unparseable frontmatter — skill skipped")
        return None
    name = str(fm.get("name") or folder).strip() or folder
    return Skill(
        name=name,
        description=str(fm.get("description") or "").strip()[:_DESC_CAP],
        body=body.strip(),
        dir=f"{SKILLS_DIR}/{folder}",
        frontmatter=fm,
    )


def _refs_only(mapping, path: str, kind: str, warnings: list) -> dict:
    """Keep only ``${account:...}``-valued entries; a literal drops its KEY
    with a warning (never the whole server — the missing key is visible)."""
    out: dict = {}
    for k, v in (mapping or {}).items():
        if account_ref(v):
            out[str(k)] = str(v)
        else:
            warnings.append(
                f"{path}: {kind} {k!r} is a literal — use "
                "'${account:<provider>}' (cell files are exported/shared; "
                "key dropped)"
            )
    return out


def _mcp_from(rel: str, text: str, warnings: list) -> "McpDecl | None":
    """Build an :class:`McpDecl` from a ``<name>.{yaml,yml,json}`` config-folder
    entry (``rel`` is relative to the ``_mcp`` folder root)."""
    path = f"{MCP_DIR}/{rel}"
    try:
        doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"{path}: unparseable ({e}) — mcp declaration skipped")
        return None
    if not isinstance(doc, dict):
        warnings.append(f"{path}: not a mapping — mcp declaration skipped")
        return None
    name = rel
    for ext in _MCP_EXTS:
        name = name[: -len(ext)] if name.endswith(ext) else name
    transport = str(doc.get("transport") or "").strip().lower()
    url = str(doc.get("url") or "").strip()
    command = doc.get("command")
    if transport not in ("http", "stdio"):
        warnings.append(f"{path}: transport must be http or stdio — skipped")
        return None
    if transport == "http":
        if not url.lower().startswith(("http://", "https://")):
            warnings.append(f"{path}: http transport needs an http(s) url — skipped")
            return None
        command = []
    else:
        if not (isinstance(command, list) and command
                and all(isinstance(c, (str, int, float)) for c in command)):
            warnings.append(f"{path}: stdio transport needs a command list — skipped")
            return None
        command = [str(c) for c in command]
        url = ""
    allowed = doc.get("allowed_tools")
    allowed = [str(a) for a in allowed] if isinstance(allowed, list) else []
    return McpDecl(
        name=name, transport=transport, url=url, command=command,
        env=_refs_only(doc.get("env"), path, "env", warnings),
        headers=_refs_only(doc.get("headers"), path, "header", warnings),
        allowed_tools=allowed,
    )


def _folder_text(data) -> str:
    """Coerce a config-folder entry (``str`` or ``bytes``) to text."""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data if isinstance(data, str) else str(data)


def discover_cell_capabilities(skills_files: dict, mcp_files: dict) -> CellCaps:
    """Parse the runner's ``_skills`` + ``_mcp`` config folders into a
    :class:`CellCaps`.

    ``skills_files`` / ``mcp_files`` are ``{relpath: str | bytes}`` mappings
    relative to each folder root — exactly what ``gen.config_folder("_skills")``
    / ``gen.config_folder("_mcp")`` return (plain dicts in tests). Only
    top-level ``<folder>/SKILL.md`` skills and top-level ``<name>.{yaml,yml,json}``
    MCP files are picked up; nested/misplaced files are ignored. Bounded,
    tolerant, and fingerprinted: ``digest`` is a stable content hash over both
    folders. Config folders are STATIC for a run (staged once, before
    hydration), so the digest is constant across a run's turns; the engine
    (_agent_shared.discover_cell_caps) keeps the first discovery for the run
    and uses the digest only to log a mid-run change."""
    caps = CellCaps()
    h = hashlib.sha256()

    # Fingerprint EVERY file in both folders (skills scripts + references too),
    # so the digest faithfully identifies the provided capability set.
    for root, files in ((SKILLS_DIR, skills_files), (MCP_DIR, mcp_files)):
        for rel in sorted(files or {}):
            h.update(f"{root}/{rel}".encode())
            data = files[rel]
            payload = data if isinstance(data, bytes) else str(data).encode()
            h.update(hashlib.sha256(payload).digest())

    for rel in sorted(skills_files or {}):
        # A skill is exactly ``<folder>/SKILL.md`` (one level deep); deeper or
        # bare-root SKILL.md files are sibling material, not a skill root.
        if not (rel.endswith("/SKILL.md") and rel.count("/") == 1):
            continue
        if len(caps.skills) >= _MAX_SKILLS:
            caps.warnings.append(
                f"{SKILLS_DIR}/{rel}: over the {_MAX_SKILLS}-skill cap — skipped"
            )
            continue
        skill = _skill_from(rel, _folder_text(skills_files[rel]), caps.warnings)
        if skill is not None:
            caps.skills.append(skill)

    for rel in sorted(mcp_files or {}):
        # A declaration is a top-level ``<name>.{yaml,yml,json}`` (no subdir).
        if not (rel.endswith(_MCP_EXTS) and "/" not in rel):
            continue
        if len(caps.mcp) >= _MAX_MCP:
            caps.warnings.append(
                f"{MCP_DIR}/{rel}: over the {_MAX_MCP}-server cap — skipped"
            )
            continue
        decl = _mcp_from(rel, _folder_text(mcp_files[rel]), caps.warnings)
        if decl is not None:
            caps.mcp.append(decl)

    if caps.skills or caps.mcp:
        caps.digest = h.hexdigest()
    return caps


def skills_index(skills: list, *, root: str = "") -> str:
    """The one-line-per-skill index appended to system prompts / staged docs —
    name + description only (bodies stay lazy).

    ``root`` is the path where the harness can READ a skill's ``SKILL.md`` as a
    plain file (the workspace-relative config-folder root every harness's own
    file tools reach). When ``root`` is blank (no config folder to point at),
    the index points at the bridge's ``load_skill`` tool instead."""
    if not skills:
        return ""
    if root:
        header = "Skills available for this run (read the full skill before using it):"
    else:
        header = ("Skills available for this run — call the ``load_skill`` tool with a "
                  "skill's name to read its full instructions before using it:")
    lines = [header]
    for s in skills:
        desc = f" — {s.description}" if s.description else ""
        if root:
            folder = s.dir.rsplit("/", 1)[-1]
            lines.append(f"  * {s.name}{desc}  [{root}/{folder}/SKILL.md]")
        else:
            lines.append(f"  * {s.name}{desc}")
    return "\n".join(lines)
