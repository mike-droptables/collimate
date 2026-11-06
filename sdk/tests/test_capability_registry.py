# SPDX-License-Identifier: MIT
"""Drift guard for the ONE workspace/web tool registry.

The MCP entries (the bridge, the in-memory FastMCP twin and the claude SDK
servers) derive their membership from ``tool_entries``; every row names a valid
gate and a callable implementation, and ``view_image`` is the MCP Image shape
every bridged client renders.
"""
from __future__ import annotations



import _agent_shared as eng  # noqa: E402
from _agent_shared import workspace_tools  # noqa: E402


def _row_names(**axes):
    return [r[0] for r in workspace_tools.tool_entries(**{
        "enable_file_tools": True, "websearch": True, "annotation_tools": True, **axes})]


def test_every_enabled_file_tool_is_in_the_registry_P4F26():
    """Native harness capability differences cannot remove generic writers."""
    full = set(_row_names())
    assert {"read_file", "write_file", "view_image", "web_fetch", "annotate"} <= full
    assert set(_row_names(enable_file_tools=False, websearch=False, annotation_tools=False)) == {
        "list_handlers", "handler_skills", "validate_file"}


def test_every_row_is_well_formed():
    """Workspace tool rows name valid gates and callable implementations."""
    from _agent_shared.generator_test import CommandContext
    for row in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=True,
        commands='allow', command_context=CommandContext('host'), annotation_tools=True,
    ):
        name, gate, main_loop, twin, fn = row
        assert name and callable(fn) and gate in (
            "handler", "file", "web", "annotation", "command")
        # Only the rows that await a host op run on the generator loop.
        assert main_loop is (name in ('annotate', 'list_annotations', 'test_generator'))
        assert workspace_tools.tool_description(fn, twin).strip()


def test_view_image_is_the_mcp_image_shape():
    by_name = {r[0]: r[4] for r in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=False)}
    assert by_name["view_image"] is workspace_tools.view_image
    assert not hasattr(workspace_tools, "view_image_mcp")
    assert not hasattr(workspace_tools, "TOOL_RENDERINGS")


# ── cell skills rows + MCP SSRF filter ─────────────────────────────────


def test_skill_rows_ride_the_registry_when_turn_has_skills():
    from _agent_shared.cell_caps import Skill
    sk = [Skill(name="a", description="A", body="do a", dir="_skills/a")]
    # No skills → no skill rows; handler tools remain always on.
    assert "list_skills" not in [r[0] for r in workspace_tools.tool_entries(
        enable_file_tools=False, websearch=False, skills=[])]
    # Skills present → list_skills + load_skill, even a text-only (no file
    # tools) turn.
    names = [r[0] for r in workspace_tools.tool_entries(
        enable_file_tools=False, websearch=False, skills=sk)]
    assert names == ["list_handlers", "handler_skills", "validate_file",
                     "list_skills", "load_skill"]


def test_skill_tool_entries_close_over_the_turns_skills():
    import asyncio
    from _agent_shared.cell_caps import Skill
    rows = workspace_tools.skill_tool_entries([Skill(
        name="pirate", description="Arr", body="talk like a pirate",
        dir="_skills/pirate")])
    impls = {name: fn for name, _g, _ml, _t, fn in rows}
    idx = asyncio.run(impls["list_skills"]())
    # The in-process list points at the load_skill tool (skills are carried
    # in-process, not on disk), not a workspace file path.
    assert "pirate — Arr" in idx and "load_skill" in idx
    body = asyncio.run(impls["load_skill"]("pirate"))
    assert "talk like a pirate" in body
    assert "ERROR" in asyncio.run(impls["load_skill"]("ghost"))
    # Skill membership remains per-turn with no module stash; the independent
    # handler group is present even without skills or optional workspace tools.
    empty_impls = {row[0]: row[4] for row in workspace_tools.skill_tool_entries([])}
    assert asyncio.run(empty_impls["list_skills"]()) == "No skills provided for this run."
    assert "available: none" in asyncio.run(empty_impls["load_skill"]("pirate"))
    assert [row[0] for row in workspace_tools.tool_entries(
        enable_file_tools=False, websearch=False, skills=[])] == [
            "list_handlers", "handler_skills", "validate_file"]


def test_mcp_ssrf_filter_blocks_untrusted_localhost_off_container():
    from _agent_shared.cell_caps import McpDecl
    # A public IP literal — no DNS needed (offline-safe), not private → allowed.
    remote = McpDecl(name="ok", transport="http", url="https://93.184.216.34/x")
    local = McpDecl(name="loop", transport="http", url="http://127.0.0.1:9/x")
    stdio = McpDecl(name="cmd", transport="stdio", command=["x"])
    # In-process backend: loopback http is blocked (SSRF), remote + stdio pass.
    kept, warns = eng.filter_mcp_decls([remote, local, stdio], containerized=False)
    assert [d.name for d in kept] == ["ok", "cmd"]
    assert any("loop" in w for w in warns)
    # Containerized harness: loopback is allowed (rewritten to host.docker.internal).
    kept, warns = eng.filter_mcp_decls([remote, local, stdio], containerized=True)
    assert [d.name for d in kept] == ["ok", "loop", "cmd"] and warns == []


def test_mcp_loopback_exemption_refuses_a_userinfo_url__F1319():
    """F1319: ``http://localhost:x@169.254.169.254/`` names the metadata
    address (``localhost:x`` is userinfo). The container loopback exemption is
    a parsed check, so the URL is not exempt: it meets the SSRF gate, which
    refuses it — it never reaches the container rewrite."""
    from _agent_shared.cell_caps import McpDecl

    sneaky = McpDecl(name="meta", transport="http",
                     url="http://localhost:x@169.254.169.254/latest/meta-data/")
    creds = McpDecl(name="creds", transport="http", url="http://u:p@127.0.0.1:9/mcp")
    kept, warns = eng.filter_mcp_decls([sneaky, creds], containerized=True)
    assert kept == []
    assert any("'meta'" in w and "169.254.169.254" in w for w in warns)
    assert any("'creds'" in w for w in warns)


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


def _public_dns(monkeypatch, *names):
    """Resolve ``names`` to a public address, offline, with no proxy."""
    import socket

    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    real = socket.getaddrinfo

    def gai(host, port, *a, **k):
        if host in names:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", gai)


def test_mcp_filter_refuses_a_url_parsers_read_differently__F1319(monkeypatch):
    """F1319: the gate reads a URL with urlsplit, but a container harness
    reads it the WHATWG way (node's URL, Rust's url crate), where a backslash
    ends the authority: ``http://169.254.169.254\\@example.com/`` is the
    metadata host to the harness and example.com to urlsplit. Such a URL is
    refused before the gate judges it, in a container or not, even though
    the name urlsplit sees is public. So are a percent-encoded or non-ASCII
    host, a port that does not parse, and a number WHATWG reads as an IPv4
    address its canonical form would not spell; and credentials before the
    host, which a declaration carries in its headers instead."""
    from _agent_shared.cell_caps import McpDecl

    _public_dns(monkeypatch, "example.com")
    refused = [
        "http://169.254.169.254\\@example.com/latest/meta-data/",
        "http://host.docker.internal:6379\\@example.com/mcp",
        "http://localhost\\@example.com/mcp",
        "http://example.com\t@169.254.169.254/",
        "http://example.com /mcp",
        "http://user@example.com/mcp",
        "https://user:pw@example.com/mcp",
        "http://%31%36%39.254.169.254/",
        "http://ｅxample.com/mcp",
        "http://example.com:80:81/mcp",
        "http://0x7f.1/mcp",
        "http://2130706433/mcp",
        "http://0251.0376.0251.0376/",
        "http://169.254.169.254./",
    ]
    decls = [McpDecl(name=f"d{i}", transport="http", url=u)
             for i, u in enumerate(refused)]
    for containerized in (True, False):
        kept, warns = eng.filter_mcp_decls(decls, containerized=containerized)
        assert kept == [], [d.url for d in kept]
        assert len(warns) == len(refused)
    (warn,) = eng.filter_mcp_decls(decls[:1], containerized=True)[1]
    assert "backslash" in warn
    # What every parser reads alike still passes.
    fine = [McpDecl(name="name", transport="http", url="https://example.com:8443/mcp"),
            McpDecl(name="ip", transport="http", url="http://93.184.216.34/mcp"),
            McpDecl(name="v6", transport="http",
                    url="http://[2606:2800:220:1:248:1893:25c8:1946]/mcp")]
    kept, warns = eng.filter_mcp_decls(fine, containerized=True)
    assert [d.name for d in kept] == ["name", "ip", "v6"] and warns == []


def test_a_host_twins_loopback_decl_meets_the_gate__F1319(native_bridge, monkeypatch):
    """F1319: the loopback exemption follows where the leg runs. Under
    ``docker: none`` codex runs as a host twin, and the ACP mapper leaves
    its URLs as declared, so a cell's ``http://127.0.0.1:<port>/`` would
    reach the host's own loopback: it meets the SSRF gate and is refused. In
    its container the same decl is kept (the mapper rewrites it to
    host.docker.internal)."""
    import asyncio

    import _agent_shared.acp_run as acp_run
    from _agent_shared.cell_caps import CellCaps, McpDecl

    caps = CellCaps(mcp=[McpDecl(name="redis", transport="http",
                                 url="http://127.0.0.1:6379/mcp")])
    monkeypatch.setattr(eng, "discover_cell_caps", lambda: caps)
    logs: list = []
    monkeypatch.setattr(eng.gen, "log", logs.append)
    seen: dict = {}

    async def fake_acp(**kw):
        seen[kw.get("runtime")] = [d.url for d in kw.get("mcp_decls") or []]
        return "ok", [], {}

    monkeypatch.setattr(acp_run, "run_acp_turn", fake_acp)
    for placement in ("standard", "docker"):
        native_bridge.permissions = {"docker": "none" if placement == "standard" else "full", "internet": "full"}
        asyncio.run(eng.run_agent(prompt="hi", backend="codex", provider="openai",
                                  cell_mcp="http", runtime=placement,
                                  host_harness={"path": "/fixture/codex"} if placement == "standard" else None))
    assert seen == {"standard": [], "docker": ["http://127.0.0.1:6379/mcp"]}
    assert any("'redis' blocked" in m and "127.0.0.1" in m for m in logs)
