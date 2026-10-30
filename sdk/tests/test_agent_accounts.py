"""Selected account metadata is authoritative, with explicit backend limits;
the optional git role carries the Git account into the harness."""

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from collimate.permissions import Grant

from _agent_shared.accounts import model_config
from _agent_shared import parse_agent_config
from tests._git_fixtures import git_account_snapshot, ssh_key
from tests._workspace import GENERATORS

TOKEN = "ghp_agent_s3cret"
GITHUB = git_account_snapshot("github", "api_key", secrets={"api_key": TOKEN})
MODEL = {"service": "openai", "protocol": "openai", "model": "local-model", "endpoint": "http://localhost:1234/v1"}


def profile(**changes):
    return {'service': 'custom', 'protocol': 'openai-compatible',
            'endpoint': 'https://models.example/v1', 'model': 'claude-on-proxy',
            'auth_type': 'api_key', 'secrets': {'api_key': 'private'}, **changes}


def test_custom_endpoint_model_is_never_replaced_by_name_heuristics():
    selected = profile()
    cfg = parse_agent_config({**model_config({'backend': 'pydantic-ai'}, selected), "surface": "annotation"}, grant=Grant.from_dict({}), can_ask=True)
    assert cfg['model'] == 'claude-on-proxy'
    assert cfg['_account'] == selected


def test_cli_does_not_ignore_custom_endpoint():
    with pytest.raises(ValueError, match='custom endpoint'):
        model_config({'backend': 'codex'}, profile())


def test_cli_login_cannot_be_used_by_another_harness():
    account = profile(service='openai', protocol='cli', endpoint='', auth_type='cli',
                      metadata={'plan': 'chatgpt', 'harness': 'codex'})
    with pytest.raises(ValueError, match='belongs to'):
        model_config({'backend': 'opencode'}, account)
    assert model_config({'backend': 'codex'}, account)['auth'] == 'chatgpt'


def test_unauthenticated_connection_requires_explicit_endpoint():
    assert model_config({'backend': 'pydantic-ai'}, profile(auth_type='none'))['auth'] == 'api_key'
    with pytest.raises(ValueError, match='endpoint'):
        model_config({}, profile(auth_type='none', endpoint=''))




def test_cli_login_is_not_treated_as_generic_bearer_auth():
    from _agent_shared.accounts import account_token
    account = profile(auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                      secrets={"access_token": "cli-owned"})
    with pytest.raises(LookupError, match="api_key"):
        account_token(account)


def test_model_subitem_owns_harness_and_model_over_stale_runner_settings():
    selected = profile(kind='model', service='openai', protocol='openai-responses',
                       endpoint='https://api.openai.com/v1', model='saved-model',
                       credential_account_id='openai-account', metadata={'harness': 'codex'})
    cfg = parse_agent_config({**model_config({'backend': 'opencode', 'model': 'old'}, selected), "surface": "annotation"},
                             grant=Grant.from_dict({}), can_ask=True)
    assert cfg['backend'] == 'codex'
    assert cfg['model'] == 'saved-model'
    assert cfg['auth'] == 'api_key'


def test_plain_model_subitem_defaults_to_pydantic_ai():
    cfg = model_config({'backend': 'codex'}, profile(kind='model'))
    assert cfg['backend'] == 'pydantic-ai'
    assert cfg['provider'] == 'openai'


def test_cli_subitem_selects_its_own_login_harness():
    selected = profile(kind='model', service='openai', protocol='cli', endpoint='',
                       auth_type='cli', metadata={'harness': 'codex', 'plan': 'chatgpt'})
    cfg = model_config({'backend': 'pydantic-ai'}, selected)
    assert cfg['backend'] == 'codex'
    assert cfg['auth'] == 'chatgpt'


def test_cli_protocol_rejects_contradictory_api_key_auth():
    selected = profile(kind='model', service='copilot', protocol='cli', endpoint='',
                       auth_type='api_key', metadata={'harness': 'opencode'})
    with pytest.raises(ValueError, match='login authentication'):
        model_config({}, selected)


def test_service_default_endpoint_is_not_silently_replaced_by_cli_provider_endpoint():
    selected = profile(kind='model', service='openai', protocol='anthropic', endpoint='',
                       metadata={'harness': 'claude-code'})
    with pytest.raises(ValueError, match='custom endpoint'):
        model_config({}, selected)


def test_api_harness_metadata_preserves_custom_provider_and_api_key():
    from _agent_shared.accounts import account_token
    selected = profile(kind='model', auth_type='api_key', metadata={'harness': 'pydantic-ai'},
                       secrets={'api_key': 'selected-token'})
    cfg = model_config({}, selected)
    assert cfg['provider'] == 'openai'
    assert cfg['auth'] == 'api_key'
    assert account_token(selected) == 'selected-token'


def test_model_role_rejects_credential_root(monkeypatch):
    from _agent_shared import accounts
    monkeypatch.setattr(accounts.gen, 'account', lambda role: profile(kind='account'))
    with pytest.raises(ValueError, match='model subitem'):
        accounts.model_account()


@pytest.mark.parametrize('protocol,provider', [('anthropic','anthropic'), ('gemini','gemini'), ('openai-responses','openai')])
def test_custom_connection_protocol_drives_provider(protocol, provider):
    selected = profile(protocol=protocol)
    cfg = parse_agent_config({**model_config({}, selected), "surface": "annotation"}, grant=Grant.from_dict({}), can_ask=True)
    assert cfg['provider'] == provider
    assert cfg['model'] == 'claude-on-proxy'


def test_unknown_protocol_is_rejected_before_invocation():
    with pytest.raises(ValueError, match='protocol'):
        model_config({}, profile(service='openai', protocol='unrecognized'))


def test_openai_compatible_service_preserves_selected_model():
    selected = profile(service='xai', model='private-grok', endpoint='')
    cfg = parse_agent_config({**model_config({}, selected), "surface": "annotation"}, grant=Grant.from_dict({}), can_ask=True)
    assert cfg['provider'] == 'xai' and cfg['model'] == 'private-grok'
    from _agent_shared.accounts import model_endpoint
    assert model_endpoint(selected) == 'https://api.x.ai/v1'
    with pytest.raises(ValueError, match='explicit endpoint'):
        model_endpoint(profile(endpoint=''))


def _prepared_spawn(monkeypatch, native_bridge, selected, **ctx):
    """Prepare the pydantic-ai leg for the account bound to the model role,
    spawn it against a recorder, and return the server's environment."""
    import asyncio
    from _agent_shared.harnesses import HARNESS, pipeline
    from _agent_shared.harnesses.pydantic_ai import backend
    native_bridge._rec_accounts['model'] = selected
    spawned = {}

    async def spawn(name, argv, **kwargs):
        import os
        # The key pipe the server alone inherits, read as the server would.
        key = "".join(os.read(fd, 65536).decode() for fd in kwargs.get("pass_fds") or ())
        spawned.update(name=name, argv=argv, **kwargs)
        spawned['env'] = {**kwargs['env'], '_key_on_pipe': key}

    monkeypatch.setattr(backend, 'ensure_python_adapter', lambda name: '/fake/python')
    monkeypatch.setattr(backend, 'spawn_host_adapter', spawn)
    profile = asyncio.run(pipeline.prepare(HARNESS['pydantic-ai'], {
        'auth': 'api_key', 'commands': 'deny', 'model': selected['model'], **ctx})).profile
    asyncio.run(profile.spawn())
    return spawned['env']


@pytest.mark.parametrize('protocol,wire,endpoint', [
    ('openai-compatible', 'openai', 'https://models.example/v1'),
    ('openai-responses', 'openai-responses', 'https://models.example/v1'),
    ('anthropic', 'anthropic', 'https://models.example/v1'),
    ('gemini', 'gemini', 'https://models.example/v1beta'),
])
def test_pai_server_uses_account_model_endpoint_and_token(native_bridge, monkeypatch, protocol, wire, endpoint):
    from _agent_shared import accounts
    selected = profile(kind='model', protocol=protocol, endpoint=endpoint)
    monkeypatch.setenv('OPENAI_API_KEY', 'unselected')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'unselected')
    monkeypatch.setenv('GEMINI_API_KEY', 'unselected')
    env = _prepared_spawn(monkeypatch, native_bridge, selected,
                          provider=accounts.model_provider(selected))
    assert env['COLLIMATE_PAI_WIRE'] == wire
    assert env['COLLIMATE_PAI_ENDPOINT'] == endpoint
    assert env['COLLIMATE_PAI_MODEL'] == selected['model']
    assert env.pop('_key_on_pipe') == 'private'
    assert 'unselected' not in env.values() and 'private' not in env.values()


def test_a_run_leaves_no_provider_key_in_the_process_environment(native_bridge, monkeypatch):
    """The account's key goes to the file the server reads and nowhere else:
    after preparing and spawning the pydantic-ai leg (and a direct key lookup,
    the path every harness profile takes) this process's environment holds no
    provider key it did not hold before, and neither does the server's — so
    no later child (an adapter's npm install, a version probe, a git clone)
    inherits one."""
    import asyncio
    import os

    from _agent_shared import accounts
    from collimate.providers import PROVIDER_KEY_ENV

    for name in PROVIDER_KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    secret = 'sk-run-only-openrouter'
    selected = profile(
        kind='model', service='openrouter', protocol='openai-compatible',
        endpoint='https://openrouter.ai/api/v1', model='some/model',
        secrets={'api_key': secret})
    native_bridge._rec_accounts['model'] = selected
    before = dict(os.environ)
    assert asyncio.run(accounts.account_api_key('openrouter')) == secret
    env = _prepared_spawn(monkeypatch, native_bridge, selected, provider='openrouter')
    assert env.pop('_key_on_pipe') == secret
    assert not any(secret in value for value in env.values())
    assert not set(PROVIDER_KEY_ENV.values()) & set(env)
    assert dict(os.environ) == before
    assert not any(secret in value for value in os.environ.values())


def test_a_blank_account_model_is_the_accounts_default():
    """A model entry saved with no model means the default Key Manager shows
    for it (account_taxonomy.default_model_for), so the harness
    gets a model rather than failing with "No model selected"; a harness that
    keeps its own default under a plan keeps the blank."""
    from collimate.account_taxonomy import PROVIDER_DEFAULT_MODELS, default_model_for

    selected = profile(kind='model', service='openai', protocol='openai',
                       endpoint='https://api.openai.com/v1', model='')
    assert default_model_for(selected) == PROVIDER_DEFAULT_MODELS['openai']
    cfg = parse_agent_config({**model_config({}, selected), "surface": "annotation"}, grant=Grant.from_dict({}), can_ask=True)
    assert (cfg['backend'], cfg['model']) == ('pydantic-ai', PROVIDER_DEFAULT_MODELS['openai'])
    plan = profile(kind='model', service='openai', protocol='cli', endpoint='', model='',
                   auth_type='cli', metadata={'harness': 'cline', 'plan': 'chatgpt'})
    assert default_model_for(plan) == ''
    cfg = parse_agent_config({**model_config({}, plan), "surface": "annotation"}, grant=Grant.from_dict({}), can_ask=True)
    assert (cfg['backend'], cfg['model']) == ('cline', '')
    # An account that names no harness takes the default of the backend the
    # runner's settings chose, not pydantic-ai's: agy's own sentinel, never a
    # gemini-* id its catalog does not serve.
    key = profile(service='gemini', protocol='gemini', endpoint='', model='')
    cfg = parse_agent_config({**model_config({'backend': 'antigravity'}, key), "surface": "annotation"},
                             grant=Grant.from_dict({}), can_ask=True)
    assert (cfg['backend'], cfg['model']) == ('antigravity', 'default')
    cfg = parse_agent_config({**model_config({'backend': 'pydantic-ai'}, key), "surface": "annotation"},
                             grant=Grant.from_dict({}), can_ask=True)
    assert cfg['model'] == PROVIDER_DEFAULT_MODELS['gemini']


# ── the git role ──────────────────────────────────────────────────────


@pytest.fixture
def agent_cell(tmp_path, monkeypatch):
    """``agent_cell(accounts=..., permissions=...)`` binds a host-free run
    whose workspace is ``tmp_path/ws``; the session-resolved Git account is
    reset around the test."""
    import tempfile
    from collimate import native
    from collimate.testing import make_test_bridge
    from _agent_shared import accounts
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    accounts.forget_git_session()
    made: list = []

    def install(*, accounts=None, permissions=None):
        ws = tmp_path / f"ws{len(made) or ''}"
        ws.mkdir()
        bridge = make_test_bridge(ws, accounts=accounts or {},
                                  permissions=permissions or {"internet": "full", "docker": "none"})
        native.set_bridge(bridge)
        made.append(bridge)
        return bridge

    try:
        yield install
    finally:
        accounts.forget_git_session()
        native._clear_bridge()
        for bridge in made:
            bridge.close()


IDENTITY = {"GIT_AUTHOR_NAME": "Ada Lovelace", "GIT_AUTHOR_EMAIL": "ada@example.org",
            "GIT_COMMITTER_NAME": "Ada Lovelace", "GIT_COMMITTER_EMAIL": "ada@example.org"}


def _header(scope: str, user: str, secret: str) -> dict:
    from _env_shared.git_run import basic_authorization
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{scope}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_authorization(user, secret)}"}


def test_git_session_refuses_a_broken_bound_entry_and_is_none_without_role(agent_cell):
    from _agent_shared.accounts import git_session
    agent_cell(accounts={"git": git_account_snapshot(secrets={})})
    with pytest.raises(ValueError, match="has no token; add it in Key manager"):
        git_session()
    agent_cell(accounts={"git": git_account_snapshot(kind="account")})
    with pytest.raises(ValueError, match="needs a GitHub, GitLab or Git server entry"):
        git_session()
    agent_cell(accounts={})
    assert git_session() is None


def test_git_session_registers_every_secret_form_for_redaction(agent_cell):
    import json
    from _agent_shared import secret_hygiene
    from _agent_shared.accounts import git_harness_environment, git_session
    from _env_shared.git_run import basic_authorization
    agent_cell(accounts={"git": GITHUB})
    account = git_session()
    assert account.secret == TOKEN
    header = git_harness_environment("claude-code", "host")["GIT_CONFIG_VALUE_0"]
    for form in (TOKEN, basic_authorization("x-access-token", TOKEN), header, *account.secret_forms()):
        assert "[redacted]" in secret_hygiene.scrub(f"leak {form} here"), form
        assert form not in secret_hygiene.scrub(f"leak {form} here")
    key = ssh_key()
    agent_cell(accounts={"git": git_account_snapshot("git", "ssh_key", metadata={
        "known_hosts": "box.example.org ssh-ed25519 " + key_blob()})})
    account = git_session()
    for form in (key, key.rstrip("\n"), json.dumps(key)[1:-1], *account.secret_forms()):
        scrubbed = secret_hygiene.scrub(f"leak {form} here")
        assert "[redacted]" in scrubbed and form not in scrubbed


def key_blob() -> str:
    return ssh_key("ed25519.pub").split()[1]


@pytest.mark.parametrize("locus", ["host", "container"])
@pytest.mark.parametrize("backend", ["claude-code", "pydantic-ai", "copilot"])
def test_git_harness_environment_matches_the_table(agent_cell, fake_ssh, backend, locus):
    from _agent_shared.accounts import _GH_LOGIN_NAMES, git_harness_environment, git_session
    copilot = backend == "copilot"
    agent_cell(accounts={"git": GITHUB})
    assert git_harness_environment(backend, locus) == {}  # nothing before the session start resolves it
    rows = [
        (GITHUB, {**IDENTITY, **_header("https://github.com/", "x-access-token", TOKEN),
                  **({} if copilot else {"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN})}),
        (git_account_snapshot("github", "api_key", endpoint="https://ghe.example.com"),
         {**IDENTITY, **_header("https://ghe.example.com/", "x-access-token", "ghp_fake"),
          **({} if copilot else {"GH_HOST": "ghe.example.com", "GH_ENTERPRISE_TOKEN": "ghp_fake",
                                 "GITHUB_ENTERPRISE_TOKEN": "ghp_fake"})}),
        (git_account_snapshot("gitlab", "api_key", endpoint="https://example.com/gitlab"),
         {**IDENTITY, **_header("https://example.com/gitlab/", "oauth2", "glpat-fake"),
          "GITLAB_TOKEN": "glpat-fake", "GITLAB_HOST": "https://example.com/gitlab"}),
        (git_account_snapshot("git", "basic"),
         {**IDENTITY, **_header("https://git.example.org/", "ada", "pw-fake")}),
        (git_account_snapshot("github", "ssh_key"), IDENTITY),
        (git_account_snapshot("git", "none"), IDENTITY),
    ]
    for snapshot, expected in rows:
        agent_cell(accounts={"git": snapshot})
        git_session()
        env = git_harness_environment(backend, locus)
        if snapshot["auth_type"] == "ssh_key" and locus == "host":
            assert env.pop("GIT_SSH_VARIANT") == "ssh"
            assert env.pop("GIT_SSH_COMMAND").startswith(f"{fake_ssh.command()} -F /dev/null -i ")
            assert env.pop("COLLIMATE_GIT_HTTPS_BASE") == "https://github.com/"
            assert env.pop("COLLIMATE_GIT_SSH_BASE") == "ssh://git@github.com/"
        assert env == expected, (snapshot["service"], snapshot["auth_type"])
        if copilot:
            assert not set(_GH_LOGIN_NAMES) & env.keys()
        assert not any("PRIVATE KEY" in value or "GIT_SSH" in name for name, value in env.items())
    agent_cell(accounts={})
    git_session()
    assert git_harness_environment(backend, locus) == {}


def test_harness_environment_refuses_an_unknown_locus(agent_cell):
    from _agent_shared.accounts import git_harness_environment
    with pytest.raises(ValueError, match="unknown harness locus"):
        git_harness_environment("claude-code", "cloud")


BOX_HOSTS = "[box.example.org]:2222 ssh-ed25519 "


def _ssh_session(agent_cell, logged=None, monkeypatch=None):
    """Bind a Git server SSH account whose own known_hosts line names
    box.example.org:2222 and start the session; ``logged`` collects gen.log."""
    from _agent_shared import accounts
    agent_cell(accounts={"git": git_account_snapshot("git", "ssh_key",
                                                     metadata={"known_hosts": BOX_HOSTS + key_blob()})})
    if logged is not None:
        monkeypatch.setattr(accounts.gen, "log", logged.append)
    return accounts.git_session()


def test_ssh_key_reaches_a_host_harness_as_the_hardened_ssh_command(agent_cell, fake_ssh, monkeypatch):
    import os
    import shlex
    import stat
    import tempfile
    from pathlib import Path
    from collimate import git_hosts
    from _agent_shared import accounts
    from _env_shared import git_run
    logged: list = []
    account = _ssh_session(agent_cell, logged, monkeypatch)
    assert account.secret == git_hosts.normalise_private_key(ssh_key())
    env = accounts.git_harness_environment("claude-code", "host")
    command = env.pop("GIT_SSH_COMMAND")
    assert env == {**IDENTITY, "GIT_SSH_VARIANT": "ssh"}
    words = shlex.split(command)
    key = Path(words[4])
    known = Path(next(w.split("=", 1)[1] for w in words if w.startswith("UserKnownHostsFile=")))
    folder = key.parent
    assert command == git_run.ssh_command(fake_ssh.command(), key, known)
    assert known.parent == folder and folder.parent == Path(tempfile.gettempdir())
    assert git_run._SESSION.fullmatch(folder.name) and folder.name.startswith(f"collimate-git-{os.getpid()}-")
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
    assert sorted(p.name for p in folder.iterdir()) == ["key", "known_hosts"]
    for path in (key, known):
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600 and not path.is_symlink()
    assert key.read_text() == account.secret
    assert known.read_text().splitlines() == [BOX_HOSTS + key_blob()]
    assert "StrictHostKeyChecking=yes" in words and "IdentityAgent=none" in words
    for form in account.secret_forms():
        assert form not in command
    assert logged == []
    accounts.forget_git_session()
    assert not folder.exists()
    assert accounts.git_harness_environment("claude-code", "host") == {}


def _session_known_hosts(env: dict) -> list[str]:
    import shlex
    from pathlib import Path
    words = shlex.split(env["GIT_SSH_COMMAND"])
    known = Path(next(w.split("=", 1)[1] for w in words if w.startswith("UserKnownHostsFile=")))
    return known.read_text().splitlines()


@pytest.mark.parametrize("service, endpoint, host", [
    ("github", None, "github.com"),
    ("gitlab", None, "gitlab.com"),
])
def test_a_forge_sessions_known_hosts_trusts_only_its_own_host(agent_cell, fake_ssh, service, endpoint, host):
    """The agent's ssh has no auth_for in front of it: with both shipped
    forges in its known_hosts, a GitHub account's key would be offered to
    gitlab.com. Only the account's web host is trusted, under that name."""
    from collimate import git_hosts
    from _agent_shared import accounts
    agent_cell(accounts={"git": git_account_snapshot(service, "ssh_key")})
    accounts.git_session()
    lines = _session_known_hosts(accounts.git_harness_environment("claude-code", "host"))
    pinned = [k for k in git_hosts.pinned_known_hosts() if host in k.hosts]
    assert pinned and lines == [f"{host} {k.keytype} {k.blob}" for k in pinned]
    other = "gitlab.com" if host == "github.com" else "github.com"
    assert not any(other in line.split(" ", 1)[0].split(",") for line in lines)
    accounts.forget_git_session()


def test_a_self_managed_forge_trusts_its_own_lines_for_its_web_host_only(agent_cell, fake_ssh):
    """GitLab under a port and a path: the pasted line for its web host is
    kept, a pasted line for another host and both shipped forges are not."""
    from _agent_shared import accounts
    blob = key_blob()
    agent_cell(accounts={"git": git_account_snapshot("gitlab", "ssh_key", endpoint="https://gitlab.example.com:8443/git",
                                                     metadata={"known_hosts": f"gitlab.example.com ssh-ed25519 {blob}\n"
                                                                              f"other.example.org ssh-ed25519 {blob}"})})
    accounts.git_session()
    env = accounts.git_harness_environment("claude-code", "host")
    assert _session_known_hosts(env) == [f"gitlab.example.com ssh-ed25519 {blob}"]
    assert (env["COLLIMATE_GIT_HTTPS_BASE"], env["COLLIMATE_GIT_SSH_BASE"]) == (
        "https://gitlab.example.com:8443/git/", "ssh://git@gitlab.example.com/")
    accounts.forget_git_session()


def test_a_git_server_session_trusts_no_shipped_forge(agent_cell, fake_ssh):
    from _agent_shared import accounts
    _ssh_session(agent_cell)
    env = accounts.git_harness_environment("claude-code", "host")
    assert _session_known_hosts(env) == [BOX_HOSTS + key_blob()]
    assert not {"COLLIMATE_GIT_HTTPS_BASE", "COLLIMATE_GIT_SSH_BASE"} & env.keys()
    accounts.forget_git_session()


def test_ssh_key_in_a_container_gives_identity_only_and_logs_once(agent_cell, fake_ssh, monkeypatch):
    from _agent_shared import accounts
    logged: list = []
    _ssh_session(agent_cell, logged, monkeypatch)
    assert logged == []                                # the locus is not known at session start
    assert accounts.git_harness_environment("claude-code", "container") == IDENTITY
    assert accounts.git_harness_environment("copilot", "container") == IDENTITY
    assert "GIT_SSH_COMMAND" in accounts.git_harness_environment("claude-code", "host")
    assert logged == ["The Git account uses an SSH key, which reaches harnesses running on this machine; in a "
                      "container git reaches only public repositories"]


def test_ssh_key_without_ssh_on_this_machine_gives_identity_only(agent_cell, tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path
    from _agent_shared import accounts
    empty = tmp_path / "no-ssh-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    logged: list = []
    _ssh_session(agent_cell, logged, monkeypatch)
    assert accounts.git_harness_environment("claude-code", "host") == IDENTITY
    assert accounts.git_harness_environment("claude-code", "container") == IDENTITY
    assert logged == ["The Git account uses an SSH key but this machine has no ssh; git in agent commands "
                      "reaches only public repositories"]
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_session_start_sweeps_a_dead_sessions_key(agent_cell, fake_ssh):
    import subprocess
    import sys
    import tempfile
    from pathlib import Path
    from _agent_shared import accounts
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    stale = Path(tempfile.gettempdir()) / f"collimate-git-{child.pid}-abc123"
    stale.mkdir(mode=0o700)
    (stale / "key").write_text(ssh_key())
    agent_cell(accounts={"git": GITHUB})
    accounts.git_session()
    assert not stale.exists()


def test_the_key_text_is_in_no_harness_value(agent_cell, fake_ssh, monkeypatch):
    import json
    from _agent_shared import accounts
    logged: list = []
    account = _ssh_session(agent_cell, logged, monkeypatch)
    values = [v for locus in ("host", "container") for backend in ("claude-code", "copilot")
              for v in accounts.git_harness_environment(backend, locus).values()]
    forms = (*account.secret_forms(), json.dumps(account.secret)[1:-1])
    for form in forms:
        assert not any(form in value for value in [*values, *logged]), form


def test_forget_git_session_clears_it_in_finally(agent_cell, monkeypatch):
    from _agent_shared import accounts, session
    agent_cell(accounts={"git": GITHUB})
    entry = GENERATORS / "agent/agent/agent.py"
    spec = importlib.util.spec_from_file_location("git_role_forget_entry", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen: list = []

    async def run_session(cfg):
        seen.append(accounts.git_harness_environment("claude-code", "host").get("GITHUB_TOKEN"))
        raise RuntimeError("the harness stopped")

    monkeypatch.setattr(accounts, "model_account", lambda: MODEL)
    monkeypatch.setattr(session.gen, "config", lambda _: {"surface": "annotation"})
    monkeypatch.setattr(session, "stage_builtin_skills", lambda *a, **k: [])
    monkeypatch.setattr(session.entry, "run_session", run_session)
    with pytest.raises(RuntimeError, match="the harness stopped"):
        asyncio.run(module.session.run(here=str(entry.parent)))
    assert seen == [TOKEN]
    assert accounts._git is None and accounts.git_harness_environment("claude-code", "host") == {}


def test_the_session_key_folder_goes_in_the_sessions_finally(agent_cell, monkeypatch, fake_ssh):
    from pathlib import Path
    from _agent_shared import accounts, session
    agent_cell(accounts={"git": git_account_snapshot("github", "ssh_key")})
    entry = GENERATORS / "agent/agent/agent.py"
    spec = importlib.util.spec_from_file_location("git_role_key_entry", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folders: list = []

    async def run_session(cfg):
        folders.append(accounts._keys)
        assert (Path(accounts._keys) / "key").is_file()
        raise RuntimeError("the harness stopped")

    monkeypatch.setattr(accounts, "model_account", lambda: MODEL)
    monkeypatch.setattr(session.gen, "config", lambda _: {"surface": "annotation"})
    monkeypatch.setattr(session, "stage_builtin_skills", lambda *a, **k: [])
    monkeypatch.setattr(session.entry, "run_session", run_session)
    with pytest.raises(RuntimeError, match="the harness stopped"):
        asyncio.run(module.session.run(here=str(entry.parent)))
    assert len(folders) == 1 and not folders[0].exists()
    assert accounts._keys is None and accounts._ssh_command is None


class _Spawned(Exception):
    """Stops a spawn once its environment is captured."""


def test_host_and_container_adapters_and_terminal_get_the_git_environment(agent_cell, monkeypatch, tmp_path,
                                                                          fake_ssh):
    from _agent_shared import accounts, acp_client, terminal_runner
    from _agent_shared.harnesses import Terminal
    from _agent_shared.harnesses import pipeline
    from _agent_shared.accounts import git_session
    agent_cell(accounts={"git": GITHUB})
    git_session()
    seen: dict = {}

    async def create_subprocess_exec(*argv, env, **kwargs):
        seen["host"] = env
        raise _Spawned

    def attached_exec(container_id, argv, pidfile, *, stdin, environment, workdir):
        seen["container"] = environment
        raise _Spawned

    @asynccontextmanager
    async def serve_terminal(container_id, argv, *, env, **kwargs):
        seen["terminal"] = env
        raise _Spawned
        yield

    launch = {"now": terminal_runner.TerminalLaunch(None, ("cli",), str(tmp_path), {"PATH": "/usr/bin"}, "CLI",
                                                    [], "ready", "")}

    @asynccontextmanager
    async def native_context(prepared, request, bridge):
        yield launch["now"]

    async def prepare(row, cfg, **kwargs):
        return SimpleNamespace()

    async def commit(**kwargs):
        return None

    facet = Terminal(tier="C", native_context="fake:native", bridge_enabled=False, default_label="CLI",
                     locus="host")
    monkeypatch.setattr(acp_client, "register_adapter_log", lambda backend: str(tmp_path / "adapter.log"))
    monkeypatch.setattr(acp_client.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(acp_client, "_attached_exec", attached_exec)
    monkeypatch.setattr(terminal_runner, "HARNESS", {
        b: SimpleNamespace(id=b, terminal=facet) for b in ("claude-code", "copilot")})
    monkeypatch.setattr(terminal_runner, "resolve", lambda target: native_context)
    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(terminal_runner.gen, "commit", commit)
    monkeypatch.setattr(terminal_runner.gen, "phase", lambda *args: None)
    monkeypatch.setattr(terminal_runner.gen, "serve_terminal", serve_terminal)

    def spawn_all(backend: str) -> dict:
        seen.clear()
        with pytest.raises(_Spawned):
            asyncio.run(acp_client.spawn_host_adapter(backend, ["adapter"], env={"PATH": "/usr/bin"},
                                                      cwd=str(tmp_path)))
        with pytest.raises(_Spawned):
            asyncio.run(acp_client.spawn_container_adapter(backend, SimpleNamespace(container_id="c1"),
                                                           ["adapter"], env_vars={"PATH": "/usr/bin"}))
        with pytest.raises(_Spawned):
            asyncio.run(terminal_runner.run_terminal({"backend": backend}, pointer="own.terminal"))
        return dict(seen)

    shared = {**IDENTITY, **_header("https://github.com/", "x-access-token", TOKEN), "PATH": "/usr/bin"}
    claude = spawn_all("claude-code")
    assert set(claude) == {"host", "container", "terminal"}
    for where, env in claude.items():
        assert env == {**shared, "GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN}, where
    copilot = spawn_all("copilot")
    assert set(copilot) == {"host", "container", "terminal"}
    for where, env in copilot.items():
        assert env == shared, where

    # An SSH account: the host adapter and a terminal on this machine get the
    # session's ssh command; the container adapter and a container terminal
    # get the identity only.
    agent_cell(accounts={"git": git_account_snapshot("github", "ssh_key")})
    git_session()
    ssh = {**IDENTITY, "GIT_SSH_COMMAND": accounts._ssh_command, "GIT_SSH_VARIANT": "ssh", "PATH": "/usr/bin",
           "COLLIMATE_GIT_HTTPS_BASE": "https://github.com/", "COLLIMATE_GIT_SSH_BASE": "ssh://git@github.com/"}
    assert accounts._ssh_command.startswith(fake_ssh.command())
    claude = spawn_all("claude-code")
    assert claude == {"host": ssh, "terminal": ssh, "container": {**IDENTITY, "PATH": "/usr/bin"}}
    launch["now"] = terminal_runner.TerminalLaunch("c1", ("cli",), "/workspace", {"PATH": "/usr/bin"}, "CLI", [],
                                                   "ready", "")
    monkeypatch.setattr(terminal_runner, "HARNESS", {"claude-code": SimpleNamespace(id="claude-code", terminal=Terminal(
        tier="C", native_context="fake:native", bridge_enabled=False, default_label="CLI", locus="container"))})
    assert spawn_all("claude-code")["terminal"] == {**IDENTITY, "PATH": "/usr/bin"}


def test_session_start_refuses_a_broken_git_entry_before_the_harness(agent_cell, monkeypatch):
    from _agent_shared import accounts, session
    agent_cell(accounts={"git": git_account_snapshot(secrets={})})
    entry = GENERATORS / "agent/agent/agent.py"
    spec = importlib.util.spec_from_file_location("git_role_entry", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(accounts, "model_account", lambda: MODEL)
    monkeypatch.setattr(session.gen, "config", lambda _: {"surface": "annotation"})
    monkeypatch.setattr(session, "stage_builtin_skills", lambda *a, **k: [])
    monkeypatch.setattr(session.entry, "run_session", lambda *a, **k: pytest.fail("session before the git role"))
    with pytest.raises(ValueError, match="has no token; add it in Key manager"):
        asyncio.run(module.session.run(here=str(entry.parent)))
