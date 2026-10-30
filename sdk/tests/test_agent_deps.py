# SPDX-License-Identifier: MIT
"""The Agent generator's PEP 723 header is what the product installs; the SDK
dev environment must carry the same tool stack, or tests exercise a
different surface than users get (no workspace bridge without fastmcp's
server half, fallback web extractors without trafilatura / ddgs)."""
from __future__ import annotations

import os
import re
import tomllib

import pytest

from tests._workspace import GENERATORS


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(GENERATORS, "agent", "agent", "agent.py")
# Every shipped generator entrypoint with a PEP 723 header.
ENTRYPOINTS = (
    AGENT,
    os.path.join(GENERATORS, "git", "git", "git.py"),
    os.path.join(GENERATORS, "launch", "launch", "run.py"),
    os.path.join(GENERATORS, "web", "web", "web.py"),
)

# The header requirements the tests depend on seeing for real.
MIRRORED = ("mcp", "fastmcp", "trafilatura", "ddgs", "httpx2", "agent-client-protocol")


def _name(req: str) -> str:
    return re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0].strip().lower()


def _header(path: str = AGENT) -> dict:
    """The parsed ``# /// script`` block of a PEP 723 entrypoint."""
    src = open(path, encoding="utf-8").read()
    block = re.search(r"^# /// script\n(.*?)^# ///$", src, re.S | re.M).group(1)
    toml = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in block.splitlines())
    return tomllib.loads(toml)


def _header_deps() -> dict:
    return {_name(r): r for r in _header()["dependencies"]}


def _pyproject() -> dict:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def _dev_deps() -> dict:
    return {_name(r): r for r in _pyproject()["dependency-groups"]["dev"]}


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda p: os.path.relpath(p, ROOT))
def test_entrypoint_python_floor_matches_the_sdk(entrypoint):
    """A generator runs against the installed SDK, so its header must not
    admit a Python the SDK's own requires-python refuses. It may admit fewer:
    the Agent's ACP client (agent-client-protocol) caps Python below 3.15, so
    a uv that downloads 3.15 by default still picks a Python it can run."""
    specifiers = pytest.importorskip("packaging.specifiers")
    header = specifiers.SpecifierSet(_header(entrypoint)["requires-python"])
    sdk = specifiers.SpecifierSet(_pyproject()["project"]["requires-python"])
    probes = [f"3.{minor}.0" for minor in range(8, 20)]
    assert [v for v in probes if header.contains(v) and not sdk.contains(v)] == []
    assert min(v for v in probes if header.contains(v)) == min(v for v in probes if sdk.contains(v))


def test_dev_extras_mirror_the_agent_tool_stack():
    header, dev = _header_deps(), _dev_deps()
    for name in MIRRORED:
        assert name in header, f"{name} left the agent header"
        assert dev.get(name) == header[name], f"{name}: dev {dev.get(name)!r} != header {header[name]!r}"


def test_the_http_stack_floor_excludes_the_teardown_bug():
    """httpcore2 < 2.13 (pinned by httpx2 < 2.13) printed athrow tracebacks on
    every mid-reply Stop; the header must keep it out, and so must this env."""
    specifiers = pytest.importorskip("packaging.specifiers")
    version = pytest.importorskip("packaging.version")
    spec = specifiers.SpecifierSet(_header_deps()["httpx2"][len("httpx2"):])
    assert not spec.contains("2.12.0") and spec.contains("2.13.1")
    from importlib.metadata import version as installed
    assert version.Version(installed("httpcore2")) >= version.Version("2.13")


def test_fastmcp_server_support_is_installed():
    """pydantic-ai brings only fastmcp-slim, whose server half is missing —
    the MCP workspace bridge requires the full server package."""
    from fastmcp import FastMCP
    FastMCP("probe")
