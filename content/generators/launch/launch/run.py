# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml", "docker"]
# ///
"""Launch — one containerised runner behind every environment surface.

A ``mode`` setting picks the shape; all of them run over the shared
:class:`_env_shared.DockerEnvironment` helper (build the image if missing,
bind-mount the cell workspace, exec / background / expose-port / terminal /
teardown):

  * ``oneshot`` — push nothing (the workspace is bind-mounted), run ``command``
    over the cell's files, hand the workspace back to its owner, pull the
    changed/new files back into the cell (outside the live-files skip dirs)
    and drop the ones this run made and a later pass deleted, and optionally
    leave a server up (``serve_command`` + ``serve_port``, shown as
    ``preview.website``, whose toolbar's Re-run runs ``command`` again in the
    same container). Fully self-contained: no child environment is spawned.
  * ``terminal`` — attach a live terminal app (default ``bash``, shown as
    ``app.terminal``) to the cell's files with bidirectional sync; the run
    ends when the program exits or the user presses Stop
    (``gen.live_files()``), the Git account and commit author from Key
    manager wired into git, optional sudo.
  * ``app`` — run the image's own entrypoint as a daemon web app, expose its
    port as a live preview (``app.website``), and mirror edits (VS
    Code-style). The Git account's credentials arrive once the app answers,
    through one exec's environment, never the container's; the startup
    commands wait for them. The commit author comes from the Git account in
    Key manager.

The ``.terminal`` / ``.website`` pointers are the run's UI files: written in
its run folder with ``gen.update_ui_file`` (the manifest declares both suffixes),
revealed when written and deleted when their surface ends. Everything else a
run leaves — ``run.txt``, ``error.md``, a ``README.md`` placeholder, the
files a command writes — is ordinary work output at the cell root.

Image resolution (``_resolve_image``): the ``build`` config folder (a runner-
provided Dockerfile + context) builds a custom image lazily, tagged by the
CONTENT of that folder so an edit to it rebuilds; a bare ``image`` setting with
no build folder names a pre-built/registry tag; otherwise the shared base image
(``collimate-linux-small``, built from ``_env_shared/base``). The ``scripts``
config folder is staged into the container and run before the command / app.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import TYPE_CHECKING, Literal
from pathlib import Path
from urllib.parse import quote, urlsplit

import gen
from collimate.fs import WORKSPACE_SKIP_DIRS
from collimate.pointers import is_run_path, website_pointer

# The shared ``_env_shared`` cell is a SIBLING at run time (materialised into
# the cell); in the repo/tests it lives at the asset root, one or two levels
# above this generator's cell folder. Insert both candidates.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))
from _env_shared import (  # noqa: E402
    BASE_NAME, DockerEnvironment, base_context, derive_image_tag, layered_image_spec,
)

if TYPE_CHECKING:
    from _env_shared.git_account import GitAccount

# The shared base image (Debian + python + node), built from the sibling
# ``_env_shared/base`` context. Its TAG is the content hash of that context
# (``derive_image_tag``), so only the repo name is a constant — a build folder
# declares its dependency on the base by naming that repo, which is what a
# Dockerfile's ``ARG BASE=collimate-linux-small:…`` / ``FROM ${BASE}`` does.
BASE_REPO = f"collimate-{BASE_NAME}"


# ---------------------------------------------------------------------------
# Image resolution — the `build` config folder, the `image` setting, the base
# ---------------------------------------------------------------------------


# WHERE the apps our shipped launch-runner build folders install live upstream,
# and which Dockerfile ARG carries each version. The generator knows the
# registry (a fact about the app); the RUNNER names the version it wants via
# its ``packages`` block, so bumping code-server or lazygit is a runner edit.
# A runner shipping its own Dockerfile can name any of these; one it installs
# that is not listed here simply has no "latest" resolution available and
# keeps whatever its own ARG default says.
_LAUNCH_ARTIFACTS: dict = {
    "lazygit": {"source": "github:jesseduffield/lazygit", "arg": "LAZYGIT_VERSION"},
    "claude-code": {
        "source": "npm:@anthropic-ai/claude-code", "arg": "CLAUDE_CODE_VERSION"},
    "uv": {"source": "github:astral-sh/uv", "arg": "UV_VERSION"},
    "code-server": {"source": "github:coder/code-server", "arg": "CODE_SERVER_VERSION"},
}


def _declared_args(dockerfile: str) -> set:
    """The ``ARG`` names a Dockerfile declares."""
    try:
        text = Path(dockerfile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {m.group(1) for m in re.finditer(r"(?m)^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", text)}


def _requested_versions(dockerfile: str) -> dict:
    """``{BUILD_ARG: version}`` for the tools this run's image installs.

    A tool the runner's Dockerfile carries a version ARG for (Claude Code,
    uv, lazygit, code-server — see ``_LAUNCH_ARTIFACTS``) keeps itself current: it is
    "latest" unless the runner's ``packages`` pins it. Only the tools the
    Dockerfile declares float, so an unrelated release never re-tags an image
    that does not install it. Resolving "latest" reaches a registry, so it
    rides the run's ``update_packages`` grant; without it the last version
    this machine resolved stands in, and a never-resolved tool keeps the
    Dockerfile's own ARG default."""
    named = gen.packages()
    from _env_shared import git_account
    from _env_shared.upstream import LATEST, resolve_requests

    declared = _declared_args(dockerfile)
    requests = {name: LATEST for name, decl in _LAUNCH_ARTIFACTS.items() if decl["arg"] in declared}
    requests.update(named)
    if not requests:
        return {}

    allow_network = gen.grant().update_packages and gen.grant().internet != "none"
    return resolve_requests(
        _LAUNCH_ARTIFACTS, requests, allow_network=allow_network, log=gen.log,
        github_token=git_account.release_token(gen.log) if allow_network else None,
    )


def _spec(tag: str, build_ctx: "str | None", *, extra_args: "dict | None" = None) -> dict:
    """Image kwargs for a registry tag or a build without a layered base."""
    return {"image": tag, "build_context": build_ctx, "base_image": None,
            "base_context": None, "build_args": dict(extra_args or {})}


def _resolve_image(image_setting: str) -> dict:
    """The image kwargs — tag, build contexts, ``BASE`` build arg — to splat
    into :class:`DockerEnvironment`.

      * a ``build`` config folder with a Dockerfile → a CONTENT-ADDRESSED tag
        for that folder (folding in the base's digest when the Dockerfile
        ``FROM``s the shared base, which is also ensured first and injected as
        the ``BASE`` build arg). A runner's ``image`` setting does NOT override
        it: a fixed tag makes the host's tag-presence cache lie — the runner's
        Dockerfile edits would then never reach a machine that already built
        the tag once.
      * a bare ``image`` setting with no build folder → that tag as-is: a
        registry / pre-existing image whose content this cell doesn't define,
        so the cell has nothing to hash.
      * neither → the shared base image, built from ``_env_shared/base``.
    """
    image_setting = (image_setting or "").strip()
    build_path = gen.config_folder_path("build")
    dockerfile = os.path.join(build_path, "Dockerfile")
    if os.path.isfile(dockerfile):
        if image_setting:
            gen.log(f"ignoring `image: {image_setting}` — the build folder's "
                    "content names the image it produces")
        try:
            needs_base = BASE_REPO in Path(dockerfile).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            needs_base = False
        base_ctx = base_context() if needs_base else None
        versions = _requested_versions(dockerfile)
        if not base_ctx:
            return _spec(
                derive_image_tag(
                    "launch-build", build_path, build_args=versions),
                build_path, extra_args=versions,
            )
        return layered_image_spec("launch-build", build_path, build_args=versions)
    if image_setting:
        return _spec(image_setting, None)
    base_ctx = base_context()
    if not base_ctx:
        raise RuntimeError(
            "the shared base build context (_env_shared/base) is missing — "
            "the image it names can only be identified by its content"
        )
    return _spec(derive_image_tag(BASE_NAME, base_ctx), base_ctx)


def _fail(reason: str) -> None:
    """Write a clean error surface and log the reason — every mode degrades
    to this (no host / no Docker daemon / bad image) rather than crashing."""
    gen.log(reason)
    gen.update_file("error.md", f"# Run failed\n\n{reason}\n")


# ---------------------------------------------------------------------------
# scripts config folder — setup scripts run before the command / app
# ---------------------------------------------------------------------------


def _container_scripts_dir(mount: str) -> "str | None":
    """The in-container path of the staged ``scripts`` config folder (the
    workspace is bind-mounted at ``mount``), or None when the folder is empty.
    """
    host = gen.config_folder_path("scripts")
    if not os.path.isdir(host) or not any(os.scandir(host)):
        return None
    rel = os.path.relpath(host, os.path.abspath(gen.workspace_path())).replace("\\", "/")
    return f"{mount.rstrip('/')}/{rel}"


def _managed_python_preamble() -> str:
    """Select a writable per-user venv inside a built-in container.

    Login shells reset PATH. Keep their login behavior, then select this
    environment for setup scripts and the command they prepare. Custom images
    without the managed interpreter retain their own Python install policy.
    """
    return (
        'if [ -x /opt/collimate-python/bin/python ]; then '
        '_pyenv="/tmp/collimate-launch-python-$(id -u)"; '
        'if [ ! -x "$_pyenv/bin/python" ]; then '
        '(umask 077; /opt/collimate-python/bin/python -m venv "$_pyenv") || exit $?; '
        '_base_site="$(/opt/collimate-python/bin/python -c '
        "'import sysconfig; print(sysconfig.get_path(\"purelib\"))')\" || exit $?; "
        '"$_pyenv/bin/python" -c '
        "'import pathlib, sys, sysconfig; "
        "(pathlib.Path(sysconfig.get_path(\"purelib\")) / \"collimate-base.pth\").write_text(sys.argv[1] + \"\\n\")' "
        '"$_base_site" || exit $?; '
        'fi; export PATH="$_pyenv/bin:$PATH"; fi; '
    )


def _scripts_preamble(mount: str) -> str:
    """A bash snippet that runs every staged setup script in sorted order, or
    ``""`` when there are none. Ordering: scripts (sorted) run first."""
    sdir = _container_scripts_dir(mount)
    if not sdir:
        return ""
    q = shlex.quote(sdir)
    return (
        _managed_python_preamble() + f'for _s in {q}/*; do '
        f'if [ -f "$_s" ]; then echo "[scripts] $_s"; bash "$_s" || exit $?; fi; '
        f'done'
    )


# ---------------------------------------------------------------------------
# git identity and the Git account (shared by terminal + app + oneshot clones)
# ---------------------------------------------------------------------------


def _git_login() -> "GitAccount | None | Literal[False]":
    """The run's Git account (the optional ``git`` role), resolved BEFORE a
    mode places a container: None when the runner binds none, False — with
    ``error.md`` written — when the bound entry cannot serve, so every mode
    ends through :func:`_fail` instead of crashing. No network call."""
    from _env_shared import git_account

    try:
        return git_account.git_account()
    except ValueError as exc:
        _fail(f"{exc}.")
        return False


def _scrubbed(text, secrets: "tuple[str, ...]") -> str:
    """``text`` (bytes or str) with every secret form replaced, longest first."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    text = str(text)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# The SSH key's install in an image that ships ssh. The key and the known
# hosts ride the exec's environment; exit 3 means the image has no ssh.
_SSH_KEY_SCRIPT = (
    'command -v ssh >/dev/null 2>&1 || exit 3\n'
    'umask 077 && mkdir -p "$HOME/.ssh" && printf \'%s\' "$COLLIMATE_GIT_KEY" > "$HOME/.ssh/collimate_git_key" \\\n'
    '  && printf \'%s\\n\' "$COLLIMATE_GIT_KNOWN_HOSTS" > "$HOME/.ssh/collimate_git_known_hosts" \\\n'
    '  && git config --global core.sshCommand "ssh -i $HOME/.ssh/collimate_git_key -o IdentitiesOnly=yes '
    '-o StrictHostKeyChecking=yes -o UserKnownHostsFile=$HOME/.ssh/collimate_git_known_hosts"\n'
)
# The token's install: a store helper scoped to the account's base, so git
# offers the line only to URLs under it (credential.<url> matches by path
# prefix; the stored line names no path, which the store would match exactly).
_TOKEN_SCRIPT = (
    'umask 077 && git config --global "credential.$COLLIMATE_GIT_WEB.helper" store '
    '&& printf \'%s\\n\' "$COLLIMATE_GIT_CRED" > "$HOME/.git-credentials"'
)


def _configure_git(env, *, account: "GitAccount | None", user: str = "coder") -> None:
    """Set the container's git defaults and, with a Git account, its commit
    author (from Key manager) and credential: a token (or username and token)
    as one ``~/.git-credentials`` line behind a store helper scoped to the
    account's base, or an SSH key in ``~/.ssh`` in an image that ships ssh.
    The secret rides ONE host-mediated exec's *environment* (never an argv
    or the container's own environment), so it never lands in ``ps``, the
    run log or ``docker inspect``. Every exec output that is logged is
    scrubbed of the secret's forms. A write that fails is logged; the run
    goes on."""
    env.exec(
        ["bash", "-lc",
         "git config --global init.defaultBranch main; "
         "git config --global --add safe.directory '*'; "
         "git config --global pull.rebase false"],
        user=user,
    )
    if account is None:
        gen.log("No Git account bound: commits in the container need git config user.name and user.email")
        return
    env.exec(["git", "config", "--global", "user.name", account.identity.name], user=user)
    env.exec(["git", "config", "--global", "user.email", account.identity.email], user=user)
    secrets = account.secret_forms()
    if account.auth in ("api_key", "basic"):
        parts = urlsplit(account.web)
        cred = (f"https://{quote(account.username, safe='')}:{quote(account.secret, safe='')}"
                f"@{parts.netloc}")
        code, out = env.exec(["bash", "-lc", _TOKEN_SCRIPT], user=user,
                             env={"COLLIMATE_GIT_CRED": cred, "COLLIMATE_GIT_WEB": account.web + "/"})
        if code == 0:
            gen.log(f"Configured the Git account's token for HTTPS git under {account.web}")
        else:
            gen.log(f"Git credentials were not configured (exit {code}): "
                    f"{_scrubbed(out, (cred, *secrets)).strip()[-300:]}")
    elif account.auth == "ssh_key":
        known = "\n".join(host.line() for host in account.known_hosts)
        code, out = env.exec(["bash", "-lc", _SSH_KEY_SCRIPT], user=user,
                             env={"COLLIMATE_GIT_KEY": account.secret, "COLLIMATE_GIT_KNOWN_HOSTS": known})
        if code == 0:
            gen.log("Configured the Git account's SSH key in the container")
        elif code == 3:
            gen.log("This image has no ssh client, so the Git account's SSH key was not installed "
                    "(git here reaches only public repositories); use the Terminal or Lazygit preset, "
                    "or a token account")
        else:
            gen.log(f"Git credentials were not configured (exit {code}): "
                    f"{_scrubbed(out, secrets).strip()[-300:]}")


def _enable_sudo(env) -> None:
    """Grant the container's ``coder`` user passwordless sudo (needs ``sudo`` in
    the image; surfaces a clear log line either way)."""
    _, out = env.exec(
        ["bash", "-lc",
         "if command -v sudo >/dev/null 2>&1; then "
         "  echo 'coder ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/collimate && "
         "  chmod 440 /etc/sudoers.d/collimate && echo OK; "
         "else echo NOSUDO; fi"],
        user="root",
    )
    if isinstance(out, (bytes, bytearray)):
        out = out.decode("utf-8", errors="replace")
    if "OK" in (out or ""):
        gen.log("Enabled passwordless sudo for the terminal user (coder)")
    else:
        gen.log("sudo requested but not installed in the image")


# The marker the vscode image's entrypoint waits on, when the generator sets
# COLLIMATE_AWAIT_GIT_LOGIN, before it runs the startup commands.
_GIT_LOGIN_READY = "/home/coder/.collimate/git-login-ready"


def _release_startup_commands(env) -> None:
    """Tell the app's startup commands, held back for the Git account
    (``COLLIMATE_AWAIT_GIT_LOGIN``), that the login step is over, whether or
    not the credentials landed: they run either way. Best-effort; an image
    whose entrypoint does not wait ignores it."""
    marker = shlex.quote(_GIT_LOGIN_READY)
    parent = shlex.quote(os.path.dirname(_GIT_LOGIN_READY))
    try:
        env.exec(["sh", "-c", f"mkdir -p {parent} && touch {marker}"], user="coder")
    except Exception as exc:  # noqa: BLE001 — the commands start at the entrypoint's bound
        gen.log(f"could not release the startup commands: {exc}")


# ---------------------------------------------------------------------------
# workspace ownership — the bind mount stays the host user's
# ---------------------------------------------------------------------------


def _host_owner(path: str) -> "tuple[int, int] | None":
    """The ``(uid, gid)`` owning the workspace root ``path`` as the host
    sees it — this process runs on the host — or None, with a log line,
    when it cannot be read.

    Read BEFORE the container is placed: once it runs, anything in it (an
    image entrypoint and the startup commands it launches, a setup script,
    the command, the editor under sudo) can re-own the mount root — a root
    ``cp -a src/. .`` copies ``src``'s owner onto the directory it copies
    into, a ``sudo chown coder /workspace`` gives it to the editor — and
    the host sees that change too."""
    try:
        st = os.stat(path)
    except OSError as exc:
        gen.log(f"could not read the owner of the workspace ({exc}); what "
                "the container writes there is not handed back")
        return None
    return st.st_uid, st.st_gid


# The container's user and group id maps, as the kernel shows them to a
# process in it: lines of ``<first id inside> <first id outside> <count>``.
_ID_MAPS_SCRIPT = (
    "if [ -e /proc/self/uid_map ]; then cat /proc/self/uid_map; "
    "else printf '0 0 4294967295\\n'; fi && echo -- && "
    "if [ -e /proc/self/gid_map ]; then cat /proc/self/gid_map; "
    "else printf '0 0 4294967295\\n'; fi"
)


def _map_in(lines: "list[str]", outside: int) -> "int | None":
    """The id inside the container that the host id ``outside`` maps to
    through the id-map ``lines``, or None when it is not mapped. Raises
    :class:`ValueError` on a line that is not three ids."""
    for line in lines:
        first_in, first_out, count = (int(word) for word in line.split())
        if first_out <= outside < first_out + count:
            return first_in + outside - first_out
    return None


def _workspace_owner(env, host: "tuple[int, int]") -> "str | None":
    """The ``uid:gid`` every hand-back of the run gives the workspace back
    to: its host owner (:func:`_host_owner`) as the container's user
    namespace names it — the same ids on rootful Docker, root under
    rootless Docker. Read through the container's id maps, which no
    process in the container can change, so nothing running there races
    it. None, with a log line, when the maps cannot be read, or when the
    host owner is not mapped into the container (userns-remap: the
    container cannot write the workspace, and a chown to any id it has
    would give files to another host uid). With None nothing is handed
    back."""
    try:
        rc, out = env.exec(["sh", "-c", _ID_MAPS_SCRIPT], user="0:0", timeout=60)
    except Exception as exc:  # noqa: BLE001 — the run goes on without hand-backs
        rc, out = -1, str(exc)
    lines = (out or "").splitlines()
    try:
        if rc != 0 or "--" not in lines:
            raise ValueError(f"exit {rc}")
        cut = lines.index("--")
        uid_lines, gid_lines = lines[:cut], lines[cut + 1:]
        if not uid_lines or not gid_lines:
            raise ValueError("an empty id map")
        uid, gid = _map_in(uid_lines, host[0]), _map_in(gid_lines, host[1])
    except ValueError as exc:
        gen.log(f"could not read the container's id maps ({exc}): "
                f"{(out or '').strip()[-500:]}; what the container writes to "
                "the workspace is not handed back")
        return None
    if uid is None or gid is None:
        gen.log(f"the workspace owner {host[0]}:{host[1]} is not mapped into "
                "the container (a user-namespace remap); what the container "
                "writes to the workspace is not handed back")
        return None
    return f"{uid}:{gid}"


def _handback_script(mount: str, owner: str, *, prune: "frozenset[str]" = frozenset()) -> str:
    """A shell snippet giving every entry under ``mount``, the mount root
    included, to ``owner`` (``uid:gid``, from :func:`_workspace_owner`),
    touching only the entries with another owner or group so an unchanged
    tree keeps its ctimes (git's index stats them). Links are changed
    themselves (``-h``), never followed, and the walk stays on the mount
    (``-xdev``). The directories named in ``prune``, at any depth, are
    neither entered nor changed."""
    uid, gid = owner.split(":")
    m = shlex.quote(mount)
    skip = ""
    if prune:
        names = " -o ".join(f"-name {shlex.quote(n)}" for n in sorted(prune))
        skip = f"\\( -type d \\( {names} \\) \\) -prune -o "
    return (f"find {m} -xdev {skip}\\( ! -user {uid} -o ! -group {gid} \\) "
            f"-exec chown -h {owner} {{}} +")


def _hand_back_workspace(env, mount: str, owner: str, *,
                         prune: "frozenset[str]" = frozenset(),
                         stop_timeout: float | None = None) -> None:
    """Give what the container wrote under ``mount`` — as root, or as its
    image's user — back to ``owner``, running as root in the container. On
    rootful Docker the host can otherwise neither rewrite a file a command
    made (the pull-back's ``update_file`` meets EACCES) nor empty a directory
    it made when the run's workspace is removed. Walks the whole tree each
    time but the ``prune`` directories (:func:`_handback_script`). Never
    raises. Blocking: call it off the event loop. App Stop uses a short
    command deadline plus one second to kill descendants ignoring TERM;
    this does not bound an unresponsive host IPC connection."""
    try:
        command = ["sh", "-c", _handback_script(mount, owner, prune=prune)]
        timeout = 600
        if stop_timeout is not None:
            command = ["timeout", "--kill-after=1", str(stop_timeout), *command]
            # This one monitor owns both TERM and KILL. Do not add another
            # independent command timer through DockerEnvironment.exec;
            # its ordinary host reply wait remains separate.
            timeout = None
        rc, out = env.exec(command, user="0:0", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a pass or teardown must go on
        rc, out = -1, str(exc)
    if rc != 0:
        gen.log(f"could not hand {mount} back to the workspace owner "
                f"(exit {rc}): {(out or '').strip()[-500:]}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _network_command_reason(command: str) -> str | None:
    """Recognize declared setup that needs a network; not a shell policy engine."""
    lexer = shlex.shlex(str(command), posix=True, punctuation_chars=';&|\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None  # Command syntax errors belong to the command's own runner.
    chunks: list[list[str]] = [[]]
    for token in tokens:
        if token and all(char in ';&|\n' for char in token):
            chunks.append([])
        else:
            chunks[-1].append(token)
    for words in chunks:
        while words and (words[0] in {'then', 'do', 'if', 'env'} or
                         re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0])):
            words = words[1:]
        if words and words[0] == 'sudo':
            words = words[1:]
            while words and words[0].startswith('-'):
                size = 2 if words[0] in {'-u', '-g', '--user', '--group'} else 1
                words = words[size:]
        if not words:
            continue
        program, args = os.path.basename(words[0]), words[1:]
        if program in {'sh', 'bash'} and args and args[0] in {'-c', '-lc'} and len(args) > 1:
            reason = _network_command_reason(args[1])
            if reason:
                return reason
        if program == 'git' and args and args[0] == 'clone':
            # The shipped clone command takes its URL from the prompt. Keep a
            # local filesystem clone usable without an internet grant.
            refs = [arg.replace('${COLLIMATE_PROMPT}', str(gen.prompt or ''))
                    .replace('$COLLIMATE_PROMPT', str(gen.prompt or '')) for arg in args[1:]]
            if any(re.match(r'^(?:[A-Za-z][A-Za-z0-9+.-]*://|[^/\s]+:)' , ref)
                   and not ref.startswith('file://') for ref in refs):
                return 'clone a remote repository'
        if program in {'curl', 'wget'} and any('://' in arg for arg in args):
            return 'download setup files'
        if re.fullmatch(r'python(?:\d+(?:\.\d+)?)?', program) and args[:2] == ['-m', 'pip']:
            program, args = 'pip', args[2:]
        if program == 'uv' and args[:1] == ['pip']:
            program, args = 'pip', args[1:]
        if program in {'pip', 'pip3'} and 'install' in args and '--no-index' not in args:
            return 'install Python packages'
        if program in {'npm', 'pnpm', 'yarn'} and any(a in {'install', 'ci', 'add'} for a in args):
            if '--offline' not in args:
                return 'install JavaScript packages'
        if program in {'apt', 'apt-get', 'apk', 'dnf', 'yum'} and any(
                a in {'update', 'upgrade', 'install', 'add'} for a in args):
            offline = (program in {'apt', 'apt-get'} and '--no-download' in args and 'update' not in args
                       or program == 'apk' and '--no-network' in args
                       or program in {'dnf', 'yum'} and bool(set(args) & {'--cacheonly', '-C'}))
            if not offline:
                return 'install or refresh system packages'
    return None


def _offline_image_requirement(client, spec: dict) -> str | None:
    """Read local image/build facts without pulling, building or starting anything."""
    import docker.errors
    from string import Template

    def present(tag: str) -> bool:
        try:
            client.images.get(tag)
            return True
        except docker.errors.ImageNotFound:
            return False

    ready: set[str] = set()

    def inspect_image(tag: str, context: str | None, build_args: dict) -> str | None:
        if tag in ready or present(tag):
            ready.add(tag)
            return None
        if not context:
            return f'pull image {tag!r}'
        dockerfile = Path(context) / 'Dockerfile'
        try:
            source = dockerfile.read_text(encoding='utf-8')
        except OSError as exc:
            raise RuntimeError(f'Launch cannot inspect {dockerfile}: {exc}') from exc
        args: dict[str, str] = {}
        stages: set[str] = set()
        stage_count = 0
        for line in re.sub(r'\\\r?\n', ' ', source).splitlines():
            text = line.strip()
            if not text or text.startswith('#'):
                continue
            parts = text.split(None, 1)
            instruction = parts[0].upper()
            body = parts[1] if len(parts) == 2 else ''
            if instruction == 'ARG':
                name, _, value = body.strip().partition('=')
                args[name] = str(build_args.get(name, value))
                continue
            body = Template(body).safe_substitute(args)
            words = shlex.split(body)
            if instruction == 'FROM':
                refs = [word for word in words if not word.startswith('--')]
                if not refs:
                    continue
                ref = refs[0]
                if ref != 'scratch' and ref not in stages and ref not in ready and not present(ref):
                    return f'pull build image {ref!r}'
                stages.add(str(stage_count))
                stage_count += 1
                if len(refs) >= 3 and refs[1].upper() == 'AS':
                    stages.add(refs[2])
            elif instruction == 'COPY':
                for word in words:
                    if word.startswith('--from='):
                        ref = word.split('=', 1)[1]
                        if ref not in stages and ref not in ready and not present(ref):
                            return f'pull copy image {ref!r}'
            elif instruction == 'ADD' and any('://' in word and not word.startswith('file://') for word in words):
                return 'download a Dockerfile ADD source'
            elif instruction == 'RUN':
                run_body = re.sub(r'^(?:--[^\s]+\s+)+', '', body)
                if run_body.startswith('['):
                    try:
                        argv = json.loads(run_body)
                    except json.JSONDecodeError:
                        argv = None
                    if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv):
                        run_body = shlex.join(argv)
                reason = _network_command_reason(run_body)
                if reason:
                    return reason + ' while building the image'
        ready.add(tag)
        return None

    # A final content-addressed image remains usable even if its build-only
    # base was pruned. The host uses this same final-image short circuit.
    if present(spec['image']):
        return None
    if spec.get('base_image'):
        reason = inspect_image(spec['base_image'], spec.get('base_context'), {})
        if reason:
            return reason
    return inspect_image(spec['image'], spec.get('build_context'), spec.get('build_args') or {})


def _check_internet_requirements(cfg: dict) -> dict | None:
    if gen.grant().internet != 'none':
        return

    def refuse(reason: str) -> None:
        message = (f'Launch cannot {reason} with internet: none; set internet: full '
                   'or prepare the image and setup inputs locally before starting.')
        gen.log(message)
        raise RuntimeError(message)

    for key in ('command', 'serve_command', 'startup_commands'):
        value = cfg.get(key) or ''
        commands = value if isinstance(value, list) else [value]
        for command in commands:
            reason = _network_command_reason(str(command))
            if reason:
                refuse(reason)
    scripts = Path(gen.config_folder_path('scripts'))
    if scripts.is_dir():
        for script in sorted(scripts.iterdir()):
            if script.is_file() and not script.is_symlink():
                reason = _network_command_reason(script.read_text(encoding='utf-8', errors='replace'))
                if reason:
                    refuse(reason + f' in setup script {script.name!r}')
    spec = _resolve_image(cfg.get('image'))
    import docker
    client = docker.from_env()
    try:
        reason = _offline_image_requirement(client, spec)
    finally:
        client.close()
    if reason:
        refuse(reason)
    # Offline reuse must not re-pull even a locally available FROM image.
    return {**spec, 'refresh': False}


async def run() -> None:
    cfg = gen.config("settings.set_yaml")
    image_spec = _check_internet_requirements(cfg)
    mode = (cfg.get("mode") or "oneshot").strip()
    if mode == "terminal":
        await _run_terminal(cfg, image_spec=image_spec)
    elif mode == "app":
        await _run_app(cfg, image_spec=image_spec)
    else:
        await _run_oneshot(cfg, image_spec=image_spec)


# ---------------------------------------------------------------------------
# oneshot — code-runner semantics, self-contained (no child environment)
# ---------------------------------------------------------------------------


# run.txt keeps the LAST this-many characters of the command's output (the
# end is where a failure shows): it is committed to the cell on every pass.
_RUN_TXT_TAIL_CHARS = 1_000_000

# The line the host puts before the tail of an exec's output it cut.
_HOST_CUT_LINE = re.compile(r"\[\.\.\. \d+ earlier bytes of output dropped\]\n")


def _run_txt(command: str, rc: int, output: str) -> str:
    """The ``run.txt`` record of one pass: the command, its exit code and the
    tail of its output, marked when the head was dropped. The host's own
    cut line, when the output starts with one, is kept above this one, so
    the record counts both."""
    output = output or ""
    if len(output) > _RUN_TXT_TAIL_CHARS:
        host = _HOST_CUT_LINE.match(output)
        kept = host.group(0) if host else ""
        dropped = len(output) - len(kept) - _RUN_TXT_TAIL_CHARS
        which = "more" if kept else "earlier"
        output = (f"{kept}[... {dropped} {which} characters of output dropped]\n"
                  + output[-_RUN_TXT_TAIL_CHARS:])
    return f"$ {command}\n(exit {rc})\n\n{output}"


def _disk_files(root: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(relpath, bytes)`` for every file under ``root`` (the
    bind-mounted workspace) EXCEPT the host-reserved ``.collrun/`` band and
    the live-files skip dirs (:data:`WORKSPACE_SKIP_DIRS`), one file at a time so
    a large workspace is never held in memory whole. Reads the disk, not the
    SDK stage — the container's writes exist only on disk."""
    root = os.path.abspath(root)
    skip = WORKSPACE_SKIP_DIRS | {".collrun"}
    # Regular files only, never followed (the host manifest's rule): a
    # command's `npm install` / `python -m venv` / `ln -s` links are not cell
    # content — staging one fails the host's no-follow write — and an absolute
    # link would pull a host file in; a FIFO would block the read forever.
    # fwalk pins each dir by fd, so the leaf open is O_NOFOLLOW + O_NONBLOCK.
    use_fd = hasattr(os, "fwalk") and hasattr(os, "O_NOFOLLOW") \
        and os.open in os.supports_dir_fd
    walk = ((d, dn, fn, rfd) for d, dn, fn, rfd in os.fwalk(root)) if use_fd \
        else ((d, dn, fn, None) for d, dn, fn in os.walk(root))
    for dirpath, dirnames, filenames, rootfd in walk:
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root).replace("\\", "/")
            try:
                if rootfd is not None:
                    fd = os.open(fn, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=rootfd)
                else:
                    if not stat.S_ISREG(os.lstat(fp).st_mode):
                        continue
                    fd = os.open(fp, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                with os.fdopen(fd, "rb") as fh:
                    if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                        continue
                    data = fh.read()
            except OSError:
                continue
            yield rel, data


def _pass_summary(runs: int, rc: int, pulled: int, dropped: int) -> str:
    """The log line after a pass: its exit code, the files it pulled into
    the cell and the ones it dropped from it."""
    ran = f"ran ×{runs}" if runs > 1 else "ran"
    return f"{ran} (exit {rc}) · {pulled} files" + (f" · {dropped} removed" if dropped else "")


async def _run_oneshot(cfg: dict, *, image_spec: dict | None = None) -> None:
    command = (cfg.get("command") or "").strip()
    serve = (cfg.get("serve_command") or "").strip()
    serve_port = int(cfg.get("serve_port") or 0)
    mount_mode = (cfg.get("mount_mode") or "rw").strip()

    await _flush(name=gen.run_info().runner_name or "Launch")
    if not command:
        _fail("no command to run — set a `command`.")
        return
    account = _git_login()
    if account is False:
        return

    # git in the container authenticates only with the run's Git account
    # (_configure_git): a token through a store helper scoped to the
    # account's base, or an SSH key in images that ship ssh; the host's SSH
    # agent is never forwarded.
    volumes: dict = {}
    environment: dict = {"COLLIMATE_PROMPT": (gen.prompt or "")}

    # The command runs as the image's user: root on the base image, which
    # oneshot assumes. What it writes is handed back to the workspace owner,
    # read before the container exists.
    host = _host_owner(gen.workspace_path()) if mount_mode != "ro" else None
    try:
        env = DockerEnvironment(
            **(image_spec or _resolve_image(cfg.get("image"))),
            mount="/work", mount_mode=mount_mode,
            environment=environment, volumes=volumes,
            name_prefix="collimate-launch", kind="launch",
        )
    except Exception as exc:  # noqa: BLE001 — degrade cleanly without a host/daemon
        _fail(f"could not start the container environment: {exc}")
        return

    preview = ""
    server = ""
    # Whether the container may have written /work since its last hand-back.
    unsettled = False
    try:
        owner = await asyncio.to_thread(_workspace_owner, env, host) if host else None

        async def _settle(*, last: bool) -> None:
            """Hand /work back to its owner. Until the ``last`` one only the
            cell's content is walked (the pull-back reads nothing else), so
            a re-run never pays for a dependency tree's size; the last walks
            everything, with every process in the container frozen first
            so none writes while it runs."""
            nonlocal unsettled
            if owner:
                if last:
                    await asyncio.to_thread(env.freeze)
                await asyncio.to_thread(
                    functools.partial(_hand_back_workspace, env, "/work", owner,
                                      prune=frozenset() if last else WORKSPACE_SKIP_DIRS))
            unsettled = False

        if account is not None:
            _configure_git(env, account=account, user="root")

        preamble = _scripts_preamble("/work")
        full = f"{preamble}\n{command}" if preamble else command

        runs = 0
        # The workspace's files before the first pass (the run's inputs), and
        # the files a pass pulled into the cell that were not among them: a
        # later pass that deletes one of THOSE deletes it from the cell too.
        # An input is never deleted from the cell.
        inputs: "set[str] | None" = None
        made: set[str] = set()

        async def _one_pass(*, last: bool) -> "tuple[int, int, int]":
            """One command pass: snapshot the workspace DISK, exec, hand the
            workspace back to its owner (``last``: freezing what the command
            left running first, as no server follows), re-emit the deltas.
            The container writes to the bind-mounted workspace directly —
            those files never enter the SDK stage, so ``gen.list_files()``
            cannot see them; only a disk rescan can. The cell commits files
            written through the SDK, so we re-emit the disk deltas
            explicitly after each pass. Returns ``(rc, files pulled, files
            dropped)``."""
            nonlocal runs, inputs, unsettled
            runs += 1
            # Digests, not contents: two whole copies of a large workspace
            # (a dataset, a clone) would not fit in memory.
            before = {rel: hashlib.sha256(data).digest()
                      for rel, data in _disk_files(gen.workspace_path())}
            if inputs is None:
                inputs = set(before)
            gen.phase("running", f"running {command}")
            unsettled = True
            # Off the event loop: a long command must not block the serve
            # arm's action/cancel polling (the docker exec is thread-safe).
            rc, output = await asyncio.to_thread(
                lambda: env.exec(full, cwd="/work", timeout=600))
            # What the command wrote must be the host user's before the host
            # rewrites it below or removes the workspace.
            await _settle(last=last)
            gen.update_file("run.txt", _run_txt(command, rc, output))
            pulled = 0
            on_disk: set[str] = set()
            for p, data in _disk_files(gen.workspace_path()):
                on_disk.add(p)
                if p == "run.txt":
                    continue
                if before.get(p) == hashlib.sha256(data).digest():
                    continue
                if is_run_path(p):
                    # The runs' own UI files (this run's pointer, other runs'
                    # surfaces) are never work output; the cell keeps its copy.
                    gen.log(f"left {p} as the cell has it: files under _runs/ "
                            "belong to their runs")
                    continue
                try:
                    gen.update_file(p, data)
                except OSError as exc:
                    # Swapped for a link/FIFO since the scan: the host's
                    # no-follow write refuses it — skip it, keep the run.
                    gen.log(f"skipped {p}: {exc}")
                    continue
                if p not in inputs:
                    made.add(p)
                pulled += 1
            ws = os.path.abspath(gen.workspace_path())
            dropped = 0
            unknown: list[str] = []
            for p in sorted(made - on_disk):
                # Dropped only when known to be gone: no entry there any
                # more, or a directory in the file's place.
                try:
                    st = os.lstat(os.path.join(ws, p))
                except (FileNotFoundError, NotADirectoryError):
                    pass
                except OSError as exc:
                    # A directory above it the host cannot enter (a
                    # `chmod -R a-x`): still on disk as far as anyone knows.
                    unknown.append(f"{p} ({exc.strerror or exc})")
                    continue
                else:
                    if not stat.S_ISDIR(st.st_mode):
                        continue  # swapped for a link / FIFO: not content, not gone
                made.discard(p)
                gen.delete_file(p)
                dropped += 1
            if unknown:
                gen.log(f"kept {len(unknown)} file(s) the run made that the host "
                        f"cannot check on disk, e.g. {unknown[0]}")
            return rc, pulled, dropped

        rc, pulled, dropped = await _one_pass(last=not (serve and serve_port))

        if serve and serve_port:
            server_command = _managed_python_preamble() + serve if preamble else serve
            server = env.exec_background(server_command, cwd="/work")
            ep = await env.expose_port(serve_port)
            preview = gen.update_ui_file("preview.website", json.dumps(website_pointer(
                ep.port, buttons=[{"id": "rerun", "label": "Re-run"}])))
            gen.log(f"{_pass_summary(runs, rc, pulled, dropped)} · serving :{serve_port}")
            await _flush()
            await gen.reveal_file(preview)
            # Park serving, with each press of the preview's toolbar Re-run
            # re-executing the command in the SAME container against the
            # still-served preview.
            while not gen.cancelled:
                ev = await gen.wait_for_action_event(
                    source=preview, action_id="rerun", timeout=0.5)
                if ev is None:
                    continue
                try:
                    rc, pulled, dropped = await _one_pass(last=False)
                    gen.log(f"{_pass_summary(runs, rc, pulled, dropped)} "
                            f"· serving :{serve_port}")
                    await _flush()
                except Exception as exc:  # noqa: BLE001 — a failed pass must not end the serve
                    gen.log(f"re-run failed: {exc}")
            return

        gen.log(_pass_summary(runs, rc, pulled, dropped))
        await _flush()
    finally:
        if preview:
            await _drop_pointer("preview.website")
        if server:
            # The server kept writing after the last pass: stop it and give
            # it the host's stop grace to exit, so a SIGTERM handler that
            # rewrites a file finishes rather than being frozen halfway; the
            # last hand-back then freezes whatever else still runs and hands
            # back what they wrote.
            try:
                await asyncio.to_thread(
                    functools.partial(env.stop_process, server, wait=True))
            except Exception as exc:  # noqa: BLE001 — teardown goes on
                gen.log(f"stopping the server failed: {exc}")
            unsettled = True
        if unsettled:
            # Also a pass that raised before its own hand-back.
            await _settle(last=True)
        env.close()


# ---------------------------------------------------------------------------
# terminal — live terminal app with bidirectional file sync
# ---------------------------------------------------------------------------


async def _run_terminal(cfg: dict, *, image_spec: dict | None = None) -> None:
    raw_command = (cfg.get("command") or "").strip() or "bash"
    startup_commands = (cfg.get("startup_commands") or "").strip()
    mount_mode = (cfg.get("mount_mode") or "rw").strip()
    sudo_enabled = bool(cfg.get("sudo", False))

    command = shlex.split(raw_command) or ["bash"]
    label = command[0] or "terminal"

    # scripts (sorted) → startup_commands → exec into the app. exec carries the
    # preamble's exported env + cwd into the app (so cd/export/installs stick).
    preamble_parts = [p for p in (_scripts_preamble("/workspace"), startup_commands) if p]
    if preamble_parts:
        launch = ["bash", "-lc", "\n".join(preamble_parts) + f"\nexec {shlex.join(command)}"]
    else:
        launch = command

    await _flush(name=gen.run_info().runner_name or "Launch")
    account = _git_login()
    if account is False:
        return

    try:
        env = DockerEnvironment(
            **(image_spec or _resolve_image(cfg.get("image"))),
            mount="/workspace", mount_mode=mount_mode,
            name_prefix="collimate-launch-terminal", kind="launch_terminal",
        )
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not start the terminal environment: {exc}")
        return

    try:
        # Make the workspace a git repo so git tools have something to drive, and
        # hide the SDK-internal .collrun from git WITHOUT deleting it (it's
        # a real file in the cell workspace here).
        gen.phase("spinning_up", "Preparing workspace…")
        env.exec(
            ["bash", "-lc",
             "git -C /workspace rev-parse --git-dir >/dev/null 2>&1 || "
             "git -C /workspace init -q; "
             "grep -qxF '.collrun' /workspace/.git/info/exclude 2>/dev/null || "
             "printf '%s\\n' '.collrun' '.collrun/' "
             ">> /workspace/.git/info/exclude"],
            user="coder",
        )
        if sudo_enabled:
            _enable_sudo(env)
        _configure_git(env, account=account, user="coder")

        gen.phase("spinning_up", "Registering terminal session…")
        async with gen.serve_terminal(
            env.container_id, launch,
            cwd="/workspace", env={"TERM": "xterm-256color"},
            label=label, pointer=gen.run_path("app.terminal"),
        ) as session:
            gen.phase("running", f"{label} ready — open the cell to attach. Edits sync to the cell live.")
            # live_files_mode is `manual` in the manifest, so we drive the
            # bidirectional cell <-> workspace mirror ourselves here.
            async with gen.live_files(on_refused=_report_refused):
                exit_code = await gen.wait_terminal_exit(session["terminal_id"])
                if not gen.cancelled:
                    gen.log(f"Terminal program ended with exit code {exit_code}")
                    if exit_code not in (None, 0):
                        raise RuntimeError(f"Terminal program failed with exit code {exit_code}")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# app — vscode-style daemon (run the image entrypoint, expose a port, preview)
# ---------------------------------------------------------------------------

# gen.live_files()'s default skip set plus .vscode (a skip_dirs argument
# replaces the default, it does not extend it; .collrun is always skipped).
_APP_SYNC_SKIP_DIRS = WORKSPACE_SKIP_DIRS | {".vscode"}
_APP_HAND_BACK_TIMEOUT = 5.0


async def _run_app(cfg: dict, *, image_spec: dict | None = None) -> None:
    mount_mode = (cfg.get("mount_mode") or "rw").strip()
    expose_port = int(cfg.get("expose_port") or 8080)
    start_claude_code = bool(cfg.get("start_claude_code", True))
    startup_commands = _as_lines(cfg.get("startup_commands"))

    _ensure_workspace_has_placeholder()

    # Passed to the image entrypoint. PORT is the port the app binds; the rest
    # are read by entrypoints that support them (a no-op otherwise). No
    # secret goes here: every process in the container, and `docker
    # inspect`, sees the container's environment.
    environment: dict[str, str] = {
        "PORT": str(expose_port),
        "COLLIMATE_START_CLAUDE": "1" if start_claude_code else "0",
    }
    if startup_commands:
        environment["STARTUP_COMMANDS"] = json.dumps(startup_commands)

    await _flush(name=gen.run_info().runner_name or "Launch")
    account = _git_login()
    if account is False:
        return
    if account is not None:
        # The commit author is public; the entrypoint sets it before the editor starts.
        environment["GIT_USER_NAME"] = account.identity.name
        environment["GIT_USER_EMAIL"] = account.identity.email
    # The startup commands wait for the Git account's credential, which
    # reaches the editor's user only once the app answers.
    await_login = bool(account is not None and startup_commands)
    if await_login:
        environment["COLLIMATE_AWAIT_GIT_LOGIN"] = "1"

    # The editor's user (and whatever it runs under sudo) writes as its own
    # uid, not the host user's: /workspace is handed back to its owner, read
    # before the image's entrypoint (and the startup commands it launches)
    # can re-own it, when the app stops.
    host = _host_owner(gen.workspace_path()) if mount_mode != "ro" else None
    try:
        env = DockerEnvironment(
            **(image_spec or _resolve_image(cfg.get("image"))),
            mount="/workspace", mount_mode=mount_mode,
            environment=environment,
            ports={f"{expose_port}/tcp": ("127.0.0.1", None)},
            command=None,                              # run the image's own entrypoint
            name_prefix="collimate-launch-app", kind="launch_app",
        )
    except Exception as exc:  # noqa: BLE001
        reason = f"could not start the app: {exc}"
        _fail(reason)
        raise RuntimeError(reason) from exc

    pointer = ""
    owner = None

    async def _hand_back() -> None:
        """The app's one hand-back: every process in the container (the
        editor, its terminals and agents, the startup commands) frozen,
        then /workspace given back to its owner."""
        nonlocal owner
        if owner:
            await asyncio.to_thread(env.freeze)
            await asyncio.to_thread(_hand_back_workspace, env, "/workspace", owner,
                                    stop_timeout=_APP_HAND_BACK_TIMEOUT)
            # Cancellation at either await leaves the owner available for
            # the outer finally's idempotent retry.
            owner = None

    try:
        container = env.container
        host_port = _published_app_port(container, expose_port)
        owner = await asyncio.to_thread(_workspace_owner, env, host) if host else None
        gen.phase("spinning_up", "Waiting for the app…")
        if not await _wait_for_ready(host_port, timeout=120):
            if gen.cancelled:
                return
            raise RuntimeError("the app did not become ready within 120s.")

        # The editor's user gets the Git account through one exec's
        # environment, as terminal mode does, before the app is shown.
        try:
            _configure_git(env, account=account, user="coder")
        except Exception as exc:  # noqa: BLE001 — the app still serves
            secrets = account.secret_forms() if account is not None else ()
            gen.log(f"Git credentials were not configured in the app: {_scrubbed(exc, secrets)}")
        finally:
            if await_login:
                _release_startup_commands(env)

        # The host proxies a port the run's environment published on the host,
        # so the port served is the host side of the mapping, never the
        # container's own.
        await gen.serve_port(host_port, name="App", path="")
        pointer = gen.update_ui_file(
            "app.website",
            json.dumps(website_pointer(host_port, loading_cover=True)),
        )
        await _flush()
        await gen.reveal_file(pointer)
        gen.phase("running", "App is running. Edits sync to the cell live.")

        threading.Thread(target=_tail_logs, args=(container,), daemon=True).start()
        # live_files_mode is `manual`; drive the mirror ourselves so we can skip
        # editor/VCS noise dirs.
        async with gen.live_files(skip_dirs=_APP_SYNC_SKIP_DIRS, on_refused=_report_refused):
            try:
                await gen.wait_until_cancelled()
            finally:
                # Before the mirror's last flush, so it carries what the
                # hand-back makes the host user's (a root 0600 file a sudo'd
                # command wrote).
                await _hand_back()
    finally:
        try:
            if pointer:
                await _drop_pointer("app.website")
            await _hand_back()                         # stopped before the mirror ran
        finally:
            gen.log("Stopping app container…")
            env.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _report_refused(path: str, error: str) -> None:
    gen.log(f"Skipped {path}: {error}")


async def _flush(*, name: str | None = None) -> None:
    """A rejected document stays in the workspace; other output has landed."""
    try:
        if name is None:
            await gen.commit()
        else:
            await gen.commit(name=name)
    except PermissionError as exc:
        gen.log(f"Skipped refused paths: {exc}")


async def _drop_pointer(name: str) -> None:
    """Delete the UI file ``name`` (folder-relative) as its surface ends.
    Best-effort: the host clears the run folder when the run stops anyway."""
    try:
        gen.delete_ui_file(name)
        await _flush()
    except Exception as exc:  # noqa: BLE001 — teardown must not mask the body's error
        gen.log(f"removing {name} failed: {exc}")


def _published_app_port(container, expose_port: int) -> int:
    """Read the port Docker atomically bound when it placed this container."""
    bindings = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    published = bindings.get(f"{expose_port}/tcp") or []
    if len(published) != 1 or published[0].get("HostIp") != "127.0.0.1":
        raise RuntimeError("Docker did not publish the app port on loopback")
    try:
        port = int(published[0]["HostPort"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Docker returned an invalid app port") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("Docker returned an invalid app port")
    return port


async def _wait_for_ready(port: int, timeout: int = 120) -> bool:
    """Poll the app's root until it answers, off the event loop so Stop is
    seen between probes. Any HTTP status counts: a 401/403/404 root is still
    a server that is up."""
    url = f"http://127.0.0.1:{port}/"

    def _probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except urllib.error.HTTPError as exc:
            exc.close()
            return True
        except Exception:  # noqa: BLE001 — not listening yet
            return False

    deadline = time.monotonic() + timeout
    while not gen.cancelled and time.monotonic() < deadline:
        if await asyncio.to_thread(_probe):
            return True
        await asyncio.sleep(2)
    return False


def _ensure_workspace_has_placeholder() -> None:
    # The run folder (the host's run card, other runs' surfaces) is not
    # something to edit in the app: only work files count.
    if any(not is_run_path(p) for p in gen.list_files()):
        return
    gen.update_file(
        "README.md",
        "# Empty workspace\n\nNo input cell was attached. Create files in the app, "
        "or run a runner that populates this cell first.\n",
    )


def _tail_logs(container) -> None:
    try:
        for chunk in container.logs(stream=True, follow=True):
            line = chunk.decode("utf-8", errors="replace").rstrip()
            if line:
                gen.log(f"[container] {line}")
    except Exception as e:  # noqa: BLE001
        gen.log(f"log tail ended: {e}")


def _as_lines(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None and str(x).strip()]
    if isinstance(raw, str):
        return [ln for ln in (l.strip() for l in raw.splitlines()) if ln]
    return []


if __name__ == "__main__":
    gen.main(run)
