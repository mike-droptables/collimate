# SPDX-License-Identifier: MIT
"""Declared SDK development dependencies must never silently become skips."""
import importlib
from pathlib import Path
import re
import tomllib


_IMPORTS = {
    'pytest': ('pytest',),
    'ruff': ('ruff',),
    'build': ('build',),
    'agent-client-protocol': ('acp',),
    'mcp': ('mcp',),
    'uvicorn': ('uvicorn',),
    'fastmcp': ('fastmcp',),
    'trafilatura': ('trafilatura',),
    'ddgs': ('ddgs',),
    'httpx': ('httpx',),
    'httpx2': ('httpx2',),
    'docker': ('docker',),
    'aiohttp': ('aiohttp',),
}


def test_every_declared_dev_dependency_imports_without_skipping__F1932():
    groups = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())['dependency-groups']
    declared = {re.match(r'[A-Za-z0-9_-]+', requirement).group().lower().replace('_', '-')
                for requirement in groups['dev']}
    assert declared == _IMPORTS.keys(), 'Every declared development distribution needs an import probe'
    for distribution, modules in _IMPORTS.items():
        for name in modules:
            module = importlib.import_module(name)
            assert module is not None, f'{distribution}: {name}'
