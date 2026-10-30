# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Git: clone or update a repository into a folder of this cell, or push the
folder's edits, with git on this machine. All git work happens in a temporary
clone outside the cell; the cell never holds .git (see _env_shared/repositories.py).
The repository's host, credential and commit author come from the run's Git
account (_env_shared/git_account.py)."""
import asyncio
import os
import sys
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))
import gen  # noqa: E402

MAIN = "settings.set_yaml"

_ACTION_ERROR = "Action must be Clone / Update or Push"
_PUSH_NEEDS_ACCOUNT = ("Pushing needs a Git account bound to this runner (a Key manager GitHub, GitLab or Git "
                       "server entry) that can write to {r}. Nothing changed")
_UNEXPECTED = "Git run failed unexpectedly ({type}); see the log"


def _scrub(text: str, forms) -> str:
    """``text`` with every non-empty secret form replaced by ``[redacted]``."""
    for form in forms:
        if form:
            text = text.replace(form, "[redacted]")
    return text


def _text_any(s: dict, key: str) -> str:
    """A text setting, stripped; a YAML 1.1 boolean (an unquoted on/off/yes/no) or None is blank."""
    value = s.get(key)
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _text(s: dict, key: str, label: str) -> str:
    """A one-line text setting; a line break refuses (never echoing the value)."""
    value = _text_any(s, key)
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be one line")
    return value


def _choice(value, allowed: set, default: str) -> str:
    """A dropdown value: a YAML 1.1 boolean or None is the default; anything outside ``allowed`` refuses."""
    if value is None or isinstance(value, bool):
        return default
    choice = str(value).strip().lower()
    if choice not in allowed:
        raise ValueError(_ACTION_ERROR)
    return choice


async def run():
    from _env_shared import git_account, repositories as repos

    secrets: tuple = ()
    logged: list = []

    def log(line: str) -> None:
        line = _scrub(str(line), secrets)
        logged.append(line)
        gen.log(line)

    try:
        s = gen.config(MAIN) or {}
        action = _choice(s.get("action"), {"sync", "push"}, "sync")
        account = git_account.git_account()
        secrets = account.secret_forms() if account else ()
        remote = repos.parse_prompt(gen.prompt, account, log=log)
        folder_setting = _text(s, "folder", "Folder")
        branch = _text(s, "branch", "Branch")
        if branch:
            repos.check_branch(branch)
        if gen.grant().internet == "none":
            raise repos.RepositoryError(repos.E1)
        target = repos.resolve_target(Path(gen.workspace_path()), remote, folder_setting, log=log)
        if action == "sync":
            report = await repos.sync(target, account=account, branch=branch, log=log)
            note = report.note()
            if report.kind == "cloned" and os.path.isfile(
                    os.path.join(gen.workspace_path(), target.folder, "README.md")):
                await gen.reveal_file(f"{target.folder}/README.md")
        else:
            if account is None:
                raise repos.RepositoryError(_PUSH_NEEDS_ACCOUNT.format(r=repos.display_of(remote)))
            push_branch = _text(s, "push_branch", "Push to branch")
            if push_branch:
                try:
                    repos.check_branch(push_branch)
                except ValueError:
                    raise ValueError(repos.I7) from None
            message = _text_any(s, "commit_message") or None
            report = await repos.push(target, account=account, message=message,
                                      branch=branch, push_branch=push_branch, log=log)
            note = report.note()
            if report.compare_url:
                log(f"Open a merge or pull request: {report.compare_url}")
        note = _scrub(note, secrets)
        if note not in logged:  # the engine logs its report notes itself
            log(note)
        gen.finish(note=note, outcome="pass")
    except (repos.RepositoryError, ValueError, PermissionError) as exc:
        for line in getattr(exc, "details", ()):
            log(line)
        msg = _scrub(str(exc), secrets)
        log(msg)
        gen.finish(note=msg, outcome="fail")
        raise RuntimeError(msg) from None
    except (gen.Cancelled, asyncio.CancelledError):
        raise
    except Exception as exc:
        gen.finish(note=_UNEXPECTED.format(type=type(exc).__name__), outcome="fail")
        raise


if __name__ == "__main__":
    gen.main(run)
