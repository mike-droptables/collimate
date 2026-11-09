#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Project the Agent's harness adapters into everything that mirrors them.

The canonical facts live in one declaration per harness,
``content/generators/_agent_shared/harnesses/<package>/adapter.py`` in the
workspace. SDK core must not
import that runtime, and the settings schema and the presets are files a
runner reads, so this script writes their projections:

* ``src/collimate/harness_catalog.py`` — the metadata core reads (account
  rules, placement facts, plan logins, model defaults);
* ``content/generators/agent/agent/agent-config.set_yaml`` — the ``backend`` options
  and the harnesses the ``mcp`` options are disabled on;
* ``content/generators/agent/agent/agent.generator`` — the model role's ``harnesses``,
  the harnesses whose CLI surface runs in their container on every account
  (``requires_docker_if``) and the harness presets' entries in ``configs``;
* ``content/generators/agent/agent/configs/<preset>/manifest.config_yaml`` — one
  preset per harness.

``tests/test_harness_catalog.py`` pins the bytes.

    python scripts/sync_harness_catalog.py          # write the projections
    python scripts/sync_harness_catalog.py --check  # list what differs; exit 1 if any
"""
from __future__ import annotations

import pprint
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
GENERATORS = WORKSPACE / "content" / "generators"
CATALOG = ROOT / "src" / "collimate" / "harness_catalog.py"
AGENT = GENERATORS / "agent" / "agent"
SETTINGS = AGENT / "agent-config.set_yaml"
MANIFEST = AGENT / "agent.generator"
# The in-process engine's preset is the Agent's default one.
PRESET_IDS = {"pydantic-ai": "default"}


def _registry():
    sys.path.insert(0, str(GENERATORS))
    sys.path.insert(0, str(ROOT / "src"))
    from _agent_shared.harnesses import HARNESS
    return HARNESS


def _login(row) -> "dict | None":
    login = row.auth.login
    if login is None:
        return None
    return {
        "command": login.command, "relative": login.relative, "field": login.field,
        "lease": tuple(login.lease), "validator": login.validator, "env": login.env,
        "suffix": login.suffix, "also": tuple(login.also), "store_keys": dict(login.store_keys),
        "copy": login.copy, "hint": login.hint,
    }


def _terminal_loci(row) -> dict:
    """``{auth: "host" | "container"}``: where the CLI surface runs for each
    auth the harness offers (the adapter's ``Terminal.locus``, resolved over
    its finite domain); ``{}`` without a CLI surface."""
    from _agent_shared.harnesses import terminal_locus
    return {auth: terminal_locus(row, auth) for auth in row.auths} if row.terminal else {}


def catalog_rows(registry) -> dict:
    rows = {}
    for name, row in registry.items():
        rows[name] = {
            "label": row.label,
            "locus": row.locus,
            "host_binary": row.host_binary,
            "host_twin": row.host_twin,
            "terminal": row.terminal.tier if row.terminal else "",
            "terminal_locus": _terminal_loci(row),
            "providers": row.providers,
            "default_provider": row.auth.default_provider,
            "auths": row.auths,
            "plans": dict(row.auth.plans),
            "custom_endpoint": row.auth.custom_endpoint,
            "plan_default_for": tuple(row.auth.plan_default_for),
            "login": _login(row),
            "default_model": row.model.default,
            "vocabulary": tuple(row.model.vocabulary),
            "unchecked": row.model.unchecked,
            "plan_defaults": dict(row.model.plan_defaults),
            "key_defaults": dict(row.model.key_defaults),
            "cell_mcp": row.tools.cell_mcp,
            "commands": tuple(row.commands.levels),
            "commands_sandboxed": row.commands.sandboxed,
        }
    return rows


def catalog_source(registry) -> str:
    body = pprint.pformat(catalog_rows(registry), width=100, sort_dicts=False)
    return (
        "# SPDX-License-Identifier: MIT\n"
        '"""The Agent\'s harnesses, as SDK core sees them — GENERATED, do not edit.\n'
        "\n"
        "``scripts/sync_harness_catalog.py`` writes this from the harness adapters\n"
        "(``content/generators/_agent_shared/harnesses/<package>/adapter.py``); edit\n"
        "those and rerun it. :mod:`collimate.account_taxonomy` and\n"
        ":mod:`collimate.harness_placement` read it; the generator runtime is never\n"
        "imported here.\n"
        '"""\n'
        "from __future__ import annotations\n"
        "\n"
        f"HARNESSES: dict = {body}\n"
    )


def _replace_block(text: str, tag: str, lines: list, indent: str) -> str:
    """Replace every ``# <tag>`` … ``# </tag>`` block's body with ``lines``."""
    pattern = re.compile(rf"(?m)^(?P<i>[ \t]*)# <{tag}>\n.*?^(?P=i)# </{tag}>\n", re.S)
    if not pattern.search(text):
        raise SystemExit(f"{tag}: marker block missing")

    def body(match) -> str:
        pad = match.group("i")
        return (f"{pad}# <{tag}>\n" + "".join(f"{pad}{line}\n" for line in lines)
                + f"{pad}# </{tag}>\n")
    return pattern.sub(body, text)


def settings_source(registry) -> str:
    text = SETTINGS.read_text(encoding="utf-8")
    options = []
    for name, row in registry.items():
        options.append(f"- value: {name}")
        options.append(f"  label: {_scalar(row.label)}")
        if row.note:
            options.append(f"  description: {_scalar(row.note)}")
        if not row.terminal:
            options += ["  enable_if:", "    not:", "      surface: terminal"]
    text = _replace_block(text, "harness-catalog:backend-options", options, "")
    no_mcp = [f"- {name}" for name, row in registry.items() if not row.tools.cell_mcp]
    return _replace_block(text, "harness-catalog:no-cell-mcp", no_mcp, "")


def _scalar(value: str) -> str:
    dumped = yaml.safe_dump(value, width=10_000, default_flow_style=True).strip()
    return dumped[:-4].rstrip() if dumped.endswith("\n...") else dumped.removesuffix("...").strip()


def manifest_source(registry) -> str:
    text = MANIFEST.read_text(encoding="utf-8")
    roster = ", ".join(registry)
    text, count = re.subn(r"(?m)^(    harnesses: )\[[^\]]*\]$", rf"\g<1>[{roster}]", text)
    if count != 1:
        raise SystemExit("agent.generator: the model role's harnesses line is missing")
    container_terminals = [f"- {name}" for name, row in registry.items()
                           if set(_terminal_loci(row).values()) == {"container"}]
    text = _replace_block(text, "harness-catalog:container-terminals", container_terminals, "")
    entries = []
    for name, row in registry.items():
        preset = PRESET_IDS.get(name, name)
        entries += [f"- id: {preset}", f"  name: {_scalar('Agent' if preset == 'default' else row.label)}",
                    f"  path: configs/{preset}/manifest.config_yaml"]
    return _replace_block(text, "harness-catalog:presets", entries, "")


def preset_source(row) -> str:
    preset = {
        "name": row.label if row.id != "pydantic-ai" else "Agent",
        "description": row.summary,
        "tags": ["agent"] + ([] if row.id == "pydantic-ai" else [row.id]),
        "prompt": "",
        "settings": {"agent-config.set_yaml": {
            "surface": "annotation", "session": "interactive", "backend": row.id,
            "system_prompt": "", "mcp": "off", "approvals": "auto", "selected_file_first": False,
        }},
        "folders": [],
        "accounts": {},
        "permissions": dict(row.preset_permissions),
        "packages": {},
    }
    return yaml.safe_dump(preset, sort_keys=False, width=100, allow_unicode=True)


def expected(registry=None) -> dict:
    registry = registry or _registry()
    out = {CATALOG: catalog_source(registry), SETTINGS: settings_source(registry),
           MANIFEST: manifest_source(registry)}
    for name, row in registry.items():
        preset = PRESET_IDS.get(name, name)
        out[AGENT / "configs" / preset / "manifest.config_yaml"] = preset_source(row)
    return out


def main(argv: list) -> int:
    targets = expected()
    stale = [path for path, text in targets.items()
             if not path.is_file() or path.read_text(encoding="utf-8") != text]
    if "--check" in argv:
        for path in stale:
            print(f"stale: {path.relative_to(WORKSPACE)}")
        return 1 if stale else 0
    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(targets[path], encoding="utf-8")
        print(f"wrote {path.relative_to(WORKSPACE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
