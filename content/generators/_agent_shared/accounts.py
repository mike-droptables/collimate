"""Runner account bindings, resolved once by the host and never prompted for."""
from __future__ import annotations
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
import gen
from .secret_hygiene import remember, remember_account
from collimate.account_taxonomy import (
    STANDARD_MODEL_ENDPOINTS as _STANDARD_ENDPOINTS,
    MODEL_LOGIN_PLANS,
    default_model_for,
    is_cli_model_login as _cli_login,
    model_account_provider as model_provider,
)

if TYPE_CHECKING:
    from collimate.git_hosts import GitAccount


def model_endpoint(account: dict) -> str:
    endpoint = str(account.get('endpoint') or '').rstrip('/')
    if endpoint:
        return endpoint
    service = {'google': 'gemini'}.get(account.get('service'), account.get('service'))
    endpoint = _STANDARD_ENDPOINTS.get(service)
    if not endpoint:
        raise ValueError('A custom model connection requires an explicit endpoint in Key Manager')
    return endpoint


def model_account() -> dict:
    value = remember_account(gen.account('model'))
    if value.get('kind', 'model') != 'model':
        raise ValueError('Select a model subitem from Key Manager for the model role')
    # A blank model is the harness's default — a complete setup wherever one
    # exists; only a custom endpoint has none.
    if not value.get('model') and default_model_for(value) is None:
        raise ValueError('The selected model account has no model and this endpoint has no default; enter one in Key Manager')
    return value


def model_config(values: dict, account: dict) -> dict:
    """Combine generator settings with authoritative account connection data."""
    out = dict(values)
    provider = model_provider(account)
    auth = str(account.get('auth_type') or 'api_key')
    if _cli_login(account) and auth not in {'cli', 'subscription'}:
        raise ValueError('CLI login model entries require login authentication in Key Manager')
    metadata = account.get('metadata') or {}
    harness = metadata.get('harness')
    # Saved model subitems own their harness, just as they own their model and
    # connection. Existing runner presets must not silently override that choice.
    # Direct callers without a model-kind snapshot retain explicit settings.
    if account.get('kind') == 'model' or account.get('credential_account_id'):
        backend = str(harness or 'pydantic-ai')
    else:
        backend = out.get('backend') or harness or 'pydantic-ai'
    endpoint = str(account.get('endpoint') or '').rstrip('/')
    if not endpoint and not _cli_login(account) and auth != 'none':
        endpoint = model_endpoint(account)
    if endpoint and backend != 'pydantic-ai':
        standard = _STANDARD_ENDPOINTS.get(provider)
        if endpoint != standard:
            raise ValueError(f'Backend {backend!r} does not support a custom endpoint; use pydantic-ai')
    if auth == 'none':
        if backend != 'pydantic-ai' or not endpoint:
            raise ValueError('Unauthenticated model connections require a pydantic-ai endpoint')
        auth = 'api_key'

    if auth in {'subscription', 'cli'}:
        auth = str((account.get('metadata') or {}).get('plan') or
                   MODEL_LOGIN_PLANS.get(provider) or '')
    if auth != 'api_key' and harness and backend != harness:
        raise ValueError(f'This login belongs to {harness!r}; select a matching account or backend')
    out.update(backend=backend, provider=provider, model=account['model'], auth=auth, _account=account)
    return out


async def account_api_key(provider: str) -> str:
    """Resolve the chosen model account key; never consult ambient credentials.

    The key is returned, never written to this process's environment: each
    caller hands it to exactly the child that bills with it (the adapter's
    or container's own env, a client constructor), so no other child process
    — an npm install, a version probe, a git clone — inherits it."""
    account = await asyncio.to_thread(model_account)
    chosen = model_provider(account)
    if provider != chosen:
        raise PermissionError(f'Model account does not grant provider {provider!r}')
    return account_token(account)


def account_token(account: dict) -> str:
    """The selected connection's API credential, including refreshed bearer tokens."""
    remember_account(account)
    auth = account.get('auth_type')
    if auth == 'none':
        return 'not-required'
    key_name = 'api_key'
    key = (account.get('secrets') or {}).get(key_name)
    if not isinstance(key, str) or not key:
        raise LookupError(f'Selected model account has no {key_name}; edit it in Key Manager')
    return key


async def account_secret(reference: str) -> str:
    """Resolve a declared account role secret, written as role.secret_name."""
    role, sep, key = reference.partition('.')
    if not sep:
        key = 'api_key'
    account = remember_account(await asyncio.to_thread(gen.account, role))
    value = (account.get('secrets') or {}).get(key)
    if not isinstance(value, str) or not value:
        raise LookupError(f'No secret {key!r} in account role {role!r}')
    return value


def web_search_account() -> dict | None:
    """Resolve the optional search connection without consulting ambient keys."""
    try:
        account = remember_account(gen.account('web_search'))
    except LookupError:
        return None
    if account.get('protocol') != 'tavily':
        raise ValueError('The selected web_search account requires the tavily protocol')
    return account


# Harnesses whose CLI reads GH_TOKEN/GITHUB_TOKEN as its own login: Copilot
# (after COPILOT_GITHUB_TOKEN, which BYOK mode leaves unset). They never get
# the GitHub CLI's names; git's own header still reaches them.
_GH_LOGIN_EXCLUDED = frozenset({"copilot"})
_GH_LOGIN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")
_SSH_NO_CLIENT = ("The Git account uses an SSH key but this machine has no ssh; git in agent commands reaches "
                  "only public repositories")
_SSH_IN_CONTAINER = ("The Git account uses an SSH key, which only harnesses running on this machine can use: git in "
                     "a container cannot run ssh with it. Run the harness on this machine (docker: none) or bind a "
                     "token account")
# The ssh a host leg's git runs without an SSH account: no config, no agent,
# no key, no prompt, so the user's own keys never authenticate it.
_NO_SSH_KEY = "ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityFile=none"
_OLD_HOST_GIT = ("git on this machine is {version}, older than 2.32, which ignores GIT_CONFIG_GLOBAL: agent commands "
                 "here still read your ~/.gitconfig (its user, url.*.insteadOf, include, http.* and hooks settings)")
_GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
# The git role's account as the session start resolved it (git_session), and
# the session's commands level (deny | ask | allow; the host guard below).
_git: "GitAccount | None" = None
_commands = "deny"
# An SSH account's session key folder (0700, collimate-git-<pid>-*, holding
# the key and known_hosts at 0600) and the GIT_SSH_COMMAND that names them;
# None without an SSH account or without ssh on this machine.
_keys: "Path | None" = None
_ssh_command: "str | None" = None
# Whether the container line has been logged this session; the version of
# this machine's git, read at a deny or ask session's start (git_session) so
# no spawn waits on it; whether the old-host-git line has been logged by
# this process.
_container_told = False
_host_git: "tuple[int, int, int] | None" = None
_old_git_told = False


def git_session(commands: str = "deny") -> "GitAccount | None":
    """Resolve the optional ``git`` role at session start: None when the
    runner binds none. The host already put the account's environment in
    this process's (``secret_environment`` kind ``git``: the scoped Basic
    header, the forge names, the author); this reads the same entry for what
    stays here. Every form of its secret a child, a log or an error could
    carry is registered for redaction before the first spawn. An SSH key is
    written for the session to ``key`` and ``known_hosts`` (0600) in a 0700
    ``collimate-git-<pid>-*`` folder that forget_git_session removes and the
    next Git run or session sweeps after a hard kill; that known_hosts holds
    only the account's own host (GitAccount.agent_known_hosts), so the key
    goes nowhere the engine would not send it. ``commands`` is the session's
    commands level, which sets the host legs' guard; under deny or ask this
    machine's git version is read here, before any spawn, until the
    old-host-git line has been logged. A bound entry that cannot serve
    refuses with the Key manager fix (ValueError)."""
    global _git, _keys, _ssh_command, _commands, _host_git
    from _env_shared import git_run
    from _env_shared.git_account import git_account
    forget_git_session()
    git_run.sweep_stale_sessions()
    account = git_account()
    if account is not None:
        for form in account.secret_forms():
            remember(form)
    _git = account
    _commands = commands
    if commands != "allow" and not _old_git_told:
        _host_git = _host_git_version()
    if account is not None and account.auth == "ssh_key":
        ssh = shutil.which("ssh", path=git_run.search_path())
        if ssh is None:
            gen.log(_SSH_NO_CLIENT)
            return account
        _keys = Path(tempfile.mkdtemp(prefix=f"collimate-git-{os.getpid()}-", dir=tempfile.gettempdir()))
        git_run.write_private(_keys / "key", account.secret)
        git_run.write_private(_keys / "known_hosts",
                              "".join(f"{known.line()}\n" for known in account.agent_known_hosts()))
        _ssh_command = git_run.ssh_command(ssh, _keys / "key", _keys / "known_hosts")
    return account


def forget_git_session() -> None:
    """Drop the session's Git account and remove its SSH key folder (the
    session's ``finally``)."""
    global _git, _keys, _ssh_command, _container_told, _commands, _host_git
    if _keys is not None:
        shutil.rmtree(_keys, ignore_errors=True)
    _git = _keys = _ssh_command = _host_git = None
    _commands = "deny"
    _container_told = False


def _host_git_version() -> "tuple[int, int, int] | None":
    """The version of the git on this machine's PATH, or None when there is none that answers."""
    from _env_shared import git_run
    git = shutil.which("git", path=git_run.search_path())
    if git is None:
        return None
    try:
        done = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=15,
                              env={"PATH": git_run.search_path(), "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    match = _GIT_VERSION.search(done.stdout)
    return (int(match[1]), int(match[2]), int(match[3] or 0)) if match else None


def _tell_old_host_git() -> None:
    """Log once per process that a host git below 2.32 (as git_session read
    it) still reads ~/.gitconfig under the guard."""
    global _old_git_told
    version = _host_git
    if _old_git_told or version is None or version[:2] >= (2, 32):
        return
    _old_git_told = True
    gen.log(_OLD_HOST_GIT.format(version=".".join(str(n) for n in version)))


def git_harness_environment(env: dict, backend: str, locus: str) -> dict:
    """``env``, the whole environment of ``backend``'s harness child at
    ``locus`` ("host" or "container"), which replaces this process's, with
    the run's Git account environment forwarded
    (git_run.forward_git_environment: the host's scoped Basic header, the
    author, the forge names; ``safe.directory=*`` in a container). An
    excluded harness never gets the account's gh names: its own (``env``'s)
    stay as they are.

    On the host, an SSH account adds ``GIT_SSH_COMMAND`` naming the
    session's key file with the engine's hardened ssh options (the key's
    text is in no variable). Every host leg also gets the accident guard,
    so a harness's git does not push with the user's own credentials:
    ``credential.helper=`` after the header, no terminal prompt, without an
    SSH account an ssh with no key, and under commands deny or ask no
    global or system git config (git 2.32+; an older git still reads
    ~/.gitconfig, logged once). The guard is hygiene, not a boundary: HOME
    stays the user's. A container gets no SSH key (its image has no ssh and
    the key file is on this machine), logged once."""
    global _container_told
    from collimate.git_hosts import git_config_env
    from _env_shared import git_run
    if locus not in ("host", "container"):
        raise ValueError(f"unknown harness locus {locus!r}")
    out = git_run.forward_git_environment(env, container=locus == "container")
    if backend in _GH_LOGIN_EXCLUDED:
        for name in _GH_LOGIN_NAMES:
            out.pop(name, None)
            if name in env:
                out[name] = env[name]
    account = _git
    if locus == "container":
        if account is not None and account.auth == "ssh_key" and _ssh_command is not None and not _container_told:
            _container_told = True
            gen.log(_SSH_IN_CONTAINER)
        return out
    out = git_config_env([("credential.helper", "")], out)
    out["GIT_TERMINAL_PROMPT"] = "0"
    if account is not None and account.auth == "ssh_key" and _ssh_command is not None:
        out.update(GIT_SSH_COMMAND=_ssh_command, GIT_SSH_VARIANT="ssh")
    else:
        out["GIT_SSH_COMMAND"] = _NO_SSH_KEY
    if _commands != "allow":
        out.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        _tell_old_host_git()
    return out


def git_secret_names(env: dict) -> "tuple[str, ...]":
    """The names in ``env`` whose value carries a form of the session's Git
    account's secret (its header, a forge token), for a harness that
    redacts named variables."""
    if _git is None:
        return ()
    forms = [form for form in _git.secret_forms() if form]
    return tuple(name for name, value in env.items() if any(form in value for form in forms))
