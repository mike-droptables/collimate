# SPDX-License-Identifier: MIT
"""Goose preparation uses real policy builders and recorded process boundaries."""
import asyncio
import json
import os
import stat
from pathlib import Path


import pytest

from _agent_shared import acp_run, harness_homes
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses.goose import backend as goose
from tests._commands_matrix import recording_runtime  # noqa: F401


def context(**overrides):
    return {"auth": "api_key", "provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
            "system_prompt": "", "enable_file_tools": True, "websearch": True,
            "commands": "ask", "runtime": "docker", "host_harness": None, **overrides}


def test_selected_goose_key_uses_native_private_file_before_spawn(recording_runtime):  # noqa: F811
    profile = asyncio.run(acp_run._PROFILES["goose"](context()))
    asyncio.run(profile.spawn())
    assert "OPENROUTER_API_KEY" not in recording_runtime.spawn["env_vars"]
    home = harness_homes.harness_home("goose")
    path = os.path.join(home, "native-config", "secrets.yaml")
    with open(path, encoding="utf-8") as stream:
        assert json.load(stream) == {"OPENROUTER_API_KEY": "matrix-fake-key"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("listing", ['{}', '{"sessions":{}}', '{"sessions":false}', 'null', '42'])
def test_non_list_goose_store_is_unreadable_P0F25(listing):
    class Store:
        def exec(self, *args, **kwargs):
            return 0, listing
    with pytest.raises((ValueError, RuntimeError), match="session"):
        goose.harness_session_ids(Store())


@pytest.mark.parametrize("leg", ["acp", "terminal"])
def test_goose_preparation_policy_is_the_actual_spawn_policy(recording_runtime, leg):  # noqa: F811
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['goose'], context(), leg=leg))
    assert prepared.commands.level == "ask"
    assert prepared.commands.native["GOOSE_MODE"] == "approve"
    assert json.loads(prepared.commands.native["EXTENSIONS"])["developer"]["available_tools"] == [
        "write", "edit", "shell", "tree", "read", "read_image"]
    for key, value in prepared.commands.native.items():
        assert prepared.env[key] == value
    assert prepared.argv == (("goose", "acp") if leg == "acp" else ("goose", "session"))
    assert recording_runtime.files[goose.GOOSE_PERMISSION_PATH] == (
        'user:\n  always_allow: []\n  ask_before: ["shell"]\n  never_allow: []\n')


@pytest.mark.parametrize("placement,relative,leaf", [
    ("docker", "native-config", False),
    ("docker", "native-config/secrets.yaml", True),
    ("standard", "config", False),
    ("standard", "config/goose", False),
    ("standard", "config/goose/secrets.yaml", True),
    ("standard", "config/goose/permission.yaml", True),
    ("standard", "config/goose/.goosehints", True),
])
def test_repeated_goose_prepare_refuses_planted_native_paths(recording_runtime, tmp_path,  # noqa: F811
                                                            placement, relative, leaf):  # noqa: F811
    import shutil
    ctx = context(runtime=placement, system_prompt="original context",
                  host_harness={"path": "/fake/goose", "version": "1.52.0"})
    asyncio.run(_pipeline.prepare(_HARNESS['goose'], ctx))
    home = Path(harness_homes.harness_home("goose"))
    planted = home / relative
    outside = tmp_path / "unrelated"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    original = {p.name: p.read_bytes() for p in outside.iterdir()}
    if planted.is_dir():
        shutil.rmtree(planted)
    else:
        planted.unlink()
    planted.symlink_to(sentinel if leaf else outside, target_is_directory=not leaf)
    with pytest.raises((OSError, ValueError, PermissionError)):
        asyncio.run(_pipeline.prepare(_HARNESS['goose'], {**ctx, "system_prompt": "updated context"}))
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == original
    assert planted.is_symlink()
