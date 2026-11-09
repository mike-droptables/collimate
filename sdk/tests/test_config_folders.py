# SPDX-License-Identifier: MIT
"""``gen.config_folder`` / ``config_folder_path`` / ``config_folders`` — the
read surface over runner-provided config folders (skills / mcp / scripts) the
host stages into ``.collrun/config/folders/<name>/…`` BEFORE input
hydration. Two layers: the ``HostBridge`` read methods directly, and the
``run_generator`` harness kwarg + the guarantee that folder content never leaks
into ``gen.list_files()``."""
from __future__ import annotations

import os

from collimate.testing import run_generator_callable


# ---------------------------------------------------------------------------
# HostBridge read methods (against a seeded workspace)
# ---------------------------------------------------------------------------


def _seed(ws, name, files):
    base = os.path.join(ws, ".collrun", "config", "folders", name)
    for rel, content in files.items():
        full = os.path.join(base, rel)
        os.makedirs(os.path.dirname(full) or base, exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as fh:
            fh.write(content)


def test_bridge_reads_folder_content(tmp_path):
    from collimate.testing import make_test_bridge

    ws = str(tmp_path)
    _seed(ws, "_skills", {
        "brainstorm/SKILL.md": "# brainstorm",
        "brainstorm/helper.py": "x = 1\n",
        "logo.png": b"\x89PNG\x00",
    })
    bridge = make_test_bridge(ws)
    try:
        folder = bridge.config_folder("_skills")
        # Relpaths are relative to the folder root; utf-8 -> str, else bytes.
        assert folder["brainstorm/SKILL.md"] == "# brainstorm"
        assert folder["brainstorm/helper.py"] == "x = 1\n"
        assert folder["logo.png"] == b"\x89PNG\x00"
        # Absolute path points at the staged dir whether or not it exists.
        assert bridge.config_folder_path("_skills").endswith(
            os.path.join(".collrun", "config", "folders", "_skills"))
        # config_folders lists staged folder names.
        assert bridge.config_folders() == ["_skills"]
        # A folder that was not provided reads empty.
        assert bridge.config_folder("_mcp") == {}
    finally:
        bridge.close()


def test_bridge_no_folders_is_empty(tmp_path):
    from collimate.testing import make_test_bridge

    bridge = make_test_bridge(str(tmp_path))
    try:
        assert bridge.config_folders() == []
        assert bridge.config_folder("_skills") == {}
    finally:
        bridge.close()


# ---------------------------------------------------------------------------
# Harness kwarg + non-leakage into list_files
# ---------------------------------------------------------------------------


def test_config_folders_harness_kwarg_and_gen_surface():
    async def run():
        import gen
        skills = gen.config_folder("_skills")
        gen.update_file("skills.json", __import__("json").dumps(
            {k: (v if isinstance(v, str) else "<bytes>") for k, v in sorted(skills.items())}))
        gen.update_file("names.txt", ",".join(gen.config_folders()))
        gen.update_file("path.txt", gen.config_folder_path("_skills"))

    res = run_generator_callable(run, config_folders={
        "_skills": {"a/SKILL.md": "hello", "b.bin": b"\x89PNG\xff\xfe"},
        "_mcp": {"servers.json": "{}"},
    })
    assert res.ok, res.error
    import json
    skills = json.loads(res.files["skills.json"])
    assert skills == {"a/SKILL.md": "hello", "b.bin": "<bytes>"}
    assert set(res.files["names.txt"].split(",")) == {"_skills", "_mcp"}
    assert res.files["path.txt"].endswith(
        os.path.join(".collrun", "config", "folders", "_skills"))


def test_config_folders_do_not_leak_into_list_files():
    async def run():
        import gen
        inp = gen.list_files()
        # The staged folders live under .collrun and must be invisible to
        # the input surface — only the real input file shows up.
        gen.update_file("inputs.txt", ",".join(sorted(inp)))
        gen.update_file("has_folder_file.txt",
                        "yes" if any("_skills" in p or "SKILL" in p for p in inp) else "no")

    res = run_generator_callable(
        run,
        inputs={"doc.md": "the input"},
        config_folders={"_skills": {"brainstorm/SKILL.md": "# s"}},
    )
    assert res.ok, res.error
    assert res.files["inputs.txt"] == "doc.md"
    assert res.files["has_folder_file.txt"] == "no"
