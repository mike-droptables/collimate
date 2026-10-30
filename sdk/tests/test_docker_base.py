# SPDX-License-Identifier: MIT
"""The shared Docker base (``collimate-linux-small``) has a single canonical
Dockerfile under ``_env_shared/base``. Every Docker-family cell (the ``run``
generator's modes and the agent-family tool images) builds the base from this
one context on first use — there is no second copy to drift against anymore
(the old ``linux_env`` canonical was folded into ``launch`` and deleted). This
pins the base's identity so an accidental gutting fails CI, plus the derivation
that layers the agent-family images on it: their tags are hashes of their build
contexts (``cli_container.image_spec``), so a Dockerfile edit is a tag the host
has never built — the only thing that makes the edit reach an existing install.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from tests._workspace import GENERATORS


_GEN_DIR = str(GENERATORS)

_BASE = os.path.join(_GEN_DIR, "_env_shared", "base", "Dockerfile")

sys.path.insert(0, _GEN_DIR)


def test_base_dockerfile_exists():
    assert os.path.isfile(_BASE), "the canonical base Dockerfile is missing"


def test_base_dockerfile_is_the_v3_base():
    # A light sanity anchor so the base can't be silently gutted: the shared
    # base is Debian-derived, carries python + node, and stays alive as PID 1.
    text = open(_BASE, "r", encoding="utf-8").read()
    assert "FROM debian:trixie-slim" in text
    assert "nodejs" in text
    assert 'CMD ["sleep", "infinity"]' in text


def test_base_does_not_pipe_a_download_into_a_shell():
    # RUN has no pipefail: `curl ... | bash` turns a failed download into an
    # empty script that exits 0, and the base is then cached without npm.
    text = open(_BASE, "r", encoding="utf-8").read()
    assert "| bash" not in text and "| sh" not in text


def test_no_image_pipes_a_download_into_a_shell():
    # The same rule for every harness image: a pipe into bash/sh (even with env
    # assignments before the shell, `| X=1 bash`) caches a failed download.
    import glob
    import re

    pipe = re.compile(r"\|\s*(?:\w+=\S*\s+)*(?:ba)?sh\b")
    paths = glob.glob(os.path.join(_GEN_DIR, "_agent_shared", "harnesses", "*", "image", "Dockerfile"))
    assert paths
    for path in paths + [_BASE]:
        text = open(path, "r", encoding="utf-8").read()
        assert not pipe.search(text), path


def test_goose_is_root_owned_and_version_probed():
    # Root-owned under /usr/local/bin like agy, so `goose update` cannot replace
    # the pinned build; the build proves the pinned version landed.
    path = os.path.join(_GEN_DIR, "_agent_shared", "harnesses", "goose", "image", "Dockerfile")
    text = open(path, "r", encoding="utf-8").read()
    assert "GOOSE_BIN_DIR=/usr/local/bin" in text
    assert ".local/bin" not in text
    assert "goose --version" in text


def test_layered_dockerfiles_take_the_base_as_a_build_arg():
    # Every layered image must resolve its FROM through ${BASE}: the builder
    # injects the base tag it actually ensured, and the literal default keeps a
    # hand-run `docker build` working.
    for folder in ("codex", "antigravity", "goose", "opencode", "qwen_code",
                   "cline", "pi", "hermes", "vibe", "copilot"):
        path = os.path.join(_GEN_DIR, "_agent_shared", "harnesses", folder, "image", "Dockerfile")
        text = open(path, "r", encoding="utf-8").read()
        assert "ARG BASE=collimate-linux-small:" in text, folder
        assert "FROM ${BASE}" in text, folder


def _offline_run(monkeypatch):
    """image_spec reads the running run's package requests and floats each
    artifact through the upstream resolver: keep that resolver offline (it
    resolves nothing, so every artifact keeps its Dockerfile default)."""
    import _env_shared.upstream as upstream
    monkeypatch.setattr(upstream, "resolve_requests",
                        lambda artifacts, requests, **kw: {})


def test_image_spec_is_content_addressed_and_folds_in_the_base(host_free_run, monkeypatch):
    from _agent_shared.cli_container import image_spec
    from _env_shared import context_digest, derive_image_tag

    _offline_run(monkeypatch)

    spec = image_spec("goose", "goose")
    repo, _, digest = spec["image"].partition(":")
    assert repo == "collimate-goose"
    assert len(digest) == 12
    assert spec["build_context"].endswith(os.path.join("_agent_shared", "harnesses", "goose", "image"))
    assert spec["base_context"].endswith(os.path.join(".collrun", "container-base"))
    with open(os.path.join(spec["base_context"], "Dockerfile"), encoding="utf-8") as staged:
        with open(_BASE, encoding="utf-8") as canonical:
            assert staged.read() == canonical.read()
    # The base is content-addressed too, and rides along as the BASE build arg.
    assert spec["base_image"].startswith("collimate-linux-small:")
    assert spec["base_image"] != "collimate-linux-small:v3"
    assert spec["build_args"] == {"BASE": spec["base_image"]}
    # The base's digest AND the build args are folded into the layered tag:
    # rebuilding the base re-tags everything above it, and a runner asking for a
    # different artifact version re-tags too (the version lives in the args,
    # not in the context bytes — without this, two requested versions would
    # collide on one tag and the second would silently get the first's image).
    assert spec["image"] != derive_image_tag("goose", spec["build_context"])
    assert spec["image"] == derive_image_tag(
        "goose", spec["build_context"],
        base_digest=context_digest(spec["base_context"]),
        build_args=spec["build_args"],
    )
    bumped = derive_image_tag(
        "goose", spec["build_context"],
        base_digest=context_digest(spec["base_context"]),
        build_args={**spec["build_args"], "GOOSE_VERSION": "9.9.9"},
    )
    assert bumped != spec["image"]


def test_context_digest_ignores_python_bytecode_caches(tmp_path):
    # Tests import build-context scripts by path, leaving __pycache__ behind;
    # bytecode must not change the tag (a re-tag is a full image rebuild).
    from _env_shared import context_digest

    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    before = context_digest(str(tmp_path))
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00pyc")
    (tmp_path / "y.pyc").write_bytes(b"\x00pyc")
    assert context_digest(str(tmp_path)) == before
    (tmp_path / "Dockerfile").write_text("FROM scratch\nRUN true\n")
    assert context_digest(str(tmp_path)) != before


def test_image_spec_gives_each_backend_its_own_stable_tag(host_free_run, monkeypatch):
    from _agent_shared.cli_container import image_spec

    _offline_run(monkeypatch)

    names = ("qwen-code", "codex", "antigravity", "goose", "opencode")
    tags = {n: image_spec(n, n)["image"] for n in names}
    assert len(set(tags.values())) == len(names), tags
    # Stable across calls — the tag is a cache key, not a nonce.
    assert tags == {n: image_spec(n, n)["image"] for n in names}


def _capture_requests(monkeypatch, packages, grant):
    """What _requested_versions hands the upstream resolver for a run whose
    runner names ``packages`` under ``grant`` (no registry is reached)."""
    import gen
    from _agent_shared import cli_container
    from _env_shared import git_account
    import _env_shared.upstream as upstream

    seen: dict = {}

    def _resolve(artifacts, requests, *, allow_network, log=None, github_token=None):
        seen.update(requests=dict(requests), allow_network=allow_network, artifacts=set(artifacts),
                    github_token=github_token)
        return {}

    monkeypatch.setattr(gen, "packages", lambda: packages)
    from collimate.permissions import Grant
    monkeypatch.setattr(gen, "grant", lambda: Grant.from_dict(grant))
    monkeypatch.setattr(upstream, "resolve_requests", _resolve)
    monkeypatch.setattr(git_account, "release_token", lambda log=None: None)
    return cli_container, seen


def test_a_docker_harness_floats_every_artifact_to_latest_by_default(monkeypatch):
    # A runner that names no packages still runs the latest harness: every
    # artifact the image installs is requested as "latest".
    cc, seen = _capture_requests(monkeypatch, {}, {})
    cc._requested_versions("codex")
    # The harness and its adapter float; the in-image MCP proxy (Collimate's
    # own plumbing) keeps its Dockerfile pin unless a runner names it.
    assert seen["requests"] == {"codex": "latest", "codex-acp": "latest"}
    assert seen["allow_network"] is True
    cc._requested_versions("antigravity")
    assert seen["requests"] == {"agy": "latest"}


def test_a_runner_pin_wins_and_update_packages_off_keeps_the_network_closed(monkeypatch):
    cc, seen = _capture_requests(monkeypatch, {"codex": "0.150.0"}, {"docker": "full", "update_packages": False})
    cc._requested_versions("codex")
    assert seen["requests"]["codex"] == "0.150.0"
    assert seen["requests"]["codex-acp"] == "latest"
    assert seen["allow_network"] is False
    cc, seen = _capture_requests(monkeypatch, {"mcp-remote": "latest"}, {})
    cc._requested_versions("goose")
    assert seen["requests"] == {"goose": "latest", "mcp-remote": "latest"}


def test_a_host_adapter_name_is_not_an_unknown_package_for_an_image(monkeypatch):
    # A runner may name claude-agent-acp (the claude host chat leg's
    # adapter); no image installs it, and it must not be logged as unknown.
    cc, seen = _capture_requests(monkeypatch, {"claude-agent-acp": "latest"}, {})
    cc._requested_versions("goose")
    assert seen["requests"] == {"goose": "latest"}
    # A genuinely unknown name still reaches the resolver (which logs it).
    cc2, seen2 = _capture_requests(monkeypatch, {"not-a-package": "latest"}, {})
    cc2._requested_versions("goose")
    assert "not-a-package" in seen2["requests"]


def test_shared_base_trixie_and_managed_pip__F1873_F1967():
    text = Path(_BASE).read_text()
    assert "FROM debian:trixie-slim" in text
    assert "uv venv --seed --python 3.13 /opt/collimate-python" in text


def test_context_digest_frames_paths_and_content__F1666(tmp_path):
    from _env_shared import context_digest
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a").write_bytes(b"bc")
    (right / "ab").write_bytes(b"c")
    assert context_digest(str(left)) != context_digest(str(right))
    before = context_digest(str(left))
    assert before == context_digest(str(left))
    (left / "a").write_bytes(b"bd")
    assert before != context_digest(str(left))


def test_vscode_uses_resolvable_base_and_checked_node24__F1950_F1951():
    text = Path(_GEN_DIR, "launch/launch/configs/vscode/files/build/Dockerfile").read_text()
    assert "ARG CODE_SERVER_VERSION=" in text
    assert "FROM codercom/code-server:${CODE_SERVER_VERSION}" in text
    assert "setup_24.x" in text and "setup_22.x" not in text
    assert "| bash" not in text
    assert "node --version | grep -q '^v24\\.'" in text


def test_context_digest_streams_large_files__F1666(tmp_path, monkeypatch):
    import builtins
    from _env_shared import context_digest
    (tmp_path / 'large').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    actual_open = builtins.open
    reads = []
    class Reader:
        def __init__(self, file):
            self.file = file
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def fileno(self):
            return self.file.fileno()
        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= 1024 * 1024
            return self.file.read(size)
    monkeypatch.setattr(builtins, 'open', lambda *args, **kwargs: Reader(actual_open(*args, **kwargs)))
    assert len(context_digest(str(tmp_path))) == 64
    assert len(reads) == 4
