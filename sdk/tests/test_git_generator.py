# SPDX-License-Identifier: MIT
"""The Git generator (content/generators/git): its manifest, settings,
presets and packaged runners, and its runs end to end through the shared
repository engine against local stand-ins for the remotes (``fake_remote``)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from collimate import git_hosts
from collimate.config_values import resolve_form_values
from collimate.lint import Level, lint_path
from collimate.testing import run_generator

# The engine's modules import before any run, so the fixtures' patches on
# them survive run_generator's module cleanup.
from _env_shared import git_account, git_run, repositories  # noqa: F401
from tests._git_fixtures import GH, GIT_OK, GL, git_account_snapshot
from tests._workspace import GENERATORS

CELL = GENERATORS / "git"
PACKAGE = CELL / "git"
ENTRY = PACKAGE / "git.py"
MAIN = "settings.set_yaml"
TOKEN = "ghp_fake"
ACCOUNT = {"git": git_account_snapshot()}
FULL = {"docker": "none", "internet": "full"}
FIELDS = ["action", "folder", "branch", "commit_message", "push_branch"]
AUTHOR = "Ada Lovelace <ada@example.org>"
D = "github.com/octo/api"
_ED = git_hosts.PINNED_HOST_KEYS["github.com"][0].split(" ", 1)[1]

needs_git = pytest.mark.skipif(not GIT_OK, reason="needs git 2.31 or later")


def _yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _settings(**over) -> dict:
    values = {key: "" for key in FIELDS}
    values["action"] = "sync"
    values.update(over)
    return values


def _run(workspace: Path, prompt: str, *, accounts=ACCOUNT, permissions=FULL, **settings):
    return run_generator(ENTRY, prompt=prompt, config={MAIN: _settings(**settings)},
                         accounts=accounts, permissions=permissions, workspace=workspace)


def _push(workspace: Path, prompt: str, **settings):
    return _run(workspace, prompt, action="push", **settings)


def _marker(workspace: Path, folder: str) -> dict:
    return json.loads((workspace / f"{folder}.git-checkout.json").read_text(encoding="utf-8"))


def _failed(result, note: str) -> None:
    """A refusal: outcome fail, the note is the message and the run's error equals it."""
    assert not result.ok
    assert result.outcome == "fail"
    assert result.note == note
    assert result.error == note


def _passed(result) -> str:
    assert result.ok, (result.error, result.traceback)
    assert result.outcome == "pass"
    return result.note


@pytest.fixture
def cell(tmp_path) -> Path:
    workspace = tmp_path / "cell"
    workspace.mkdir()
    return workspace


# ── shape ──────────────────────────────────────────────────────────────────


def test_manifest_shape():
    assert _yaml(CELL / "cell.yaml") == {"name": "Git", "order": 20, "icon": "git", "shared": ["_env_shared"]}
    manifest = _yaml(PACKAGE / "git.generator")
    assert manifest["id"] == "git" and manifest["name"] == "Git"
    assert manifest["entrypoint"] == "git.py"
    assert manifest["runs_on"] == "local-machine"
    assert manifest["prompt_required"] is False
    assert manifest["prompt_intent"] == ("The repository: its address (https://host/path, ssh://user@host/path or "
                                         "user@host:path), or owner/name on the bound GitHub or GitLab account's "
                                         "server")
    assert manifest["main_config"] == MAIN and manifest["draft_files"] == [MAIN]
    for absent in ("requires_docker", "daemon", "ui_files", "opens", "env_passthrough",
                   "live_files_mode", "takes_selected_file"):
        assert absent not in manifest, absent
    assert manifest["account_requirements"] == {"git": {
        "label": "Git account", "kind": "git", "capabilities": ["git"], "required": False}}
    assert manifest["configs"] == [
        {"id": "default", "name": "Clone / Update", "path": "configs/default/manifest.config_yaml"},
        {"id": "push", "name": "Push", "path": "configs/push/manifest.config_yaml"},
    ]


def test_settings_fields_types_defaults_and_conditions():
    fields, _ = resolve_form_values(_yaml(PACKAGE / MAIN))
    by_key = {field["key"]: field for field in fields}
    assert [field["key"] for field in fields] == FIELDS
    action = by_key["action"]
    assert action["type"] == "dropdown" and action["default"] == "sync"
    assert [(o["value"], o["label"]) for o in action["options"]] == [("sync", "Clone / Update"), ("push", "Push")]
    for key in FIELDS[1:]:
        assert by_key[key]["type"] == "text" and by_key[key]["default"] == "", key
    assert {key for key in FIELDS if by_key[key].get("show_if")} == {"commit_message", "push_branch"}
    for key in ("commit_message", "push_branch"):
        assert by_key[key]["show_if"] == {"action": "push"}, key
    assert {key for key in FIELDS if by_key[key].get("advanced")} == {"branch", "push_branch"}
    assert "<folder>.git-checkout.json" in by_key["branch"]["description"]


@pytest.mark.parametrize("path, action, ident", [
    ("git/configs/default/manifest.config_yaml", "sync", None),
    ("git/configs/push/manifest.config_yaml", "push", None),
    ("git-sync/git-sync.runner", "sync", ("git-sync", "Git Clone / Update")),
    ("git-push/git-push.runner", "push", ("git-push", "Git Push")),
])
def test_configs_and_runners_list_every_field_with_docker_none_internet_full(path, action, ident):
    data = _yaml(CELL / path)
    assert list(data["settings"]) == [MAIN]
    assert list(data["settings"][MAIN]) == FIELDS
    assert data["settings"][MAIN] == _settings(action=action)
    assert data["permissions"] == {"docker": "none", "internet": "full"}
    assert data["prompt"] == "" and data["folders"] == [] and data["accounts"] == {}
    assert data["tags"] == ["git"]
    assert "GitHub" not in data["description"]
    if ident:
        assert (data["id"], data["name"]) == ident and data["generator"] == "git"


def test_lint_clean():
    issues = [issue for issue in lint_path(CELL) if issue.level in (Level.ERROR, Level.WARNING)]
    assert issues == []


# ── refusals ───────────────────────────────────────────────────────────────


def test_blank_prompt_refuses_i1(cell, git_spy):
    _failed(_run(cell, "  \n"), repositories.I1)
    assert git_spy.calls == []


def test_two_line_prompt_refuses_i4(cell, git_spy):
    _failed(_run(cell, "octo/api\nocto/web\n"), "The prompt names one repository; it has 2 lines")
    assert git_spy.calls == []


def test_short_path_without_an_account_refuses_i1b(cell, git_spy):
    _failed(_run(cell, "octo/api", accounts=None), repositories.I1b)
    assert git_spy.calls == []


@pytest.mark.parametrize("action", ["merge", "pull"])
def test_bad_action_refuses_i9(cell, git_spy, action):
    _failed(_run(cell, "octo/api", action=action), "Action must be Clone / Update or Push")
    assert git_spy.calls == []


@pytest.mark.parametrize("action", ["sync", "push"])
def test_internet_none_refuses_before_any_git(cell, git_spy, action):
    result = _run(cell, "octo/api", action=action, permissions={"docker": "none", "internet": "none"})
    _failed(result, "Git needs internet access; set Internet to full in this runner's permissions. "
                    "Nothing changed")
    assert git_spy.calls == []


def test_ssh_remote_needs_internet_full_a5(cell, git_spy):
    box = {"git": git_account_snapshot("git", "ssh_key", metadata={"known_hosts": f"box.example.org {_ED}"})}
    result = _run(cell, "git@box.example.org:team/app.git", accounts=box,
                  permissions={"docker": "none", "internet": "proxy"})
    _failed(result, repositories.A5)
    assert git_spy.calls == []


def test_git_missing_fails_with_install_message(tmp_path):
    """git missing from the run's PATH: a fresh process, so no earlier run's
    cached preflight answers for it."""
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
    assert not [name for name in os.listdir(workspace) if not name.startswith(".")]


@pytest.mark.parametrize("snapshot, message", [
    (git_account_snapshot(kind="github"), git_account.G1),
    (git_account_snapshot("github", "basic"), git_account.G1),
    (git_account_snapshot(metadata={"author_email": None}), git_account.G2),
    (git_account_snapshot(secrets={}), git_account.G3.format(what="token")),
    (git_account_snapshot(endpoint="https://api.github.com"), "The selected Git account's address cannot be used"),
    (git_account_snapshot("git", "ssh_key", secrets={"private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n"
                                                                     "-----END RSA PRIVATE KEY-----\n"}),
     "The selected Git account's SSH key cannot be used (This is an old-format key"),
    (git_account_snapshot("git", "ssh_key", metadata={"known_hosts": "* ssh-ed25519 AAAA"}),
     "The selected Git account's Known hosts cannot be read (Known hosts line 1 uses a wildcard"),
])
@pytest.mark.parametrize("action", ["sync", "push"])
def test_broken_bound_account_refuses_with_key_manager_fix(cell, git_spy, snapshot, message, action):
    result = _run(cell, "https://github.com/octo/api", action=action, accounts={"git": snapshot})
    assert not result.ok and result.outcome == "fail" and result.note == result.error
    assert result.note.startswith(message) and "Key manager" in result.note
    assert git_spy.calls == []


# ── flows ──────────────────────────────────────────────────────────────────


@needs_git
def test_clone_update_current_notes_and_pass(cell, fake_remote):
    first = fake_remote.create(GH, {"README.md": "# api\n", "src/a.py": "a = 1\n"})
    note = _passed(_run(cell, "https://github.com/octo/api", accounts=None))
    assert note == f"Cloned {D} main@{first[:7]} into api/: 2 files"
    assert (cell / "api" / "src" / "a.py").read_text() == "a = 1\n"
    assert not (cell / "api" / ".git").exists()
    assert _marker(cell, "api") == repositories.Marker(GH, "main", first).data()

    second = fake_remote.commit(GH, {"src/b.py": "b = 2\n"})
    note = _passed(_run(cell, "octo/api"))
    assert note == (f"Updated api/ from {D} main {first[:7]}→{second[:7]} (1 new commits): "
                    "1 files written, 0 deleted · no local edits")
    assert (cell / "api" / "src" / "b.py").read_text() == "b = 2\n"
    assert _marker(cell, "api")["commit"] == second

    result = _run(cell, "git@github.com:octo/api.git")
    assert _passed(result) == f"Checked {D} main: api/ is up to date at {second[:7]}"
    assert result.note in result.logs


@needs_git
def test_conflict_fails_with_note_outcome_fail_and_error_equals_note(cell, fake_remote):
    fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, "octo/api"))
    before = _marker(cell, "api")
    (cell / "api" / "a.txt").write_text("mine\n")
    fake_remote.commit(GH, {"a.txt": "theirs\n"})
    result = _run(cell, "octo/api")
    _failed(result, f"Did not update {D}: 1 files conflict between your edits and the remote's main (a.txt). "
                    "Nothing changed")
    assert f"Conflict: {D} a.txt" in result.logs
    assert result.note in result.logs
    assert (cell / "api" / "a.txt").read_text() == "mine\n"
    assert _marker(cell, "api") == before


@needs_git
def test_push_without_account_refuses(cell, fake_remote, git_spy):
    head = fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, "https://github.com/octo/api", accounts=None))
    (cell / "api" / "a.txt").write_text("two\n")
    git_spy.calls.clear()
    _failed(_push(cell, "https://github.com/octo/api", accounts=None),
            "Pushing needs a Git account bound to this runner (a Key manager GitHub, GitLab or Git server entry) "
            f"that can write to {D}. Nothing changed")
    assert git_spy.calls == []
    assert fake_remote.head(GH) == head


@needs_git
def test_push_is_authored_by_the_account_identity(cell, fake_remote):
    base = fake_remote.create(GH, {"a.txt": "one\n", "b.txt": "b\n"})
    _passed(_run(cell, "octo/api"))
    (cell / "api" / "a.txt").write_text("two\n")
    result = _push(cell, "octo/api", commit_message="Fix a")
    note = _passed(result)
    pushed = fake_remote.head(GH)
    assert note == f"Pushed api/ to {D} main {base[:7]}→{pushed[:7]}: 1 files changed"
    info = fake_remote.commit_info(GH, pushed)
    assert info["parents"] == [base]
    assert info["author"] == AUTHOR
    assert info["message"].strip() == "Fix a"
    assert fake_remote.git(GH, "log", "-1", "--format=%cn <%ce>", pushed).decode().strip() == AUTHOR
    assert fake_remote.tree(GH, pushed)["a.txt"] == b"two\n"
    assert _marker(cell, "api") == repositories.Marker(GH, "main", pushed).data()
    assert f"Pushing {pushed[:7]} to {D} main as {AUTHOR}" in result.logs


@needs_git
def test_push_folder_must_match_prompt_f4(cell, fake_remote, git_spy):
    fake_remote.create(GH, {"a.txt": "one\n"})
    web = fake_remote.create("https://github.com/octo/web.git", {"index.html": "<p>\n"})
    _passed(_run(cell, "octo/api"))
    (cell / "api" / "a.txt").write_text("two\n")
    _failed(_push(cell, "octo/web", folder="api"),
            f"Folder api/ holds {D}, not github.com/octo/web; set Folder to another name. Nothing changed")
    # Editing the record cannot redirect a push either: the prompt authorises the target.
    marker = _marker(cell, "api")
    marker["remote"] = "https://github.com/octo/web.git"
    (cell / "api.git-checkout.json").write_text(json.dumps(marker, indent=2) + "\n")
    _failed(_push(cell, "octo/api"),
            f"Folder api/ holds github.com/octo/web, not {D}; set Folder to another name. Nothing changed")
    assert "push" not in git_spy.subcommands()
    assert fake_remote.head("https://github.com/octo/web.git") == web


@needs_git
def test_new_branch_push_logs_compare_link(cell, fake_remote):
    base = fake_remote.create(GH, {"a.txt": "one\n"})
    _passed(_run(cell, "octo/api"))
    (cell / "api" / "a.txt").write_text("two\n")
    result = _push(cell, "octo/api", push_branch="feature/x", commit_message="Try x")
    note = _passed(result)
    tip = fake_remote.head(GH, "feature/x")
    link = "https://github.com/octo/api/compare/main...feature/x?expand=1"
    assert note == (f"Pushed api/ to new branch feature/x of {D} at {tip[:7]}: 1 files changed; "
                    f"api/ now follows feature/x · open a merge or pull request: {link}")
    assert f"Open a merge or pull request: {link}" in result.logs
    assert fake_remote.head(GH) == base
    assert fake_remote.commit_info(GH, tip)["parents"] == [base]
    assert _marker(cell, "api") == repositories.Marker(GH, "feature/x", tip).data()

    # The branch now exists: another copy of main naming it never overwrites it.
    _passed(_run(cell, "octo/api", folder="again"))
    (cell / "again" / "a.txt").write_text("three\n")
    _failed(_push(cell, "octo/api", folder="again", push_branch="feature/x"),
            f"Branch feature/x already exists in {D}; pushing never overwrites a branch, so choose a new "
            "name. Nothing changed")
    assert fake_remote.head(GH, "feature/x") == tip


@needs_git
def test_compare_link_for_github_and_gitlab_none_for_git_server(cell, fake_remote):
    gitlab = {"git": git_account_snapshot("gitlab", endpoint="https://example.com/gitlab")}
    fake_remote.create(GL, {"a.txt": "one\n"})
    _passed(_run(cell, "group/sub/app", accounts=gitlab))
    (cell / "app" / "a.txt").write_text("two\n")
    result = _push(cell, "group/sub/app", accounts=gitlab, push_branch="fix")
    _passed(result)
    link = "https://example.com/gitlab/group/sub/app/-/compare/main...fix"
    assert f"Open a merge or pull request: {link}" in result.logs and link in result.note

    server = {"git": git_account_snapshot("git", "basic")}
    remote = "https://git.example.org/team/tool.git"
    fake_remote.create(remote, {"a.txt": "one\n"})
    _passed(_run(cell, "team/tool.git", accounts=server))
    (cell / "tool" / "a.txt").write_text("two\n")
    result = _push(cell, "team/tool.git", accounts=server, push_branch="fix")
    note = _passed(result)
    assert note.endswith("tool/ now follows fix")
    assert not any("pull request" in line for line in result.logs)


@needs_git
def test_folder_setting_two_repositories_one_cell(cell, fake_remote):
    other = "https://github.com/other/api.git"
    ours = fake_remote.create(GH, {"a.txt": "octo\n"})
    theirs = fake_remote.create(other, {"a.txt": "other\n"})
    _passed(_run(cell, "octo/api"))
    note = _passed(_run(cell, "other/api", folder="other-api"))
    assert note == f"Cloned github.com/other/api main@{theirs[:7]} into other-api/: 1 files"
    assert (cell / "api" / "a.txt").read_text() == "octo\n"
    assert (cell / "other-api" / "a.txt").read_text() == "other\n"
    assert _marker(cell, "api")["commit"] == ours
    assert _marker(cell, "other-api") == repositories.Marker(other, "main", theirs).data()
    # Blank Folder finds each repository's own folder by its checkout record.
    assert _passed(_run(cell, "other/api")) == (f"Checked github.com/other/api main: other-api/ is up to date at "
                                                f"{theirs[:7]}")
    assert _passed(_run(cell, "octo/api")) == f"Checked {D} main: api/ is up to date at {ours[:7]}"


@needs_git
def test_notes_fit_300_chars_with_more_suffix(cell, fake_remote):
    names = [f"{letter}{'x' * 61}.txt" for letter in "abcd"]
    fake_remote.create("https://h.io/o/r", {name: "one\n" for name in names})
    _passed(_run(cell, "https://h.io/o/r", accounts=None))
    for name in names:
        (cell / "r" / name).write_text("mine\n")
    fake_remote.commit("https://h.io/o/r", {name: "theirs\n" for name in names})
    result = _run(cell, "https://h.io/o/r", accounts=None)
    assert not result.ok and result.outcome == "fail"
    assert len(result.note) <= 300 and result.note == result.error
    assert result.note.startswith("Did not update h.io/o/r: 4 files conflict between your edits and the "
                                  "remote's main (")
    assert ", +1 more)" in result.note and result.note.endswith("Nothing changed")
    assert not any(name in result.note for name in names)  # each path is cut
    assert all(f"Conflict: h.io/o/r {name}" in result.logs for name in names)

    # A new-branch note too long for its pull-request link drops the link; the log keeps it.
    folder, branch = "f" * 40, "feature/" + "y" * 52
    fake_remote.create("https://github.com/o/s.git", {"a.txt": "one\n"})
    _passed(_run(cell, "o/s", folder=folder))
    (cell / folder / "a.txt").write_text("two\n")
    result = _push(cell, "o/s", folder=folder, push_branch=branch, commit_message="Long")
    note = _passed(result)
    assert len(note) <= 300
    assert note.endswith(f"{folder}/ now follows {branch}")
    assert (f"Open a merge or pull request: https://github.com/o/s/compare/main...{branch}?expand=1"
            in result.logs)


@pytest.mark.parametrize("prompt", ["https://u:ghp_leak@github.com/o/r", "https://oauth2:glpat-x@gitlab.com/g/p"])
def test_pasted_token_url_never_reaches_note_log_or_error(cell, git_spy, prompt):
    result = _run(cell, prompt)
    _failed(result, repositories.I2)
    for text in (result.note, result.error, result.traceback or "", *result.logs):
        assert "ghp_leak" not in text and "glpat-x" not in text
    assert git_spy.calls == []


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
def test_token_never_in_note_logs_error_or_files(cell, fake_remote, git_spy, service, auth, remote, prompt):
    snapshot = git_account_snapshot(service, auth)
    accounts = {"git": snapshot}
    fake_remote.create(remote, {"a.txt": "one\n"})
    results = [_run(cell, prompt, accounts=accounts)]
    (cell / "api" / "a.txt").write_text("two\n")
    results.append(_push(cell, prompt, accounts=accounts))
    fake_remote.remove(remote)
    missing = _run(cell, prompt, accounts=accounts)
    assert not missing.ok and "was not found with the selected Git account" in missing.note
    results.append(missing)
    import gen
    real = gen.account
    try:
        gen.account = lambda role: snapshot
        forms = [form for form in git_account.git_account().secret_forms() if form]
    finally:
        gen.account = real
    assert forms
    for result in results:
        for text in (result.note or "", result.error or "", result.traceback or "", *result.logs):
            assert not any(form in text for form in forms), text
    stored = _cell_bytes(cell)
    assert not any(form.encode() in stored for form in forms)
    for argv, _env in git_spy.calls:
        assert not any(form in arg for arg in argv for form in forms)


@needs_git
def test_readme_revealed_after_first_clone(cell, fake_remote):
    fake_remote.create(GH, {"README.md": "# api\n"})
    first = _run(cell, "octo/api")
    _passed(first)
    reveals = [call for call in first.tier_b_calls if call.get("op") == "reveal_file"]
    assert [(call["path"], call["committed"]) for call in reveals] == [("api/README.md", True)]

    fake_remote.commit(GH, {"README.md": "# api, again\n"})
    second = _run(cell, "octo/api")
    _passed(second)
    assert not [call for call in second.tier_b_calls if call.get("op") == "reveal_file"]

    fake_remote.create("https://github.com/octo/bare.git", {"main.py": "print()\n"})
    third = _run(cell, "octo/bare")
    _passed(third)
    assert not [call for call in third.tier_b_calls if call.get("op") == "reveal_file"]
