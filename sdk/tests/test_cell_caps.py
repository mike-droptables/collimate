# SPDX-License-Identifier: MIT
"""Unit tests for cell-carried capability discovery (``_agent_shared.cell_caps``).

Pure fixtures — discovery consumes the two config-folder MAPPINGS
(``{relpath: content}``, as ``gen.config_folder("_skills")`` /
``gen.config_folder("_mcp")`` return), so no bridge, no filesystem, no gen.
Relpaths are relative to each folder root: a skill is ``<folder>/SKILL.md``, an
MCP declaration is ``<name>.{yaml,yml,json}``."""
from __future__ import annotations



from _agent_shared import cell_caps as cc  # noqa: E402


SKILL = """---
name: release-notes
description: Draft release notes from the git log.
allowed-tools: Bash(git log:*)
---
# Release notes

Run `scripts/collect.py` first.
"""


def test_discovers_skills_and_mcp():
    caps = cc.discover_cell_capabilities(
        {
            "release-notes/SKILL.md": SKILL,
            "release-notes/scripts/collect.py": "print('hi')",
        },
        {
            "notion.yaml": (
                "transport: http\nurl: https://mcp.notion.com/mcp\n"
                "headers: { Authorization: '${account:notion_mcp}' }\n"
                "allowed_tools: [search, fetch]\n"
            ),
        },
    )
    assert [s.name for s in caps.skills] == ["release-notes"]
    s = caps.skills[0]
    assert s.description.startswith("Draft release notes")
    assert s.dir == "_skills/release-notes"
    assert s.body.startswith("# Release notes")
    assert s.frontmatter["allowed-tools"] == "Bash(git log:*)"   # superset pass-through
    assert [m.name for m in caps.mcp] == ["notion"]
    m = caps.mcp[0]
    assert m.transport == "http" and m.url == "https://mcp.notion.com/mcp"
    assert m.headers == {"Authorization": "${account:notion_mcp}"}
    assert m.allowed_tools == ["search", "fetch"]
    assert caps.warnings == [] and caps.digest and not caps.empty


def test_input_files_cannot_spoof_capabilities():
    """Only the config-folder mappings feed discovery — there is no workspace
    scan, so nothing an INPUT cell drops can arm a skill or MCP server."""
    caps = cc.discover_cell_capabilities({}, {})
    assert caps.empty and caps.digest == ""


def test_bytes_content_is_decoded():
    caps = cc.discover_cell_capabilities(
        {"quick/SKILL.md": b"---\nname: quick\n---\nbody"}, {},
    )
    assert caps.skills[0].name == "quick" and caps.skills[0].body == "body"


def test_frontmatterless_skill_defaults_to_folder_name():
    caps = cc.discover_cell_capabilities(
        {"quick/SKILL.md": "Just a body, no frontmatter."}, {},
    )
    assert caps.skills[0].name == "quick"
    assert caps.skills[0].description == ""
    assert caps.skills[0].body == "Just a body, no frontmatter."


def test_body_keeps_later_horizontal_rules():
    caps = cc.discover_cell_capabilities(
        {"hr/SKILL.md": "---\nname: hr\n---\ntop\n---\nbottom\n"}, {},
    )
    assert caps.skills[0].body == "top\n---\nbottom"


def test_stdio_and_validation_failures_warn_and_skip():
    caps = cc.discover_cell_capabilities({}, {
        "local.yaml": "transport: stdio\ncommand: [npx, some-server]\n",
        "bad-transport.yaml": "transport: websocket\nurl: https://x\n",
        "bad-url.yaml": "transport: http\nurl: ftp://x\n",
        "no-cmd.yaml": "transport: stdio\n",
        "garbage.yaml": "::: not yaml {{{",
        "scalar.yaml": "42",
    })
    assert [m.name for m in caps.mcp] == ["local"]
    assert caps.mcp[0].command == ["npx", "some-server"]
    assert len(caps.warnings) == 5
    # Warnings carry the readable _mcp/<file> path prefix.
    assert all(w.startswith("_mcp/") for w in caps.warnings)


def test_env_header_literals_drop_the_key_with_warning():
    caps = cc.discover_cell_capabilities({}, {
        "mixed.yaml": (
            "transport: http\nurl: https://api.example.com/mcp\n"
            "env: { OK: '${account:openai}', LEAK: 'sk-live-secret' }\n"
            "headers: { Authorization: 'Bearer raw-token' }\n"
        ),
    })
    m = caps.mcp[0]
    assert m.env == {"OK": "${account:openai}"}
    assert m.headers == {}
    assert sum("literal" in w for w in caps.warnings) == 2
    assert cc.account_ref("${account:openai}") == "openai"
    assert cc.account_ref("plain") is None


def test_nested_and_deep_paths_are_ignored():
    caps = cc.discover_cell_capabilities(
        {
            "SKILL.md": "bare-root file, not a skill folder",
            "a/b/SKILL.md": "too deep",
        },
        {"sub/x.yaml": "transport: http\nurl: https://x\n"},
    )
    assert caps.empty


def test_digest_tracks_content_and_empty_is_stable():
    skills = {"a/SKILL.md": "---\nname: a\n---\nbody"}
    d1 = cc.discover_cell_capabilities(skills, {}).digest
    d1b = cc.discover_cell_capabilities(dict(skills), {}).digest
    skills2 = {"a/SKILL.md": "---\nname: a\n---\nCHANGED"}
    d2 = cc.discover_cell_capabilities(skills2, {}).digest
    assert d1 == d1b and d1 != d2
    # A sibling (non-SKILL.md) file changing still bumps the fingerprint.
    d3 = cc.discover_cell_capabilities(
        {**skills, "a/scripts/x.py": "print(1)"}, {},
    ).digest
    assert d3 != d1
    assert cc.discover_cell_capabilities({}, {}).digest == ""


def test_caps_bound_the_folders():
    skills = {
        f"s{i:03}/SKILL.md": f"---\nname: s{i}\n---\nb" for i in range(60)
    }
    caps = cc.discover_cell_capabilities(skills, {})
    assert len(caps.skills) == cc._MAX_SKILLS
    assert any("cap" in w for w in caps.warnings)


def test_skills_index_load_skill_by_default_no_path():
    caps = cc.discover_cell_capabilities({
        "a/SKILL.md": "---\nname: alpha\ndescription: Does A.\n---\nb",
        "b/SKILL.md": "b only",
    }, {})
    idx = cc.skills_index(caps.skills)
    assert "alpha — Does A." in idx
    assert "load_skill" in idx           # in-process surfaces read via the tool
    assert "SKILL.md" not in idx         # no file-path pointer without a root
    assert "  * b" in idx
    assert cc.skills_index([]) == ""


def test_skills_index_with_root_points_at_the_files():
    caps = cc.discover_cell_capabilities({
        "a/SKILL.md": "---\nname: alpha\ndescription: Does A.\n---\nb",
    }, {})
    root = ".collrun/config/folders/_skills"
    idx = cc.skills_index(caps.skills, root=root)
    assert f"[{root}/a/SKILL.md]" in idx
    assert "alpha — Does A." in idx
    assert "load_skill" not in idx
