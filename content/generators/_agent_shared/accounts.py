"""Runner account bindings, resolved once by the host and never prompted for."""
from __future__ import annotations
import asyncio
import os
import shutil
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
    from _env_shared.git_account import GitAccount


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
_SSH_IN_CONTAINER = ("The Git account uses an SSH key, which reaches harnesses running on this machine; in a "
                     "container git reaches only public repositories")
# The git role's account as the session start resolved it (git_session).
_git: "GitAccount | None" = None
# An SSH account's session key folder (0700, collimate-git-<pid>-*, holding
# the key and known_hosts at 0600) and the GIT_SSH_COMMAND that names them;
# None without an SSH account or without ssh on this machine.
_keys: "Path | None" = None
_ssh_command: "str | None" = None
# Whether the container line has been logged this session.
_container_told = False


def git_session() -> "GitAccount | None":
    """Resolve the optional ``git`` role at session start: None when the
    runner binds none. Every form of its secret a child, a log or an error
    could carry is registered for redaction before the first spawn, and the
    account is kept for the harness spawns (git_harness_environment). An SSH
    key is written for the session to ``key`` and ``known_hosts`` (0600) in a
    0700 ``collimate-git-<pid>-*`` folder that forget_git_session removes and
    the next Git run or session sweeps after a hard kill; that known_hosts
    holds only the account's own host (GitAccount.agent_known_hosts), so the
    key goes nowhere the engine would not send it. A bound entry that
    cannot serve refuses with the Key manager fix (ValueError)."""
    global _git, _keys, _ssh_command
    from _env_shared import git_run
    from _env_shared.git_account import git_account
    forget_git_session()
    git_run.sweep_stale_sessions()
    account = git_account()
    if account is not None:
        for form in account.secret_forms():
            remember(form)
    _git = account
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
    global _git, _keys, _ssh_command, _container_told
    if _keys is not None:
        shutil.rmtree(_keys, ignore_errors=True)
    _git = _keys = _ssh_command = None
    _container_told = False


def git_harness_environment(backend: str, locus: str) -> dict:
    """The Git account's environment for ``backend``'s harness running at
    ``locus`` ("host" or "container"): the commit author for every bound
    account; for a token, git's Basic header scoped to the account's base,
    plus the gh names (GitHub) or the glab names (GitLab), the excluded
    harnesses never getting the gh names; for an SSH key on the host,
    ``GIT_SSH_COMMAND`` naming the session's key file with the engine's
    hardened ssh options (the key's text is in no variable) and, on a forge,
    ``COLLIMATE_GIT_HTTPS_BASE``/``COLLIMATE_GIT_SSH_BASE``, which the
    git-checkout skill's recipe alone uses to send an https record under the
    account's base over SSH exactly as git_run.resolve_remote does (no git
    config rewrite, so other git children keep https). A container gets
    only the author for an SSH key (its image has no ssh and the key file is
    on this machine), logged once. Empty when the runner binds no git role."""
    global _container_told
    if locus not in ("host", "container"):
        raise ValueError(f"unknown harness locus {locus!r}")
    account = _git
    if account is None:
        return {}
    name, email = account.identity.name, account.identity.email
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    if account.auth == "ssh_key" and _ssh_command is not None:
        if locus == "host":
            env.update(GIT_SSH_COMMAND=_ssh_command, GIT_SSH_VARIANT="ssh")
            rewrite = account.ssh_rewrite()
            if rewrite is not None:
                ssh_base, https_base = rewrite
                env.update(COLLIMATE_GIT_HTTPS_BASE=https_base, COLLIMATE_GIT_SSH_BASE=ssh_base)
        elif not _container_told:
            _container_told = True
            gen.log(_SSH_IN_CONTAINER)
        return env
    if account.auth not in ("api_key", "basic"):
        return env
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{account.scope()}.extraHeader",
               GIT_CONFIG_VALUE_0=f"Authorization: Basic {account.authorization()}")
    if account.auth != "api_key":
        return env
    if account.service == "gitlab":
        env.update(GITLAB_TOKEN=account.secret, GITLAB_HOST=account.web)
    elif account.service == "github" and backend not in _GH_LOGIN_EXCLUDED:
        if account.web == "https://github.com":
            env.update(GITHUB_TOKEN=account.secret, GH_TOKEN=account.secret)
        else:
            from urllib.parse import urlsplit
            env.update(GH_HOST=urlsplit(account.web).netloc, GH_ENTERPRISE_TOKEN=account.secret,
                       GITHUB_ENTERPRISE_TOKEN=account.secret)
    return env
