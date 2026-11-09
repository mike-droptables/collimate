# SPDX-License-Identifier: MIT
"""The Git generator (content/generators/git): its manifest, settings,
presets and three runners, and its runs end to end against bare
repositories standing in for the remotes (``fake_remote``): Clone into a
folder, the root and an attached folder, Update in each divergence mode and
across a stopped merge, and Push to the upstream, a new branch and a
triangular remote, never forcing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from collimate import fs, git_hosts
from collimate.config_values import resolve_form_values
from collimate.lint import Level, lint_path
from collimate.testing import run_generator

# The engine's modules import before any run, so the fixtures' patches on
# them survive run_generator's module cleanup.
from _env_shared import git_account, git_run  # noqa: F401
from tests._git_fixtures import GH, GIT_OK, carry_account, git_account_snapshot, subcommand_of
from tests._workspace import GENERATORS

CELL = GENERATORS / "git"
PACKAGE = CELL / "git"
ENTRY = PACKAGE / "git.py"
MAIN = "settings.set_yaml"
ACCOUNT = {"git": git_account_snapshot()}
FULL = {"docker": "none", "internet": "full"}
FIELDS = ["action", "folder", "branch", "on_divergence", "base_commit", "commit_message", "push_branch"]
AUTHOR = "Ada Lovelace <ada@example.org>"
FORK = "https://github.com/me/api.git"
D = "github.com/octo/api"
_ED = git_hosts.PINNED_HOST_KEYS["github.com"][0].split(" ", 1)[1]

needs_git = pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")


@pytest.fixture(autouse=True)
def _commit_author(monkeypatch):
    """Without a bound account git asks who you are (D8): a run's commits
    here take the author a user's environment gives them."""
    for name, value in (("GIT_AUTHOR_NAME", "Repo User"), ("GIT_AUTHOR_EMAIL", "user@example.invalid"),
                        ("GIT_COMMITTER_NAME", "Repo User"), ("GIT_COMMITTER_EMAIL", "user@example.invalid")):
        monkeypatch.setenv(name, value)


@pytest.fixture
def cell(tmp_path) -> Path:
    workspace = tmp_path / "cell"
    workspace.mkdir()
    return workspace


def _yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _settings(**over) -> dict:
    values = {key: "" for key in FIELDS}
    values.update(action="clone", on_divergence="merge")
    values.update(over)
    return values


def _run(workspace: Path, prompt: str = "", *, accounts=ACCOUNT, permissions=FULL, **settings):
    return run_generator(ENTRY, prompt=prompt, config={MAIN: _settings(**settings)},
                         accounts=accounts, permissions=permissions, workspace=workspace)


def _update(workspace: Path, **settings):
    return _run(workspace, action="update", **settings)


def _push(workspace: Path, **settings):
    return _run(workspace, action="push", **settings)


def _failed(result, note: str) -> None:
    """A refusal: outcome fail, the note is the message and the run's error equals it."""
    assert not result.ok
    assert result.outcome == "fail", (result.error, result.traceback)
    assert result.note == note
    assert result.error == note


def _passed(result) -> str:
    assert result.ok, (result.error, result.traceback, result.logs)
    assert result.outcome == "pass"
    return result.note


def _git(top: Path, *args: str, check: bool = True) -> str:
    """Plain git in a cell's repository, as a person in a terminal runs it."""
    home = top.parent / ".git-home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C",
               GIT_AUTHOR_NAME="Cell User", GIT_AUTHOR_EMAIL="cell@example.invalid",
               GIT_COMMITTER_NAME="Cell User", GIT_COMMITTER_EMAIL="cell@example.invalid")
    done = subprocess.run(["git", *args], cwd=top, env=env, capture_output=True, text=True, timeout=60)
    if check and done.returncode:
        raise AssertionError(f"git {args[0]}: {done.stderr}")
    return done.stdout


def _status(top: Path) -> str:
    return _git(top, "status", "--porcelain", "--untracked-files=all")


def _cloned(cell: Path, fake_remote, files: dict, **settings) -> Path:
    fake_remote.create(GH, files)
    _passed(_run(cell, "octo/api", **settings))
    return cell / "api"


# ── shape ──────────────────────────────────────────────────────────────────


def test_manifest_shape():
    assert _yaml(CELL / "cell.yaml") == {"name": "Git", "order": 20, "icon": "git", "shared": ["_env_shared"]}
    manifest = _yaml(PACKAGE / "git.generator")
    assert manifest["id"] == "git" and manifest["name"] == "Git"
    assert manifest["entrypoint"] == "git.py"
    assert manifest["runs_on"] == "local-machine"
    assert manifest["live_files_mode"] == "auto"
    assert manifest["prompt_required"] is False
    assert manifest["prompt_show_if"] == {"action": "clone"}
    assert manifest["prompt_label"] == "Repository" and manifest["prompt_type"] == "text"
    assert manifest["main_config"] == MAIN and manifest["draft_files"] == [MAIN]
    for absent in ("requires_docker", "daemon", "ui_files", "opens", "env_passthrough", "takes_selected_file"):
        assert absent not in manifest, absent
    assert manifest["account_requirements"] == {"git": {
        "label": "Git account", "kind": "git", "capabilities": ["git"], "required": False}}
    assert manifest["configs"] == [
        {"id": action, "name": action.capitalize(), "path": f"configs/{action}/manifest.config_yaml"}
        for action in ("clone", "update", "push")]
    assert sorted(p.name for p in CELL.iterdir()) == ["cell.yaml", "git", "git-clone", "git-push", "git-update"]


def test_settings_fields_types_defaults_and_conditions():
    fields, _ = resolve_form_values(_yaml(PACKAGE / MAIN))
    by_key = {field["key"]: field for field in fields}
    assert [field["key"] for field in fields] == FIELDS
    assert [o["value"] for o in by_key["action"]["options"]] == ["clone", "update", "push"]
    assert by_key["action"]["default"] == "clone"
    assert [o["value"] for o in by_key["on_divergence"]["options"]] == ["merge", "rebase", "ff-only"]
    assert by_key["on_divergence"]["default"] == "merge"
    assert {key: by_key[key].get("show_if") for key in FIELDS} == {
        "action": None, "folder": None, "branch": {"action": "clone"}, "on_divergence": {"action": "update"},
        "base_commit": {"action": "clone"}, "commit_message": {"action": "push"},
        "push_branch": {"action": "push"}}
    assert {key for key in FIELDS if by_key[key].get("advanced")} == {"branch", "base_commit", "push_branch"}


@pytest.mark.parametrize("path, action, ident", [
    ("git/configs/clone/manifest.config_yaml", "clone", None),
    ("git/configs/update/manifest.config_yaml", "update", None),
    ("git/configs/push/manifest.config_yaml", "push", None),
    ("git-clone/git-clone.runner", "clone", ("git-clone", "Git Clone")),
    ("git-update/git-update.runner", "update", ("git-update", "Git Update")),
    ("git-push/git-push.runner", "push", ("git-push", "Git Push")),
])
def test_configs_and_runners_carry_exactly_the_settings(path, action, ident):
    data = _yaml(CELL / path)
    assert list(data["settings"]) == [MAIN]
    assert list(data["settings"][MAIN]) == FIELDS
    assert data["settings"][MAIN] == _settings(action=action)
    assert data["permissions"] == {"docker": "none", "internet": "full"}
    assert data["prompt"] == "" and data["folders"] == [] and data["accounts"] == {}
    assert data["tags"] == ["git"]
    if ident:
        assert (data["id"], data["name"]) == ident and data["generator"] == "git"


def test_lint_clean():
    issues = [issue for issue in lint_path(CELL) if issue.level in (Level.ERROR, Level.WARNING)]
    assert issues == []


def test_the_auto_mirror_wraps_the_async_run_and_never_carries_git(monkeypatch):
    """native.main wraps an async run() in gen.live_files() when the
    manifest says auto, and the default mirror skips every .git, the root's
    and a folder repository's alike."""
    import asyncio
    import types

    from collimate import native

    monkeypatch.setattr(native, "require_bridge", lambda: types.SimpleNamespace(folder="git-0123abcd"))
    mirror = native.live_files()
    assert mirror._skip_dirs == fs.WORKSPACE_SKIP_DIRS and ".git" in mirror._skip_dirs
    assert [mirror._rule(path) for path in (".git/HEAD", "api/.git/config", "libs/web/.git/index", "api/a.txt")] \
        == ["skip", "skip", "skip", "sync"]

    entered = []

    class Recorder:
        async def __aenter__(self):
            entered.append("in")

        async def __aexit__(self, *exc):
            entered.append("out")

    monkeypatch.setenv("COLLIMATE_LIVE_FILES_MODE", "auto")
    monkeypatch.setattr(native, "_has_host", lambda: True)
    monkeypatch.setattr(native, "live_files", lambda: Recorder())

    async def body():
        entered.append("run")

    asyncio.run(native._run_with_auto_live_files(body))
    assert entered == ["in", "run", "out"]


# ── refusals before any git ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _runner_modules():
    """The runner's own prompt.py imports from beside git.py, as the run imports it."""
    sys.path.insert(0, str(PACKAGE))
    try:
        yield
    finally:
        sys.path.remove(str(PACKAGE))
        sys.modules.pop("prompt", None)


def _prompt():
    import prompt
    return prompt


def test_blank_prompt_refuses_i1(cell, git_spy):
    _failed(_run(cell, "  \n"), _prompt().I1)
    assert git_spy.calls == []


def test_two_line_prompt_refuses_i4(cell, git_spy):
    _failed(_run(cell, "octo/api\nocto/web\n"), "Repository is one line naming one repository; it has 2 lines")
    assert git_spy.calls == []


def test_short_path_without_an_account_refuses_i1b(cell, git_spy):
    _failed(_run(cell, "octo/api", accounts=None), _prompt().I1b)
    assert git_spy.calls == []


@pytest.mark.parametrize("action", ["sync", "pull"])
def test_bad_action_refuses(cell, git_spy, action):
    _failed(_run(cell, "octo/api", action=action), "Action must be Clone, Update or Push")
    assert git_spy.calls == []


@pytest.mark.parametrize("action", ["clone", "update", "push"])
def test_internet_none_refuses_before_any_git(cell, git_spy, action):
    result = _run(cell, "octo/api", action=action, permissions={"docker": "none", "internet": "none"})
    _failed(result, "Git needs internet access; set Internet to full in this runner's permissions. "
                    "Nothing changed")
    assert git_spy.calls == []


def test_ssh_remote_needs_internet_full(cell, git_spy):
    box = {"git": git_account_snapshot("git", "ssh_key", metadata={"known_hosts": f"box.example.org {_ED}"})}
    result = _run(cell, "git@box.example.org:team/app.git", accounts=box,
                  permissions={"docker": "none", "internet": "proxy"})
    assert not result.ok and result.note.startswith("SSH remotes need Internet set to full")
    assert git_spy.calls == []


def test_an_address_the_account_cannot_serve_refuses_before_any_git(cell, git_spy):
    result = _run(cell, "https://gitlab.com/octo/api")
    assert not result.ok and result.note.startswith("The bound Git account is for github.com, not gitlab.com")
    _failed(_run(cell, "git@example.org:octo/api.git", accounts=None),
            "example.org:octo/api is an SSH address, and SSH needs a Git account with an SSH key bound to this "
            "runner. Nothing changed")
    assert git_spy.calls == []


@pytest.mark.parametrize("folder, message", [
    ("..", "I5"), ("../x", "I5"), ("/abs", "I5"), ("a/../b", "I5"), ("a\\b", "I5"),
    (".git", "I5b"), ("app/.GIT", "I5b"), ("git~1", "I5b"), ("_runs/x", "I5b"), (".collrun", "I5b"),
    ("node_modules", "I5b"), ("libs/.venv", "I5b"),
])
@pytest.mark.parametrize("action", ["clone", "update", "push"])
def test_folder_outside_the_repository_rule_refuses(cell, git_spy, folder, message, action):
    _failed(_run(cell, "octo/api", action=action, folder=folder), getattr(_prompt(), message))
    assert git_spy.calls == []


@pytest.mark.parametrize("value, normal", [(".", "."), ("./", "."), ("app", "app"), ("./libs/app/", "libs/app")])
def test_folder_takes_the_root_and_nested_paths(value, normal):
    assert _prompt().check_folder(value) == normal


def test_bad_branch_push_branch_and_base_commit_refuse(cell, git_spy):
    _failed(_run(cell, "octo/api", branch="bad..name"), _prompt().I6)
    _failed(_push(cell, push_branch="-x"), _prompt().I7)
    _failed(_run(cell, "octo/api", base_commit="HEAD~1"), "Base commit is a commit id: 4 to 64 hexadecimal digits")
    assert git_spy.calls == []


@pytest.mark.parametrize("snapshot, message", [
    (git_account_snapshot(kind="github"), git_account.G1),
    (git_account_snapshot(metadata={"author_email": None}), git_account.G2),
    (git_account_snapshot(secrets={}), git_account.G3.format(what="token")),
])
@pytest.mark.parametrize("action", ["clone", "update", "push"])
def test_broken_bound_account_refuses_with_key_manager_fix(cell, git_spy, snapshot, message, action):
    result = _run(cell, "https://github.com/octo/api", action=action, accounts={"git": snapshot})
    _failed(result, message)
    assert git_spy.calls == []


@pytest.mark.parametrize("prompt", ["https://u:ghp_leak@github.com/o/r", "https://oauth2:glpat-x@gitlab.com/g/p"])
def test_pasted_token_url_never_reaches_note_log_or_error(cell, git_spy, prompt):
    result = _run(cell, prompt)
    _failed(result, _prompt().I2)
    for text in (result.note, result.error, result.traceback or "", *result.logs):
        assert "ghp_leak" not in text and "glpat-x" not in text
    assert git_spy.calls == []


def test_git_missing_fails_with_install_message(tmp_path):
    """git missing from the run's PATH, in a fresh process."""
    workspace, empty = tmp_path / "cell", tmp_path / "no-git"
    workspace.mkdir()
    empty.mkdir()
    script = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(GENERATORS)!r})
        from collimate.testing import run_generator
        result = run_generator({str(ENTRY)!r}, prompt="https://github.com/octo/api",
                               config={{{MAIN!r}: {_settings()!r}}},
                               permissions={FULL!r}, workspace={str(workspace)!r})
        print(json.dumps({{"ok": result.ok, "note": result.note, "outcome": result.outcome,
                          "error": result.error}}))
    """)
    env = {**os.environ, "PATH": str(empty)}
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                          timeout=120, cwd=str(Path(__file__).resolve().parents[1]))
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    note = ("git is not installed on this machine (not on the run's PATH); install git 2.31 or later "
            "and run again")
    assert result == {"ok": False, "note": note, "outcome": "fail", "error": note}
    assert [name for name in os.listdir(workspace) if name != ".collrun"] == []


# ── Clone ──────────────────────────────────────────────────────────────────


@needs_git
def test_clone_into_a_folder_keeps_an_ordinary_git_with_the_real_address(cell, fake_remote):
    head = fake_remote.create(GH, {"README.md": "# api\n", "src/a.py": "a = 1\n", "run.sh": ("exec", "echo\n")})
    result = _run(cell, "https://github.com/octo/api", accounts=None)
    assert _passed(result) == f"Cloned {D} main into api/ at {head[:7]}"
    top = cell / "api"
    assert (top / "src" / "a.py").read_text() == "a = 1\n"
    assert os.access(top / "run.sh", os.X_OK)
    assert (top / ".git").is_dir()
    assert _status(top) == ""
    assert _git(top, "rev-parse", "HEAD").strip() == head
    assert _git(top, "config", "remote.origin.url").strip() == GH
    assert _git(top, "rev-parse", "--abbrev-ref", "main@{upstream}").strip() == "origin/main"
    assert _git(top, "log", "--format=%H").split() == [head]
    assert "Cloning into 'api'..." in result.logs


@needs_git
def test_clone_takes_a_branch_and_a_nested_folder(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "main\n"})
    dev = fake_remote.commit(GH, {"a.txt": "dev\n"}, branch="dev")
    note = _passed(_run(cell, "octo/api", folder="libs/api", branch="dev"))
    assert note == f"Cloned {D} dev into libs/api/ at {dev[:7]}"
    assert (cell / "libs" / "api" / "a.txt").read_text() == "dev\n"
    assert _git(cell / "libs" / "api", "symbolic-ref", "--short", "HEAD").strip() == "dev"


@needs_git
def test_clone_into_an_empty_folder_with_its_marker(cell, fake_remote):
    (cell / "api").mkdir()
    (cell / "api" / ".collkeep").write_bytes(b"")
    fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, "octo/api"))
    assert (cell / "api" / "a.txt").read_text() == "one\n" and _status(cell / "api") == ""


@needs_git
def test_clone_refuses_a_folder_with_a_repository_or_files(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _failed(_run(cell, "octo/api"),
            "api/ already holds a repository; run Git Update or Git Push on it, or set Folder to another folder")
    (cell / "other").mkdir()
    (cell / "other" / "notes.md").write_text("mine\n")
    result = _run(cell, "octo/api", folder="other")
    assert not result.ok and result.note.startswith("other/ holds files but no repository. To keep them as a "
                                                    f"checkout of {D}, set Base commit")
    assert "a JSON record left by the retired checkout handler" in result.note
    assert not (cell / "other" / ".git").exists() and _status(top) == ""


@needs_git
def test_clone_into_the_root_beside_the_hosts_bands(cell, fake_remote):
    head = fake_remote.create(GH, {"README.md": "# api\n", "src/a.py": "a = 1\n"})
    for band in (".collrun", "_runs/clone-0123abcd", "_agent"):
        (cell / band).mkdir(parents=True)
        (cell / band / "x.txt").write_text("host\n")
    (cell / ".collkeep").write_bytes(b"")
    result = _run(cell, "octo/api", folder=".")
    assert _passed(result) == f"Cloned {D} main into the cell root at {head[:7]}"
    assert (cell / ".git").is_dir() and (cell / "src" / "a.py").read_text() == "a = 1\n"
    assert (cell / ".collrun" / "x.txt").read_text() == "host\n"
    assert _status(cell) == ""
    exclude = (cell / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude[-5:] == ["/.collrun/", "/_runs/", "/_agent/", "/.pydantic-ai-harness/", ".collkeep"]
    assert _git(cell, "config", "remote.origin.url").strip() == GH
    assert _git(cell, "rev-parse", "--abbrev-ref", "main@{upstream}").strip() == "origin/main"
    assert _git(cell, "symbolic-ref", "refs/remotes/origin/HEAD").strip() == "refs/remotes/origin/main"


@needs_git
def test_root_clone_follows_the_remote_head_or_a_tag(cell, fake_remote, tmp_path):
    fake_remote.create(GH, {"a.txt": "one\n"}, branch="trunk")
    fake_remote.git(GH, "symbolic-ref", "HEAD", "refs/heads/trunk")
    tagged = fake_remote.head(GH, "trunk")
    fake_remote.git(GH, "tag", "v1", tagged)
    fake_remote.commit(GH, {"a.txt": "two\n"}, branch="trunk")
    _passed(_run(cell, "octo/api", folder="."))
    assert _git(cell, "symbolic-ref", "--short", "HEAD").strip() == "trunk"
    assert (cell / "a.txt").read_text() == "two\n"
    other = tmp_path / "tagged"
    other.mkdir()
    _passed(_run(other, "octo/api", folder=".", branch="v1"))
    assert _git(other, "rev-parse", "HEAD").strip() == tagged
    assert _git(other, "symbolic-ref", "-q", "HEAD", check=False) == ""


@needs_git
def test_root_clone_refuses_files_a_missing_branch_and_a_tree_holding_a_band(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "one\n", "_runs/x.txt": "theirs\n"})
    (cell / "notes.md").write_text("mine\n")
    result = _run(cell, "octo/api", folder=".")
    assert not result.ok and result.note.startswith("the cell root holds files but no repository")
    (cell / "notes.md").unlink()
    _failed(_run(cell, "octo/api", folder=".", branch="nope"), "Remote branch nope not found in upstream origin")
    assert not (cell / ".git").exists()
    _failed(_run(cell, "octo/api", folder="."),
            f"{D} main has a top-level _runs, which the cell keeps for itself; clone it into a folder instead")
    assert not (cell / ".git").exists() and not (cell / "a.txt").exists()


@needs_git
def test_clone_of_an_empty_remote(cell, fake_remote, tmp_path):
    path = fake_remote.path(GH)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True, capture_output=True)
    assert _passed(_run(cell, "octo/api")) == f"Cloned {D} into api/: it has no commits yet"
    assert (cell / "api" / ".git").is_dir()
    root = tmp_path / "root"
    root.mkdir()
    result = _run(root, "octo/api", folder=".")
    assert _passed(result) == f"Cloned {D} into the cell root: it has no commits yet"
    assert "You appear to have cloned an empty repository." in result.logs
    assert _git(root, "symbolic-ref", "HEAD").strip() == "refs/heads/main"


@needs_git
def test_attach_makes_files_a_checkout_of_the_base_commit(cell, fake_remote):
    base = fake_remote.create(GH, {"a.txt": "one\n", "b.txt": "b\n"})
    fake_remote.commit(GH, {"a.txt": "upstream\n"})
    top = cell / "api"
    top.mkdir()
    (top / "a.txt").write_text("one\n")
    (top / "b.txt").write_text("mine\n")
    result = _run(cell, "octo/api", base_commit=base[:12].upper())
    assert _passed(result) == f"Attached api/ to {D} main at {base[:7]}"
    assert _git(top, "rev-parse", "HEAD").strip() == base
    assert _status(top) == " M b.txt\n"
    assert (top / "b.txt").read_text() == "mine\n"
    assert _git(top, "rev-parse", "--abbrev-ref", "main@{upstream}").strip() == "origin/main"
    assert any(line.startswith("## main...origin/main [behind 1]") for line in result.logs)


@needs_git
def test_attach_takes_a_base_commit_named_like_one_of_the_files(cell, fake_remote):
    base = fake_remote.create(GH, {"a.txt": "one\n"})
    top = cell / "api"
    top.mkdir()
    (top / "a.txt").write_text("one\n")
    (top / base[:8]).write_text("a file named like the commit\n")
    _passed(_run(cell, "octo/api", base_commit=base[:8]))
    assert _git(top, "rev-parse", "HEAD").strip() == base
    assert _status(top) == f"?? {base[:8]}\n"


@needs_git
def test_attach_refuses_a_base_outside_the_history_and_leaves_no_git(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "one\n"})
    (cell / "api").mkdir()
    (cell / "api" / "a.txt").write_text("one\n")
    _failed(_run(cell, "octo/api", base_commit="0123abcd"), f"Base commit 0123abcd is not in {D}'s history")
    assert not (cell / "api" / ".git").exists()


@needs_git
def test_a_forge_ssh_accounts_clone_keeps_its_ssh_address(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, "octo/api", accounts={"git": git_account_snapshot("github", "ssh_key")}))
    assert _git(cell / "api", "config", "remote.origin.url").strip() == "ssh://git@github.com/octo/api.git"


@needs_git
def test_a_forge_ssh_account_updates_and_pushes_an_https_origin_over_ssh(cell, fake_remote, git_spy):
    """An origin cloned by its https address (without an account, or in a
    terminal) keeps that address; a forge SSH account reaches it over SSH
    through a command-line insteadOf, the address git reads rewritten."""
    fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, GH, accounts=None))
    top = cell / "api"
    ssh = {"git": git_account_snapshot("github", "ssh_key")}
    after = fake_remote.commit(GH, {"b.txt": "two\n"})
    git_spy.calls.clear()
    assert _passed(_update(cell, accounts=ssh)).endswith(f"→{after[:7]}")
    (top / "a.txt").write_text("mine\n")
    _passed(_push(cell, accounts=ssh, commit_message="mine"))
    assert fake_remote.head(GH) == _git(top, "rev-parse", "HEAD").strip()
    assert _git(top, "config", "remote.origin.url").strip() == GH
    network = [argv for argv, _env in git_spy.calls if subcommand_of(argv) in ("fetch", "pull", "push")]
    assert len(network) == 3
    assert all(f"url.{fake_remote.url(GH)}.insteadOf={GH}" in argv for argv in network)


# ── Update ─────────────────────────────────────────────────────────────────


@needs_git
def test_update_fast_forwards_and_says_when_it_is_current(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    before = _git(top, "rev-parse", "HEAD").strip()
    after = fake_remote.commit(GH, {"b.txt": "two\n", "a.txt": None})
    assert _passed(_update(cell)) == f"Updated api/ main from origin: {before[:7]}→{after[:7]}"
    assert (top / "b.txt").read_text() == "two\n" and not (top / "a.txt").exists()
    assert _passed(_update(cell, folder="api")) == f"api/ is up to date with origin/main at {after[:7]}"


@needs_git
def test_update_without_an_upstream_sets_it_from_the_remote_branch(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _git(top, "branch", "--unset-upstream")
    fake_remote.commit(GH, {"b.txt": "two\n"})
    _passed(_update(cell))
    assert _git(top, "rev-parse", "--abbrev-ref", "main@{upstream}").strip() == "origin/main"
    assert (top / "b.txt").exists()
    _git(top, "checkout", "-q", "-b", "local")
    _failed(_update(cell), "branch local has no upstream, and origin has no branch local; set one with git "
                           "branch --set-upstream-to")
    _git(top, "checkout", "-q", "--detach")
    _failed(_update(cell), "HEAD is detached in api/; check out a branch to update it")


def _diverged(cell: Path, fake_remote) -> tuple[Path, str, str]:
    top = _cloned(cell, fake_remote, {"a.txt": "one\n", "b.txt": "b\n"})
    (top / "a.txt").write_text("mine\n")
    _git(top, "commit", "-qam", "local")
    local = _git(top, "rev-parse", "HEAD").strip()
    remote = fake_remote.commit(GH, {"b.txt": "theirs\n"})
    return top, local, remote


@needs_git
def test_update_merges_when_the_branch_diverged(cell, fake_remote):
    top, local, remote = _diverged(cell, fake_remote)
    _passed(_update(cell))
    assert _git(top, "log", "-1", "--format=%P").split() == [local, remote]
    assert (top / "a.txt").read_text() == "mine\n" and (top / "b.txt").read_text() == "theirs\n"


@needs_git
def test_update_rebases_when_asked(cell, fake_remote):
    top, local, remote = _diverged(cell, fake_remote)
    _passed(_update(cell, on_divergence="rebase"))
    assert _git(top, "log", "-1", "--format=%P").split() == [remote]
    assert _git(top, "log", "-1", "--format=%s").strip() == "local"


@needs_git
def test_update_stopped_by_a_rebase_conflict_leaves_the_rebase_to_finish(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("mine\n")
    _git(top, "commit", "-qam", "local")
    fake_remote.commit(GH, {"a.txt": "theirs\n"})
    _failed(_update(cell, on_divergence="rebase"),
            "Update stopped rebasing on conflicts in a.txt: resolve them, then git add them and git rebase "
            "--continue in a terminal")
    assert (top / ".git" / "rebase-merge").is_dir() or (top / ".git" / "rebase-apply").is_dir()
    _failed(_update(cell), "a rebase is in progress in api/; finish it (git rebase --continue, or --abort) in a "
                           "terminal")


@needs_git
def test_update_brings_the_first_commits_into_an_empty_clone(cell, fake_remote):
    path = fake_remote.path(GH)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True, capture_output=True)
    _passed(_run(cell, "octo/api"))
    head = fake_remote.create(GH, {"a.txt": "first\n"})
    assert _passed(_update(cell)) == f"Updated api/ main from origin: unborn→{head[:7]}"
    assert (cell / "api" / "a.txt").read_text() == "first\n"


@needs_git
def test_update_merges_past_a_repositorys_pull_ff_only(cell, fake_remote):
    """On divergence Merge beats the repository's pull.ff=only, which on
    git 2.31 stops even an explicit --no-rebase."""
    top, local, remote = _diverged(cell, fake_remote)
    _git(top, "config", "pull.ff", "only")
    _passed(_update(cell))
    assert _git(top, "log", "-1", "--format=%P").split() == [local, remote]


@needs_git
@pytest.mark.parametrize("mode", ["merge", "rebase"])
def test_update_without_an_author_stops_before_the_autostash_takes_anything(cell, fake_remote, monkeypatch, mode):
    """With no account and no user.* git cannot record the merge or the
    rebased commits: git's own "Please tell me who you are" comes before
    the pull, so a dirty tree's edits stay in the files and no
    MERGE_AUTOSTASH is left behind."""
    top, local, _remote = _diverged(cell, fake_remote)
    (top / "b.txt").write_text("dirty\n")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    _git(top, "config", "user.useConfigOnly", "true")
    result = _update(cell, accounts=None, on_divergence=mode)
    assert not result.ok and result.note == "git var failed (exit 128)"
    assert any("Please tell me who you are" in line for line in result.logs)
    assert (top / "b.txt").read_text() == "dirty\n"
    assert not (top / ".git" / "MERGE_AUTOSTASH").exists() and _git(top, "stash", "list") == ""
    assert _git(top, "rev-parse", "HEAD").strip() == local


@needs_git
def test_update_without_an_author_still_fast_forwards(cell, fake_remote, monkeypatch):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("dirty\n")
    after = fake_remote.commit(GH, {"b.txt": "two\n"})
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    _git(top, "config", "user.useConfigOnly", "true")
    assert _passed(_update(cell, accounts=None)).endswith(f"→{after[:7]}")
    assert (top / "a.txt").read_text() == "dirty\n"


@needs_git
def test_update_refuses_changes_a_stopped_merge_set_aside(cell, fake_remote):
    """A MERGE_AUTOSTASH without a merge (left by a merge that died in a
    terminal) holds edits; Update names the way back instead of setting
    another autostash over it."""
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("set aside\n")
    stash = _git(top, "stash", "create").strip()
    _git(top, "checkout", "--", "a.txt")
    (top / ".git" / "MERGE_AUTOSTASH").write_text(stash + "\n")
    _failed(_update(cell), "a stopped merge left uncommitted changes set aside in api/ (.git/MERGE_AUTOSTASH); "
                           "git merge --quit in a terminal puts them in the stash list, then git stash pop brings "
                           "them back")
    _git(top, "merge", "--quit")
    _git(top, "stash", "pop")
    assert (top / "a.txt").read_text() == "set aside\n"


@needs_git
def test_update_ff_only_stops_without_changing_anything(cell, fake_remote):
    top, local, _remote = _diverged(cell, fake_remote)
    result = _update(cell, on_divergence="ff-only")
    assert not result.ok and result.note == "git pull failed (exit 128)"
    assert result.logs.index("git pull failed (exit 128)") < next(
        i for i, line in enumerate(result.logs) if "Not possible to fast-forward" in line)
    assert _git(top, "rev-parse", "HEAD").strip() == local


@needs_git
def test_update_keeps_uncommitted_edits_through_the_autostash(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n", "b.txt": "b\n"})
    (top / "a.txt").write_text("mine\n")
    (top / "new.txt").write_text("untracked\n")
    fake_remote.commit(GH, {"b.txt": "theirs\n"})
    _passed(_update(cell))
    assert (top / "a.txt").read_text() == "mine\n" and (top / "b.txt").read_text() == "theirs\n"
    assert _status(top) == " M a.txt\n?? new.txt\n"
    assert _git(top, "stash", "list") == ""


@needs_git
def test_update_reports_an_autostash_conflict_and_keeps_the_stash(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("mine\n")
    fake_remote.commit(GH, {"a.txt": "theirs\n"})
    result = _update(cell, on_divergence="ff-only")
    _failed(result, "Applying autostash resulted in conflicts in a.txt: your uncommitted changes are also kept "
                    "in the stash (git stash list)")
    assert "<<<<<<<" in (top / "a.txt").read_text()
    assert _git(top, "stash", "list") != ""


def _conflicted(cell: Path, fake_remote) -> Path:
    top = _cloned(cell, fake_remote, {"a.txt": "one\n", "b.txt": "b\n", "c.txt": "c\n"})
    (top / "a.txt").write_text("mine\n")
    _git(top, "commit", "-qam", "local")
    fake_remote.commit(GH, {"a.txt": "theirs\n"})
    result = _update(cell)
    _failed(result, "Update stopped on conflicts in a.txt: the markers are in the files; resolve them and run "
                    "Git Update again, or Push with a Commit message")
    assert "git pull failed (exit 1)" in result.logs
    assert (top / ".git" / "MERGE_HEAD").exists()
    assert "<<<<<<<" in (top / "a.txt").read_text()
    return top


@needs_git
def test_update_refuses_while_markers_remain(cell, fake_remote):
    top = _conflicted(cell, fake_remote)
    _failed(_update(cell), "finish the merge first")
    assert (top / ".git" / "MERGE_HEAD").exists()


@needs_git
def test_update_commits_a_clean_resolution_and_leaves_other_changes_out(cell, fake_remote):
    top = _conflicted(cell, fake_remote)
    merge_head = (top / ".git" / "MERGE_HEAD").read_text().strip()
    local = _git(top, "rev-parse", "HEAD").strip()
    (top / "a.txt").write_text("resolved\n")
    (top / "c.txt").write_text("dirty\n")
    (top / "u.txt").write_text("untracked\n")
    note = _passed(_update(cell))
    merge = _git(top, "rev-parse", "HEAD").strip()
    assert note == f"Committed the merge; api/ is up to date with origin/main at {merge[:7]}"
    assert not (top / ".git" / "MERGE_HEAD").exists()
    assert _git(top, "log", "-1", "--format=%P").split() == [local, merge_head]
    assert _git(top, "show", "HEAD:a.txt") == "resolved\n"
    assert _git(top, "show", "HEAD:c.txt") == "c\n"
    assert _status(top) == " M c.txt\n?? u.txt\n"


@needs_git
def test_a_conflicting_merge_sets_uncommitted_changes_aside_until_it_is_committed(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n", "c.txt": "c\n"})
    (top / "a.txt").write_text("mine\n")
    _git(top, "commit", "-qam", "local")
    (top / "c.txt").write_text("dirty\n")
    fake_remote.commit(GH, {"a.txt": "theirs\n"})
    _failed(_update(cell), "Update stopped on conflicts in a.txt: the markers are in the files; resolve them and "
                           "run Git Update again, or Push with a Commit message; your uncommitted changes come "
                           "back when the merge is committed")
    assert (top / "c.txt").read_text() == "c\n" and (top / ".git" / "MERGE_AUTOSTASH").exists()
    (top / "a.txt").write_text("resolved\n")
    _passed(_update(cell))
    assert (top / "c.txt").read_text() == "dirty\n" and not (top / ".git" / "MERGE_AUTOSTASH").exists()
    assert len(_git(top, "log", "-1", "--format=%P").split()) == 2


@needs_git
def test_update_refuses_added_markers_and_an_index_equal_to_head(cell, fake_remote):
    top = _conflicted(cell, fake_remote)
    _git(top, "add", "a.txt")
    _failed(_update(cell), "finish the merge first")
    _git(top, "checkout", "HEAD", "--", "a.txt")
    _failed(_update(cell), "finish the merge first: staged resolutions must be re-added")
    assert (top / ".git" / "MERGE_HEAD").exists()


@needs_git
def test_update_and_push_find_the_cells_repository(cell, fake_remote):
    _failed(_update(cell), "this cell has no repository; run Git Clone first, or set Folder")
    _failed(_update(cell, folder="api"), "no repository at api/")
    _cloned(cell, fake_remote, {"a.txt": "one\n"})
    fake_remote.create("https://github.com/octo/web.git", {"i.html": "<p>\n"})
    _passed(_run(cell, "octo/web"))
    (cell / "node_modules" / "x").mkdir(parents=True)
    _git(cell / "node_modules" / "x", "init", "-q")
    _failed(_push(cell), "this cell has several repositories (api, web); set Folder to the one to push")
    _passed(_update(cell, folder="web"))
    (cell / "plain").mkdir()
    (cell / "plain" / ".git").write_text("gitdir: elsewhere\n")
    _failed(_update(cell, folder="plain"), "no repository at plain/")


@needs_git
def test_folder_scans_never_run_on_the_event_loop(cell, fake_remote, monkeypatch):
    """The live mirror shares the run's event loop: the cell's folders are
    read in a worker thread (Clone's folder state, Update's and Push's
    repository), never on the loop."""
    import asyncio

    seen: list[bool] = []

    def on_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    real_open_parent, real_repositories = fs.open_parent, git_run.repositories

    def open_parent(root, parts, *args, **kwargs):
        if list(parts)[-1:] == [".git"]:
            seen.append(on_loop())
        return real_open_parent(root, parts, *args, **kwargs)

    def repositories(root):
        seen.append(on_loop())
        return real_repositories(root)

    monkeypatch.setattr(fs, "open_parent", open_parent)
    monkeypatch.setattr(git_run, "repositories", repositories)
    _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _passed(_update(cell))
    _passed(_update(cell, folder="api"))
    _passed(_push(cell))
    assert len(seen) >= 4 and not any(seen)


@needs_git
def test_update_refuses_remotes_a_run_may_not_use(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _git(top, "config", "branch.main.remote", "gone")
    _failed(_update(cell), "branch.main.remote names gone, which is not a remote of api/")
    _git(top, "config", "branch.main.remote", "origin")
    _git(top, "remote", "set-url", "origin", "http://github.com/octo/api.git")
    _failed(_update(cell), f"remote origin of api/: {git_hosts._R_HTTP}")
    _git(top, "remote", "set-url", "origin", GH)
    _git(top, "remote", "set-url", "--add", "origin", "git@github.com:octo/api.git")
    _failed(_update(cell), "a remote's addresses use different transports (https and ssh); give it addresses on one")


@needs_git
def test_update_and_push_a_root_repository_never_commit_the_hosts_bands(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, "octo/api", folder="."))
    after = fake_remote.commit(GH, {"b.txt": "two\n"})
    assert _passed(_update(cell)).startswith("Updated the cell root main from origin:")
    assert _git(cell, "rev-parse", "HEAD").strip() == after
    exclude = cell / ".git" / "info" / "exclude"
    exclude.write_text("")                        # as a root repository made in a terminal has it
    for band in (".collrun", "_runs/run-0123abcd", "_agent"):
        (cell / band).mkdir(parents=True, exist_ok=True)
        (cell / band / "x.txt").write_text("host\n")
    (cell / "a.txt").write_text("mine\n")
    _passed(_push(cell, commit_message="mine"))
    pushed = fake_remote.head(GH)
    assert fake_remote.tree(GH, pushed) == {"a.txt": b"mine\n", "b.txt": b"two\n"}
    assert "/.collrun/" in exclude.read_text().splitlines()


# ── Push ───────────────────────────────────────────────────────────────────


@needs_git
def test_push_sends_committed_work_to_the_upstream(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("two\n")
    _git(top, "commit", "-qam", "two")
    head = _git(top, "rev-parse", "HEAD").strip()
    assert _passed(_push(cell)) == f"Pushed api/ main to origin main at {head[:7]}"
    assert fake_remote.head(GH) == head
    assert _passed(_push(cell)) == f"Everything up-to-date: origin main is at {head[:7]}"


@needs_git
def test_push_commits_uncommitted_changes_only_with_a_message(cell, fake_remote, monkeypatch, git_spy):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    base = fake_remote.head(GH)
    (top / "a.txt").write_text("two\n")
    (top / "new.txt").write_text("new\n")
    git_spy.calls.clear()
    _failed(_push(cell), "api/ has uncommitted changes; set Commit message to commit them before pushing")
    assert "push" not in git_spy.subcommands() and fake_remote.head(GH) == base
    carry_account(monkeypatch, ACCOUNT["git"])
    _passed(_push(cell, commit_message="Edit a"))
    pushed = fake_remote.head(GH)
    info = fake_remote.commit_info(GH, pushed)
    assert info["parents"] == [base] and info["message"] == "Edit a" and info["author"] == AUTHOR
    assert fake_remote.tree(GH, pushed) == {"a.txt": b"two\n", "new.txt": b"new\n"}
    assert _status(top) == ""


@needs_git
def test_push_refuses_conflict_markers(cell, fake_remote):
    top = _conflicted(cell, fake_remote)
    result = _push(cell, commit_message="merge")
    _failed(result, "api/ still has conflict markers (a.txt); resolve them, then push")
    (top / "a.txt").write_text("resolved\n")
    _passed(_push(cell, commit_message="merge"))
    assert len(fake_remote.commit_info(GH, fake_remote.head(GH))["parents"]) == 2


@needs_git
def test_push_commits_a_markdown_underline_that_is_no_conflict(cell, fake_remote):
    """diff --check calls a line of seven '=' a conflict marker; with no
    unmerged path and no merge it is a heading's underline."""
    top = _cloned(cell, fake_remote, {"README.md": "# api\n"})
    (top / "README.md").write_text("Title\n=======\n\ntext\n")
    _passed(_push(cell, commit_message="Title"))
    assert fake_remote.tree(GH, fake_remote.head(GH))["README.md"] == b"Title\n=======\n\ntext\n"


@needs_git
def test_push_refuses_while_a_rebase_is_stopped(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("mine\n")
    _git(top, "commit", "-qam", "local")
    moved = fake_remote.commit(GH, {"a.txt": "theirs\n"})
    assert not _update(cell, on_divergence="rebase").ok
    _failed(_push(cell, push_branch="half", commit_message="half"),
            "a rebase is in progress in api/; finish it (git rebase --continue, or --abort) in a terminal")
    assert fake_remote.head(GH) == moved
    assert subprocess.run(["git", "--git-dir", str(fake_remote.path(GH)), "rev-parse", "-q", "--verify",
                           "refs/heads/half"], capture_output=True).returncode != 0


@needs_git
def test_push_never_forces_past_a_moved_remote(cell, fake_remote, git_spy):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    moved = fake_remote.commit(GH, {"b.txt": "theirs\n"})
    (top / "a.txt").write_text("mine\n")
    _git(top, "commit", "-qam", "mine")
    result = _push(cell)
    assert not result.ok and result.note == "git push failed (exit 1)"
    assert any("[rejected]" in line for line in result.logs)
    assert fake_remote.head(GH) == moved
    [argv] = [argv for argv, _env in git_spy.calls if "push" in argv]
    assert "--force" not in argv and not any(a.startswith("+") for a in argv)
    assert argv[-2:] == ("origin", "HEAD:refs/heads/main")


@needs_git
def test_push_of_a_new_branch_sets_its_upstream(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _git(top, "checkout", "-q", "-b", "feature")
    (top / "f.txt").write_text("f\n")
    _git(top, "add", "f.txt")
    _git(top, "commit", "-qm", "feature")
    head = _git(top, "rev-parse", "HEAD").strip()
    _passed(_push(cell))
    assert fake_remote.head(GH, "feature") == head
    assert _git(top, "rev-parse", "--abbrev-ref", "feature@{upstream}").strip() == "origin/feature"


@needs_git
def test_push_from_a_detached_head_needs_a_branch(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _git(top, "checkout", "-q", "--detach")
    (top / "a.txt").write_text("two\n")
    _git(top, "commit", "-qam", "two")
    _failed(_push(cell), "HEAD is detached in api/; set Push to branch")
    _passed(_push(cell, push_branch="review/two"))
    assert fake_remote.head(GH, "review/two") == _git(top, "rev-parse", "HEAD").strip()
    assert fake_remote.head(GH) != fake_remote.head(GH, "review/two")


@needs_git
def test_push_to_branch_leaves_the_upstream_alone(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    (top / "a.txt").write_text("two\n")
    _passed(_push(cell, commit_message="two", push_branch="topic"))
    assert fake_remote.head(GH, "topic") == _git(top, "rev-parse", "HEAD").strip()
    assert _git(top, "rev-parse", "--abbrev-ref", "main@{upstream}").strip() == "origin/main"


@needs_git
def test_a_triangular_push_goes_to_the_push_remote_under_the_branchs_name(cell, fake_remote, git_spy):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    upstream = fake_remote.head(GH)
    fake_remote.create(FORK, {"a.txt": "one\n"}, branch="elsewhere")
    _git(top, "remote", "add", "fork", FORK)
    _git(top, "config", "branch.main.pushRemote", "fork")
    _git(top, "branch", "-m", "main", "work")
    (top / "a.txt").write_text("two\n")
    _git(top, "commit", "-qam", "two")
    _passed(_push(cell))
    assert fake_remote.head(FORK, "work") == _git(top, "rev-parse", "HEAD").strip()
    assert fake_remote.head(GH) == upstream
    [argv] = [argv for argv, _env in git_spy.calls if "push" in argv]
    assert argv[-2:] == ("fork", "HEAD:refs/heads/work")
    assert _git(top, "config", "branch.work.remote").strip() == "origin"
    assert _git(top, "config", "branch.work.merge").strip() == "refs/heads/main"


@needs_git
def test_remote_push_default_takes_the_push_under_the_branchs_name(cell, fake_remote, git_spy):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    upstream = fake_remote.head(GH)
    fake_remote.create(FORK, {"a.txt": "one\n"}, branch="elsewhere")
    _git(top, "remote", "add", "fork", FORK)
    _git(top, "config", "remote.pushDefault", "fork")
    (top / "a.txt").write_text("two\n")
    _git(top, "commit", "-qam", "two")
    _passed(_push(cell))
    assert fake_remote.head(FORK, "main") == _git(top, "rev-parse", "HEAD").strip()
    assert fake_remote.head(GH) == upstream
    [argv] = [argv for argv, _env in git_spy.calls if "push" in argv]
    assert "--set-upstream" not in argv and argv[-2:] == ("fork", "HEAD:refs/heads/main")
    assert _git(top, "config", "branch.main.remote").strip() == "origin"


@needs_git
def test_a_mirror_or_forcing_refspec_in_the_config_deletes_nothing(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    other = fake_remote.commit(GH, {"o.txt": "o\n"}, branch="other")
    _git(top, "config", "remote.origin.mirror", "true")
    _git(top, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")
    (top / "a.txt").write_text("two\n")
    _git(top, "commit", "-qam", "two")
    _passed(_push(cell))
    assert fake_remote.head(GH, "other") == other
    assert fake_remote.head(GH) == _git(top, "rev-parse", "HEAD").strip()


@needs_git
def test_push_without_an_account_refuses_a_separate_push_address(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, GH, accounts=None))
    _git(cell / "api", "remote", "set-url", "--push", "origin", FORK)
    _failed(_push(cell, accounts=None),
            "remote origin of api/ pushes to another address than it fetches from, which needs a Git account "
            "bound to this runner")


@needs_git
def test_push_with_an_account_refuses_an_address_outside_it(cell, fake_remote):
    top = _cloned(cell, fake_remote, {"a.txt": "one\n"})
    _git(top, "remote", "set-url", "--push", "origin", "https://gitlab.com/octo/api.git")
    result = _push(cell)
    assert not result.ok and result.note.startswith("The bound Git account is for github.com, not gitlab.com")


# ── secrets ────────────────────────────────────────────────────────────────


def _cell_bytes(workspace: Path) -> bytes:
    data = b""
    for root, _dirs, files in os.walk(workspace):
        for name in files:
            data += Path(root, name).read_bytes()
    return data


@needs_git
@pytest.mark.parametrize("service, auth, remote, prompt", [
    ("github", "api_key", GH, "octo/api"),
    ("git", "basic", "https://git.example.org/octo/api.git", "octo/api.git"),
    ("github", "ssh_key", GH, "octo/api"),
])
def test_the_secret_never_reaches_a_note_log_argv_or_the_cell(cell, fake_remote, git_spy, monkeypatch,
                                                              service, auth, remote, prompt):
    snapshot = git_account_snapshot(service, auth)
    accounts = {"git": snapshot}
    carry_account(monkeypatch, snapshot)
    fake_remote.create(remote, {"a.txt": "one\n"})
    forms = [form for form in git_hosts.account_from_record(snapshot).secret_forms() if form]
    results = [_run(cell, prompt, accounts=accounts)]
    (cell / "api" / "a.txt").write_text("two\n")
    results.append(_push(cell, accounts=accounts, commit_message="two"))
    git_spy.fail("fetch", code=128, stderr=f"fatal: could not read from {forms[0]}\n".encode())
    leaked = _update(cell, accounts=accounts)
    assert not leaked.ok and "fatal: could not read from [redacted]" in leaked.logs
    results.append(leaked)
    for result in results:
        for text in (result.note or "", result.error or "", result.traceback or "", *result.logs):
            assert not any(form in text for form in forms), text
    assert not any(form.encode() in _cell_bytes(cell) for form in forms)
    for argv, _env in git_spy.calls:
        assert not any(form in arg for arg in argv for form in forms)
