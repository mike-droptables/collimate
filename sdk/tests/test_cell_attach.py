# SPDX-License-Identifier: MIT
"""The surviving ``_agent_shared.cell_attach`` helpers — keystore-secret
resolution, the container localhost rewrite, and the container skills-root
helper that sources from ``gen.config_folder_path``. (The five per-harness
MCP config translators died with the ACP rework: declarations now ride
``session/new.mcpServers`` — see ``acp_client.build_mcp_servers``.)"""
from __future__ import annotations



import gen  # noqa: E402
from _agent_shared import cell_attach as ca  # noqa: E402
from _agent_shared.cell_caps import McpDecl  # noqa: E402

_KEYS = {"notion": "ntn-secret", "openai": "sk-x"}


def _fetch(p):
    return _KEYS.get(p)


def test_container_localhost_rewrite():
    assert ca.rewrite_for_container("http://localhost:8080/mcp") == \
        "http://host.docker.internal:8080/mcp"
    assert ca.rewrite_for_container("http://127.0.0.1/x") == \
        "http://host.docker.internal/x"
    assert ca.rewrite_for_container("https://notion.com/mcp") == "https://notion.com/mcp"
    # a hostname merely CONTAINING localhost is untouched (the parsed host)
    assert ca.rewrite_for_container("https://localhostx.com/") == "https://localhostx.com/"
    # port, query and fragment survive; the host is matched case-insensitively
    assert ca.rewrite_for_container("http://LOCALHOST:9/mcp?x=1#f") == \
        "http://host.docker.internal:9/mcp?x=1#f"


def test_loopback_check_and_rewrite_agree_on_userinfo__F1319():
    """F1319: a URL with userinfo is never a loopback URL, whatever its
    userinfo spells — ``localhost:x@169.254.169.254`` is the metadata host.
    The exemption check and the rewrite parse it the same way, so neither
    exempts nor rewrites it."""
    for url in ("http://localhost:x@169.254.169.254/latest/meta-data/",
                "http://localhost@evil.test/", "http://u:p@localhost:8080/mcp",
                "http://localhost:99999/", "ftp://localhost/x"):
        assert not ca.is_container_loopback(url), url
        assert ca.rewrite_for_container(url) == url, url
    for url in ("http://localhost:8080/mcp", "https://127.0.0.1/x", "http://localhost"):
        assert ca.is_container_loopback(url), url


def test_resolve_secrets_drops_unresolved():
    assert ca.resolve_secrets({"A": "${account:notion}"}, _fetch) == {"A": "ntn-secret"}
    assert ca.resolve_secrets({"A": "${account:missing}"}, _fetch) == {}


def test_container_skills_root_is_workspace_relative(monkeypatch):
    """The terminal/CLI skill index points its file tools at the config folder
    as it appears inside the /workspace bind mount — the workspace-relative
    path derived from gen.config_folder_path (staged under .collrun)."""
    monkeypatch.setattr(gen, "workspace_path", lambda: "/run/ws", raising=False)
    monkeypatch.setattr(
        gen, "config_folder_path",
        lambda name: f"/run/ws/.collrun/config/folders/{name}",
        raising=False,
    )
    assert ca.container_skills_root() == ".collrun/config/folders/_skills"


def test_container_skills_root_degrades_without_bridge(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no bridge")
    monkeypatch.setattr(gen, "config_folder_path", _boom, raising=False)
    assert ca.container_skills_root() == ""


def test_resolve_secrets_passes_literals_through():
    # Pre-resolved decls carry literal values (not refs) — those pass through;
    # empty drops; a still-ref value resolves only when a fetch_key is given.
    assert ca.resolve_secrets({"A": "sk-literal", "B": ""}, None) == {"A": "sk-literal"}
    assert ca.resolve_secrets({"A": "${account:notion}"}, None) == {}   # no fetch
    assert ca.resolve_secrets({"A": "${account:notion}"}, _fetch) == {"A": "ntn-secret"}


def test_resolve_decl_secrets_awaits_once_into_literals():
    import asyncio
    calls = []

    async def _api_key(provider):
        calls.append(provider)
        return _KEYS.get(provider)

    decls = [
        McpDecl(name="n", transport="http", url="https://x/mcp",
                headers={"Authorization": "${account:notion}", "X": "${account:notion}"}),
        McpDecl(name="s", transport="stdio", command=["srv"],
                env={"TOK": "${account:openai}", "MISSING": "${account:ghost}"}),
    ]
    out = asyncio.run(ca.resolve_decl_secrets(decls, _api_key))
    # Two refs to 'notion' → awaited ONCE (cache); ghost → dropped.
    assert calls.count("notion") == 1
    assert out[0].headers == {"Authorization": "ntn-secret", "X": "ntn-secret"}
    assert out[1].env == {"TOK": "sk-x"}
    # Originals untouched (still refs) — the unit tests downstream rely on that.
    assert decls[0].headers["Authorization"] == "${account:notion}"
    # A downstream pass over the resolved decl sees the literal (no re-fetch).
    assert ca.resolve_secrets(out[0].headers, None) == {
        "Authorization": "ntn-secret", "X": "ntn-secret"}
