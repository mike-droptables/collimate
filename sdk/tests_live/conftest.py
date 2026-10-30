# SPDX-License-Identifier: MIT
"""Fixtures + provider discovery for the LIVE agent suite.

Everything here answers one question per test: *may this run, and against
what?* The answer comes from the environment alone — nothing in this tree may
carry a credential, and a provider with no key must SKIP with a reason rather
than fail (a red suite for "I only have one key" would train people to ignore
it).

Selection contract
------------------
* ``<PROVIDER>_API_KEY`` (exactly :data:`collimate.providers.PROVIDER_KEY_ENV`)
  opts a provider in.
* ``COLLIMATE_LIVE_CLAUDE_CODE=1`` opts into subscription-auth claude-code
  tests (the logged-in local ``claude`` CLI; no key involved).
* ``COLLIMATE_LIVE_DOCKER=1`` opts into the container-harness tests, which may
  build images on first run (minutes, and network).
* ``COLLIMATE_LIVE_LOGIN_<HARNESS>=<path>`` (``COLLIMATE_LIVE_LOGIN_ANTIGRAVITY``
  and so on) names a plan login document on this machine; a plan leg leases a
  copy of it (:func:`plan_login_lease`).
* ``COLLIMATE_LIVE_UNPINNED=1`` runs every harness at the release a user's run
  gets today (:data:`UNPINNED`).
* ``COLLIMATE_LIVE_MODEL_<PROVIDER>`` / ``COLLIMATE_LIVE_MULTIMODAL_MODEL_<PROVIDER>``
  override the model a provider is exercised with.

How a run is pointed at a provider
----------------------------------
The agent reads its harness, provider, model, endpoint and credential from the
account bound to the ``model`` role — never from the config. So every live run
binds one: :func:`model_account` builds it from the provider's env key, and
:func:`run_agent_generator` passes it as ``accounts={"model": …}``. An
engine-level turn (``eng.run_agent`` against a bridge fixture) binds it with
:func:`bind_model_account`.

Why the helpers below exist
---------------------------
* The image builders are *in code* (zlib/struct/LZW) so this tree ships no
  binary fixtures: a checked-in PNG is a thing reviewers can't read, and the
  colour a test asserts on must be visible in the source that asserts it.
* :func:`docker_env_bridge` turns the RecordingBridge's env plane into a REAL
  docker driver. The recording bridge answers ``env_place`` with a synthetic
  ack and ``env_exec`` with ``rc=0, output=""`` — perfect for structural unit
  tests, and utterly useless here: a containerized harness would "pass"
  without a container ever existing. Placement/exec/build is normally the
  HOST's job (the collimate app), so the live suite has to supply that half
  itself.
* :func:`run_chat_generator` re-times the scripted chat queue: load-bearing
  rather than tidy — its comment says why.
"""
from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
import signal
import subprocess
import time
import os
import shutil
import struct
import sys
import uuid
import zlib

import pytest
import yaml

from collimate.providers import PROVIDER_DEFAULT_MODELS, PROVIDER_KEY_ENV

# The engine is generator-side shared code; import it the way the Agent
# generators do (sibling on the generators path) — same pattern as
# tests/test_agent_engine.py. Done at import time so the module object is
# already in sys.modules when run_generator executes an Agent entrypoint: the harness
# evicts only modules the RUN imported, so a pre-imported engine survives and
# the test and the generator share one instance.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The first-party content sits beside the SDK member, at the workspace root.
_WORKSPACE = os.path.dirname(_REPO)
_GEN_DIR = os.path.join(_WORKSPACE, "content", "generators")
sys.path.insert(0, _GEN_DIR)
import _agent_shared as eng  # noqa: E402

from collimate.testing import run_generator  # noqa: E402
from collimate.transcript import parse_transcript  # noqa: E402

#: The Agent cell's real generator's entrypoint — every generator-level live
#: test runs THIS file, not a synthetic stand-in.
AGENT_ENTRYPOINT = os.path.join(_GEN_DIR, "agent", "agent", "agent.py")


def _agent_ui_files() -> list:
    """The generator manifest's ``ui_files`` — what the host passes a run, so
    a live run refuses an undeclared UI file type as a hosted one does."""
    import yaml

    folder = os.path.dirname(AGENT_ENTRYPOINT)
    with open(os.path.join(folder, "agent.generator"), encoding="utf-8") as fh:
        return list((yaml.safe_load(fh) or {}).get("ui_files") or [])


# ══════════════════════════════════════════════════════════════════════
# A live run's surface files: all in its run folder, named by its label.
# ══════════════════════════════════════════════════════════════════════

# The label every live agent run is given (a synthetic runner id and run id),
# so a test can name the run's surface files before and after the run.
LIVE_LABEL = "live-0badc0de"


def run_folder(label: str = LIVE_LABEL) -> str:
    """The run folder ``_runs/<label>`` holding a run's surface files."""
    return f"_runs/{label}"


def chat_pointer(label: str = LIVE_LABEL) -> str:
    return f"{run_folder(label)}/chat/conversation.chat"


def chat_transcript(label: str = LIVE_LABEL, thread: str = "main") -> str:
    """A chat run's open-format transcript (``chat/<thread>.json``)."""
    return f"{run_folder(label)}/chat/{thread}.json"


def terminal_pointer(backend: str, label: str = LIVE_LABEL) -> str:
    """The ``.terminal`` pointer a terminal run of ``backend`` serves."""
    return f"{run_folder(label)}/terminal/{backend}.terminal"


# ══════════════════════════════════════════════════════════════════════
# Provider discovery
# ══════════════════════════════════════════════════════════════════════

# The canonical env-var names, one per key-billed provider of the registry.
KEY_ENV: dict = dict(PROVIDER_KEY_ENV)
LIVE_PROVIDERS: tuple = tuple(KEY_ENV)

# Multimodal model per provider — the image tests need a model that can SEE.
# Defaults track what each provider's flagship actually accepts:
#   anthropic / openai / gemini — natively multimodal at their default model,
#     so the ordinary default is reused;
#   cerebras — gpt-oss-120b (the text default) is text-only, so vision goes to
#     qwen-3.8-27b, the catalog's text+vision model (gemma-4-31b, the old
#     vision tier, is archived: a 404 on every call);
#   openrouter — its default slug IS sonnet, i.e. multimodal;
#   groq — nothing vision-capable in the default lineup, so it stays blank and
#     its image tests SKIP until COLLIMATE_LIVE_MULTIMODAL_MODEL_GROQ names one.
_MULTIMODAL_DEFAULT: dict = {
    "anthropic": PROVIDER_DEFAULT_MODELS["anthropic"],
    "openai": PROVIDER_DEFAULT_MODELS["openai"],
    "gemini": PROVIDER_DEFAULT_MODELS["gemini"],
    "cerebras": "qwen-3.8-27b",
    "openrouter": PROVIDER_DEFAULT_MODELS["openrouter"],
    "groq": "",
}

# Providers documented to accept ``image/gif`` on the wire. Gemini's inline-data
# vocabulary is png/jpeg/webp/heic/heif — no GIF — and the cerebras vision tier
# makes no claim either, so the format-variance test covers only these.
GIF_PROVIDERS: frozenset = frozenset({"anthropic", "openai", "openrouter"})


def _flag(name: str) -> bool:
    """An opt-in env flag: set and not an explicit falsey word."""
    return (os.environ.get(name) or "").strip().lower() not in ("", "0", "false", "no", "off")


def has_key(provider: str) -> bool:
    return bool((os.environ.get(KEY_ENV[provider]) or "").strip())


def model_for(provider: str) -> str:
    """The text model a provider is exercised with — the engine's own default
    unless ``COLLIMATE_LIVE_MODEL_<PROVIDER>`` overrides it. Reading the default
    from ``eng.PROVIDER_DEFAULT_MODELS`` (rather than restating it) means a
    lineup rotation moves the live suite with the engine."""
    override = (os.environ.get(f"COLLIMATE_LIVE_MODEL_{provider.upper()}") or "").strip()
    return override or eng.PROVIDER_DEFAULT_MODELS[provider]


def multimodal_model_for(provider: str) -> str:
    """The vision model for a provider; ``""`` when none is configured (its
    image tests then skip)."""
    override = (
        os.environ.get(f"COLLIMATE_LIVE_MULTIMODAL_MODEL_{provider.upper()}") or ""
    ).strip()
    return override or _MULTIMODAL_DEFAULT.get(provider, "")


# Cerebras retires models outright (an archived model 404s for everyone), and
# its public catalog needs no key — so a stale default or override is reported
# as a SKIP naming the fix, not a wall of red that reads like an engine bug.
# Only a model MISSING from a catalog we actually fetched skips: an unreachable
# catalog skips nothing, and a 404 at turn time still fails, so a real
# model-resolution bug in the engine is never hidden.
CEREBRAS_CATALOG_URL = "https://api.cerebras.ai/public/v1/models"
_catalogs: dict = {}


def cerebras_catalog() -> "frozenset | None":
    """The model ids Cerebras serves now, or ``None`` when the catalog could
    not be read (cached for the session)."""
    if "cerebras" not in _catalogs:
        import json
        import urllib.request
        try:
            # The edge answers urllib's default User-Agent with a 403.
            req = urllib.request.Request(CEREBRAS_CATALOG_URL,
                                         headers={"User-Agent": "collimate-live-suite"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r).get("data") or []
            _catalogs["cerebras"] = frozenset(
                m.get("id") for m in data if isinstance(m, dict) and m.get("id"))
        except Exception:  # noqa: BLE001 — offline / catalog moved: check nothing
            _catalogs["cerebras"] = None
    return _catalogs["cerebras"]


def unserved_reason(provider: str, model: str) -> str:
    """Why ``model`` can't run on ``provider`` per its live catalog, or ``""``."""
    if provider != "cerebras" or not model or not has_key(provider):
        return ""
    served = cerebras_catalog()
    if served is None or model in served:
        return ""
    return (f"cerebras no longer serves {model} (served: {', '.join(sorted(served))}) — "
            f"set COLLIMATE_LIVE_MODEL_CEREBRAS / COLLIMATE_LIVE_MULTIMODAL_MODEL_CEREBRAS")


def provider_params(providers=LIVE_PROVIDERS) -> list:
    """``pytest.param`` rows for a per-provider parametrize, each carrying its
    own skip mark so an absent key reports as SKIPPED with the env var named."""
    return [
        pytest.param(
            p,
            marks=pytest.mark.skipif(not has_key(p), reason=f"{KEY_ENV[p]} is not set"),
        )
        for p in providers
    ]


def multimodal_params(providers=LIVE_PROVIDERS) -> list:
    """Like :func:`provider_params`, but also skips a provider whose multimodal
    model isn't configured — the two reasons are reported separately so a skip
    never hides which half is missing."""
    out: list = []
    for p in providers:
        if not has_key(p):
            reason = f"{KEY_ENV[p]} is not set"
        elif not multimodal_model_for(p):
            reason = (
                f"no multimodal model for {p} — set "
                f"COLLIMATE_LIVE_MULTIMODAL_MODEL_{p.upper()}"
            )
        else:
            reason = unserved_reason(p, multimodal_model_for(p))
        out.append(pytest.param(p, marks=pytest.mark.skipif(bool(reason), reason=reason)))
    return out


def first_provider(*candidates: str) -> str:
    """The first candidate provider with a key, or ``""``. Used by the CLI
    harness tests, which bill ONE turn to whichever key the machine happens to
    have rather than fanning out over every provider."""
    for p in candidates:
        if p in KEY_ENV and has_key(p):
            return p
    return ""


# ── coarse opt-in gates ──────────────────────────────────────────────

CLAUDE_CODE_ENABLED = _flag("COLLIMATE_LIVE_CLAUDE_CODE")
DOCKER_ENABLED = _flag("COLLIMATE_LIVE_DOCKER")
#: Every harness at the release a user's run gets today: the container images
#: float to their latest release (a bridge's packages default to ``{}``), and
#: a host adapter floats the way its named configuration asks (:func:`preset_packages`).
UNPINNED = _flag("COLLIMATE_LIVE_UNPINNED")


def preset_packages(backend: str) -> dict:
    """The ``packages`` the Agent generator's shipped ``<backend>`` named
    configuration asks for — what ``gen.packages()`` returns in a user's run
    of that harness (``{"codex-acp": "latest", …}``)."""
    path = os.path.join(_GEN_DIR, "agent", "agent", "configs", backend,
                        "manifest.config_yaml")
    with open(path, encoding="utf-8") as fh:
        return dict((yaml.safe_load(fh) or {}).get("packages") or {})


def docker_available() -> bool:
    """Docker opted into AND actually reachable. The daemon ping is guarded by
    the flag so a machine that never opted in pays nothing at collection."""
    if not DOCKER_ENABLED:
        return False
    try:
        import docker

        docker.from_env().ping()
    except Exception:  # noqa: BLE001 — any failure to reach dockerd is "no docker"
        return False
    return True


requires_docker = pytest.mark.skipif(
    not docker_available(),
    reason="set COLLIMATE_LIVE_DOCKER=1 and have a reachable docker daemon",
)
requires_claude_code = pytest.mark.skipif(
    not CLAUDE_CODE_ENABLED,
    reason="set COLLIMATE_LIVE_CLAUDE_CODE=1 to use the logged-in claude CLI",
)


# ══════════════════════════════════════════════════════════════════════
# Bridge / run helpers
# ══════════════════════════════════════════════════════════════════════


# The running test's event loop (one_loop_per_test).
_TEST_LOOP: list = []


@pytest.fixture
def one_loop_per_test():
    """Every turn of the test runs on one event loop (:func:`run_on_test_loop`),
    as a run's turns do, and the test ends the way the generator's run does:
    the ACP sessions close on that loop — each adapter child exits and its
    subprocess transport closes while the loop still runs — then the
    containers and bridges go, and only then does the loop end. A cached
    AcpSession would otherwise leak its subprocess into the next test, and a
    transport collected after its loop closed raises "Event loop is closed"."""
    from _agent_shared.acp_run import close_acp_sessions
    from _agent_shared.cli_container import close_all

    async def _close_sessions() -> None:
        await close_acp_sessions()
        # One more turn of the loop for the exits the child watcher reports
        # and the pipes' end of file, so every subprocess transport closes here.
        await asyncio.sleep(0.2)

    runner = asyncio.Runner()
    _TEST_LOOP.append(runner)
    try:
        yield runner
    finally:
        _TEST_LOOP.remove(runner)
        try:
            runner.run(_close_sessions())
        finally:
            close_all()
            runner.close()


def run_on_test_loop(coro):
    """Run ``coro`` on the test's one loop; the test must use
    :func:`one_loop_per_test`."""
    if not _TEST_LOOP:
        raise RuntimeError("run_on_test_loop needs the one_loop_per_test fixture")
    return _TEST_LOOP[-1].run(coro)


def wire_protocol(provider: str) -> str:
    """The protocol a provider's model account speaks — its own wire for the
    two non-OpenAI shapes, the OpenAI wire for every other service (there the
    ``service`` is what tells them apart, which is how
    ``model_account_provider`` reads the provider back off)."""
    return {"anthropic": "anthropic", "gemini": "gemini"}.get(provider, "openai")


#: Where a plan's own token comes from in this tree. A CLI login has no API
#: key: the harness reads the token the sign-in produced off the ACCOUNT, so
#: the suite hands it the one its gate already requires.
PLAN_TOKEN_ENV: dict = {"claude_code": "CLAUDE_CODE_OAUTH_TOKEN"}


def model_account(provider: str, *, model: str = "", harness: str = "pydantic-ai",
                  auth_type: str = "api_key", plan: str = "") -> dict:
    """The ``model`` account a live run binds — the same shape Key Manager
    saves for a model entry under a credential account.

    The account is authoritative for the run: ``harness`` becomes the backend,
    ``model`` the model, and the secret the credential (read from the
    environment, the one place this tree may take one from). A CLI login is
    expressed the way the product does — ``auth_type`` ``subscription`` plus
    the harness's ``plan`` — and carries that plan's token instead of a key."""
    login = bool(plan) or auth_type in ("cli", "subscription", "oauth", "device")
    metadata = {"harness": harness}
    if plan:
        metadata["plan"] = plan
    if login:
        token = (os.environ.get(PLAN_TOKEN_ENV.get(plan, "")) or "").strip()
        secrets = {"oauth_token": token} if token else {}
    else:
        secrets = {"api_key": os.environ.get(KEY_ENV[provider], "")}
    return {
        "kind": "model",
        "service": provider,
        "protocol": "cli" if login else wire_protocol(provider),
        "endpoint": "",
        "model": model or model_for(provider),
        "auth_type": auth_type,
        "capabilities": ["text"],
        "metadata": metadata,
        "secrets": secrets,
    }


def bind_model_account(account: dict, *, bridge=None) -> dict:
    """Bind ``account`` to the ``model`` role — what the host does when it
    resolves a runner's roles, for a turn driven at the ENGINE level
    (``eng.run_agent``), which reads the credential back through
    ``gen.account``. Defaults to the bridge the test installed as the native
    singleton (``native_bridge`` / ``docker_env_bridge``)."""
    from collimate import native

    target = bridge if bridge is not None else native.current_bridge()
    assert target is not None, "bind_model_account needs an installed host-free run"
    target._rec_accounts["model"] = account
    return account


# ── plan logins: a lease from a document on this machine ─────────────
#
# A plan (subscription) leg reads its login the way a run does: the harness
# backend asks ``login_store.login_path(harness)`` for the document in the
# selected account's LEASE, which the host writes (``gen.materialize_account``)
# and collects (``gen.release_account``). The recording bridge has no host
# half, so :func:`plan_login_lease` plays it: it copies the login document a
# ``COLLIMATE_LIVE_LOGIN_<HARNESS>`` variable names (a PATH — the document
# itself never passes through the environment, a log or an assertion) into a
# lease under the test's tmp dir, laid out as ``PLAN_LOGINS[harness].lease``.
#
# At the end the host commits a refresh the run made back to the account, and
# so does the fixture, to the source file: every plan harness but antigravity
# refreshes in place in the lease, and ChatGPT, Copilot and xAI refresh tokens
# are single-use, so a discarded lease would leave the source holding a
# revoked one. The write-back happens only when the source is still what was
# leased (its own CLI did not refresh it meanwhile). antigravity's Google
# token does not rotate: the run refreshes a per-run copy of the server's
# acp_token.json, which the fixture throws away, and its source is never
# written.

_PLAN_LOGIN_COPY_ONLY = frozenset({"antigravity"})


def plan_login_env(harness: str) -> str:
    """The variable naming ``harness``'s login document
    (``COLLIMATE_LIVE_LOGIN_ANTIGRAVITY`` for antigravity)."""
    return "COLLIMATE_LIVE_LOGIN_" + harness.upper().replace("-", "_")


def plan_login_steps(harness: str, plan: str = "") -> str:
    """How to make the document :func:`plan_login_env` names: a sign-in with
    the harness's own CLI, outside any run. (Not the Key manager: it only
    takes a paste of such a document, and writes none on this machine.)"""
    var = plan_login_env(harness)
    if harness == "antigravity":
        # The chat server's own token document, minted by signing in once
        # through the official antigravity-acp server (Zed or JetBrains).
        return ("sign in once through the official antigravity-acp server in Zed "
                "or JetBrains (on macOS set AGY_ACP_FORCE_FILE_STORAGE=1 first, "
                "or the token goes to the keychain), then set "
                f"{var} to the file it writes, "
                "~/.gemini/antigravity-acp/acp_token.json")
    from collimate.account_taxonomy import PLAN_LOGINS

    login = PLAN_LOGINS.get(harness)
    if login is None:
        return f"sign in once with {harness}'s own CLI, then set {var} to its login document"
    where = f"~/{login.relative}"
    if login.env:
        where += (f" (or ${login.env}/{login.suffix})" if login.suffix
                  else f" (or the file ${login.env} names)")
    entry = login.store_keys.get(plan)
    holding = f", holding its {entry!r} entry" if entry else ""
    command = login.command.replace(", then ", "`, then `")   # "pi, then /login"
    return (f"sign in once with `{command}` on this machine, then set "
            f"{var} to the document it writes, {where}{holding}; a refresh the "
            "run makes is written back to it")


def plan_login_missing(harness: str, plan: str = "") -> str:
    """Why no login document can be leased for ``harness`` — the variable
    unset, or naming no file — with how to make one, or ``""`` when one can.
    Names the variable and the path, never anything read from the file."""
    var = plan_login_env(harness)
    source = (os.environ.get(var) or "").strip()
    if not source:
        why = f"{var} is not set (the path of the {harness} login document)"
    elif not os.path.isfile(source):
        why = f"{var} names no file ({source})"
    else:
        return ""
    return f"{why} — {plan_login_steps(harness, plan)}"


def _write_back_login(source: str, data: bytes) -> None:
    """Replace ``source`` (through a link, to the file it names) with
    ``data``, atomically, keeping its mode and leaving its folder as it is."""
    import tempfile

    path = os.path.realpath(source)
    mode = os.stat(path).st_mode & 0o777
    descriptor, temporary = tempfile.mkstemp(prefix=".collimate-live-",
                                             dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@pytest.fixture
def plan_login_lease(tmp_path, monkeypatch):
    """``lease(harness, require=True)``: stand in for the host's account lease.

    Monkeypatches ``gen.materialize_account`` to hand the login store a lease
    root under ``tmp_path`` and ``gen.release_account`` to a no-op (the pattern
    of ``tests/test_backend_antigravity.py``'s ``selected_account_runtime``),
    and copies ``$COLLIMATE_LIVE_LOGIN_<HARNESS>`` into it 0600 at
    ``<lease>/<harness>/<PLAN_LOGINS[harness].lease…>``. With no document it
    SKIPS naming what is missing, or, with ``require=False``, leases an empty
    root, so the harness's own readiness predicate answers "no login". Returns
    the leased document's path (``""`` when none was copied). The login
    store's cached lease is closed before and after, as a run's teardown
    closes it; a refresh the run left in the lease is then written back to
    the source (see the section comment), and the lease removed."""
    from pathlib import Path

    import gen
    from _agent_shared import login_store
    from collimate.account_taxonomy import PLAN_LOGINS

    root = tmp_path / "lease"
    leased: list = []      # (harness, source, lease path, the bytes leased)

    def lease(harness: str, *, require: bool = True) -> str:
        missing = plan_login_missing(harness)
        if missing and require:
            pytest.skip(missing)
        login_store.close_account_credentials()
        login_store.ensure_store_dir(str(root))
        target = ""
        if not missing:
            target = str(root.joinpath(harness, *PLAN_LOGINS[harness].lease))
            source = os.environ[plan_login_env(harness)].strip()
            data = Path(source).read_bytes()
            login_store.write_private_atomic(target, data)
            os.chmod(target, 0o600)
            leased.append((harness, source, target, data))
        monkeypatch.setattr(gen, "materialize_account",
                            lambda role: {"path": str(root), "lease_id": "live-lease"},
                            raising=False)
        monkeypatch.setattr(gen, "release_account",
                            lambda role, lease_id: {"committed": True}, raising=False)
        return target

    try:
        yield lease
    finally:
        try:
            login_store.close_account_credentials()
            for harness, source, target, data in leased:
                if harness in _PLAN_LOGIN_COPY_ONLY:
                    continue
                # Read as the host collects a lease: the container can swap
                # the file for a link, which must not be followed.
                now = login_store._read_copy_nofollow(str(root), target)
                if not now or now == data:
                    continue
                try:
                    unchanged = Path(source).read_bytes() == data
                except OSError:
                    unchanged = False
                if unchanged:
                    _write_back_login(source, now)
                else:
                    print(f"[plan login] {plan_login_env(harness)}'s file changed "
                          "during the run; the run's refresh was not written back")
        finally:
            # pytest keeps recent tmp dirs: the lease's copy must not outlive
            # the test, as the host removes a lease on its last release.
            shutil.rmtree(root, ignore_errors=True)


def agent_config(surface: str = "annotation", **overrides) -> dict:
    """A cheap ``agent-config.set_yaml`` values map for the Agent generator on
    ``surface``.

    Provider, model and harness are NOT here — they are account facts, read off
    the bound ``model`` account (:func:`model_account`). The defaults are chosen
    for short turns with file tools disabled: a one-shot annotation run (the
    only surface with a session) that answers its prompt in its annotation
    context, an interactive conversation for chat. The internet grant selects
    web-tool availability; provider sampling and output defaults apply.
    Anything can be overridden per test."""
    cfg = {"surface": surface}
    if surface == "annotation":
        cfg["session"] = "oneshot"
    cfg["enable_file_tools"] = False
    cfg.update(overrides)
    return cfg


# Internet is full; Docker is none and commands deny (see
# run_agent_generator). The pydantic-ai engine runs commands
# only in its sandbox, which needs docker: full, and claude-code's chat runs
# them on this machine.
LIVE_GRANT = {
    "docker": "none", "internet": "full", "commands": "deny",
}


def _accepted_handler_records(root=None):
    """Read the built-in handlers only when their exact derived bytes are
    accepted."""
    from collimate.handlers import derived_hash, validate_handler
    roots = [Path(root)] if root is not None else [Path(_WORKSPACE) / "builtins/definitions"]
    records = []
    for definition in sorted(path for folder in roots for path in folder.glob("*/*.handler")):
        manifest = validate_handler(yaml.safe_load(definition.read_bytes()), allow_reserved=True)
        derived = definition.parent / "derived"
        files = {path.relative_to(derived).as_posix(): path.read_bytes()
                 for path in derived.rglob("*") if path.is_file()}
        if not files or manifest.get("derived") != derived_hash(files):
            raise ValueError(f"handler {manifest['id']} has stale accepted derived bytes")
        names = {path.split("/")[1] for path in files
                 if path.startswith("skills/") and len(path.split("/")) == 3
                 and path.endswith("/SKILL.md")}
        skills = {manifest["id"] + "--" + path[len("skills/"):]: body.decode("utf-8")
                  for path, body in files.items() if path.startswith("skills/")
                  and len(path.split("/")) >= 3 and path.split("/")[1] in names}
        records.append((manifest, files["verify.ts"], skills))
    if not records:
        raise ValueError("no accepted shipped handler definitions")
    return records


@functools.lru_cache(maxsize=32)
def _compiled_accepted_verifier(source: bytes, prelude: bytes) -> str:
    from collimate._host.renderer_bundle import compile_verifier
    return compile_verifier(source, prelude=prelude, deadline=time.monotonic() + 10).code


_VERIFIER_WORKER = """import json, sys, time
from collimate._host.verification import run_verifier
request = json.load(sys.stdin)
print(json.dumps(run_verifier(request['code'], request['input'], deadline=time.monotonic()+request['timeout'])))
"""


def _watched_verdict(code: str, input: dict, *, timeout=5.0) -> dict:
    """Run the actual compiled verifier in a process with an external watchdog."""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env["PYTHONPATH"] = str(Path(_REPO) / "src")
    process = subprocess.Popen([sys.executable, "-c", _VERIFIER_WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=os.name == "posix")
    try:
        output, error = process.communicate(json.dumps({"code": code, "input": input,
                                                      "timeout": timeout}), timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        return {"valid": False, "error": "accepted verifier deadline exceeded"}
    if process.returncode:
        raise RuntimeError(f"accepted verifier worker failed: {error[-1000:]}")
    verdict = json.loads(output)
    if not isinstance(verdict, dict) or not isinstance(verdict.get("valid"), bool):
        raise RuntimeError("accepted verifier worker returned no Verdict")
    return verdict


def _seed_accepted_handlers(bridge) -> None:
    """Seed real accepted metadata/skills and validators; publication stays recorded."""
    from collimate.handlers import handler_for
    records = _accepted_handler_records()
    bridge.stub_handlers([manifest for manifest, _, _ in records],
                         skills={manifest["id"]: skills for manifest, _, skills in records})
    prelude = (Path(_REPO) / "shared/verify/prelude.js").read_bytes()
    codes = {manifest["id"]: _compiled_accepted_verifier(source, prelude)
             for manifest, source, _ in records}
    original_validate = bridge.validate
    def validate(path, verifier=None):
        if verifier is not None:
            return original_validate(path, verifier=verifier)
        handler = handler_for(bridge.handlers(), path)
        if handler is None:
            return original_validate(path)
        package = {}
        paths = bridge.fs_list()
        for name in paths:
            body = bridge.fs_read_bytes(name)
            try:
                package[name] = body.decode("utf-8", "strict")
            except UnicodeDecodeError:
                continue
        text = bridge.fs_read_bytes(path).decode("utf-8", "strict")
        return _watched_verdict(codes[handler["id"]],
            {"path": path, "text": text, "package": package, "cell_paths": paths})
    bridge.validate = validate


def run_agent_generator(*, account: dict, config: dict, timeout: float = 300.0, **kwargs):
    """Run the real Agent generator host-free with ``config`` (its
    ``surface`` included) as its ``agent-config.set_yaml`` values and
    ``account`` bound to the ``model`` role (:func:`model_account`) — the
    binding the run cannot start without.

    The timeout default is 300s rather than the harness's 60s: a live turn on a
    slow provider — or one that has to reason before it speaks — routinely
    outlasts a minute, and a harness timeout would report as a generator
    failure with no hint that the model was simply still typing.

    The grant defaults to internet: full, docker: none and commands: deny
    (:data:`LIVE_GRANT`). The harness's unrestricted ``{}`` would arm the
    in-process engine's run_python / run_bash sandbox against the
    RecordingBridge's synthetic env plane, whose every exec "succeeds" with
    empty output — the model then acts on fake results — and would give
    claude-code's chat an unattended shell on this machine. A test that wants
    commands passes a real env plane and its own grant."""
    kwargs.setdefault("permissions", LIVE_GRANT)
    kwargs.setdefault("label", LIVE_LABEL)
    kwargs.setdefault("ui_files", _agent_ui_files())
    import collimate.testing as ct
    original = ct.make_test_bridge
    def factory(workspace, **options):
        bridge = original(workspace, **options)
        _seed_accepted_handlers(bridge)
        return bridge
    ct.make_test_bridge = factory
    try:
        return run_generator(
            AGENT_ENTRYPOINT,
            config={"agent-config.set_yaml": config},
            accounts={"model": account},
            timeout=timeout,
            **kwargs,
        )
    finally:
        ct.make_test_bridge = original



def _install_turn_gate(bridge, scripted: list) -> None:
    """Replace the bridge's scripted-message queue with a TURN-GATED one.

    The stock ``chat_poll`` pops one command per poll and signals stop the
    moment the queue drains. Against a synthetic ``on_turn`` that returns
    instantly — the shape ``tests/`` exercises — that is exactly right. Against
    a live model it is fatal twice over: ``serve_chat`` polls every 0.2s, so
    with a turn still in flight the *next* scripted message is treated as a
    same-thread REDIRECT (cancel the current turn, re-run with the new text),
    and the drain-time stop then cancels whatever is left after a 2s grace.
    A two-message script produced one redirect and one ``_(stopped by user)_``.

    So this gate releases message N+1 only once N assistant replies have been
    written to the transcript, and signals stop only once the last turn has
    landed. Nothing about ``serve_chat`` or the generator is bypassed — only
    the arrival TIMING a real user would supply is restored."""
    state = {"sent": 0}

    transcript = chat_transcript(bridge.label)

    def completed_turns() -> int:
        try:
            if not bridge.fs_exists(transcript):
                return 0
            raw = bridge.fs_read_any(transcript)
        except Exception:  # noqa: BLE001 — a half-written transcript just isn't ready
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return sum(1 for m in parse_transcript(raw) if m.role == "assistant")

    def chat_poll():
        done = completed_turns()
        if state["sent"] >= len(scripted):
            if done >= len(scripted):
                bridge._signal_stop()
            return None
        if done < state["sent"]:
            return None                      # the previous turn is still generating
        cmd = scripted[state["sent"]]
        state["sent"] += 1
        return dict(cmd) if isinstance(cmd, dict) else {
            "kind": "user_msg", "content": str(cmd),
        }

    bridge.chat_poll = chat_poll


def run_chat_generator(*, messages: list, account: dict, config: dict,
                       timeout: float = 600.0, **kwargs):
    """Run the Agent generator on the chat surface with ``messages``
    delivered one completed turn at a time (see :func:`_install_turn_gate`).

    The scripted queue is installed by intercepting the bridge factory —
    the same seam ``tests/conftest.py``'s ``bridge_hook`` uses — rather than by
    passing ``messages=`` through, because the gate has to replace the queue
    wholesale. Timeout is doubled over the oneshot default: this is N live
    turns back to back."""
    import collimate.testing as ct

    original = ct.make_test_bridge

    def factory(workspace, **kw):
        kw.pop("messages", None)
        bridge = original(workspace, **kw)
        _install_turn_gate(bridge, list(messages))
        return bridge

    ct.make_test_bridge = factory
    try:
        return run_agent_generator(account=account, config={**config, "surface": "chat"},
                                   timeout=timeout, **kwargs)
    finally:
        ct.make_test_bridge = original


def assert_ran(res, provider: str = "") -> None:
    """Fail loudly with the run's own error + traceback. A live test that only
    asserted on the reply text would report a dead key as 'LIVE-OK not found'."""
    label = f"{provider}: " if provider else ""
    assert res.ok, f"{label}run failed: {res.error}\n{res.traceback or ''}"


def assistant_replies(res) -> list:
    """Assistant turns from a chat run's open-format transcript.

    Read through the canonical parser rather than by indexing the JSON: the
    chat records thinking / attachments / tool rows alongside the text, and a
    positional read would silently start asserting on the wrong field the first
    time that shape grows.

    Note what this does NOT check: a chat turn that fails is recorded as
    ``_(error: …)_`` rather than raised, so ``res.ok`` stays True and the
    caller must inspect the text. Each chat test does.

    Where the transcript lives depends on how the run ended. While the
    session is open it is a cell file; at run end the §9.1.8 rule
    (``session/run_end.py``) logs it to the run log under the SAME relative name
    and clears it from the cell — so a run that ran to completion, which is
    every run this harness makes, hands it back in ``log_files``. The cell
    copy is still checked first for a run that was cut short."""
    raw = (res.files.get(chat_transcript())
           or getattr(res, "log_files", {}).get(chat_transcript()))
    assert raw, (
        f"no transcript written; files: {sorted(res.files)}, "
        f"log files: {sorted(getattr(res, 'log_files', {}))}"
    )
    return [m.content for m in parse_transcript(raw) if m.role == "assistant"]


# ══════════════════════════════════════════════════════════════════════
# In-code images (no binary fixtures)
# ══════════════════════════════════════════════════════════════════════

RED = (0xE0, 0x10, 0x10)
BLUE = (0x10, 0x10, 0xE0)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) + tag + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png_bytes(rgb: tuple, size: int = 64) -> bytes:
    """A solid ``rgb`` square as a truecolour PNG. Hand-rolled because the SDK
    has no imaging dependency and this suite ships no binary fixtures."""
    w = h = int(size)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8-bit, colour type 2 (RGB)
    raw = (b"\x00" + bytes(rgb) * w) * h                  # filter byte 0 per scanline
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _gif_bitstream(codes, width: int) -> bytes:
    """Pack ``codes`` LSB-first at a fixed ``width`` (GIF bit order)."""
    out = bytearray()
    acc = n = 0
    for code in codes:
        acc |= code << n
        n += width
        while n >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            n -= 8
    if n:
        out.append(acc & 0xFF)
    return bytes(out)


def gif_bytes(rgb: tuple, size: int = 64) -> bytes:
    """A solid ``rgb`` square as a GIF87a — the format-variance twin of
    :func:`png_bytes`.

    The LZW payload deliberately emits a Clear code before every literal. That
    keeps the code table permanently empty, so the code width never grows past
    the initial ``min_code_size + 1`` and the notoriously off-by-one
    width-bump rule never has to be reimplemented (and never has to agree with
    a particular decoder's interpretation of it). It costs ~2 bits per pixel on
    a 64x64 square — irrelevant here, and worth it for a payload whose
    correctness is obvious by inspection."""
    w = h = int(size)
    # Logical screen descriptor: 0x80 = global colour table present, size field
    # 0 → 2^(0+1) = 2 entries.
    header = b"GIF87a" + struct.pack("<HH", w, h) + bytes([0x80, 0, 0])
    gct = bytes(rgb) + b"\x00\x00\x00"                    # our colour, then black
    descriptor = b"\x2c" + struct.pack("<HHHH", 0, 0, w, h) + b"\x00"

    min_code_size = 2                                     # GIF's floor, even for 2 colours
    clear, eoi, code_width = 1 << min_code_size, (1 << min_code_size) + 1, min_code_size + 1
    codes: list = []
    for _ in range(w * h):
        codes += [clear, 0]                               # colour index 0 = rgb
    codes.append(eoi)
    payload = _gif_bitstream(codes, code_width)

    blocks = bytearray([min_code_size])
    for i in range(0, len(payload), 255):
        chunk = payload[i:i + 255]
        blocks.append(len(chunk))
        blocks += chunk
    blocks.append(0)                                      # block terminator

    return header + gct + descriptor + bytes(blocks) + b"\x3b"


def attachment(data: bytes, media_type: str, name: str) -> dict:
    """The engine's caller-attachment shape."""
    return {"data": data, "media_type": media_type, "name": name}


# ══════════════════════════════════════════════════════════════════════
# Real Docker on the env plane
# ══════════════════════════════════════════════════════════════════════


def ensure_image(client, spec: dict) -> None:
    """Build ``spec['image']`` (and its base) when this host doesn't have the
    content-addressed tag yet — the same build-if-missing rule the app's host
    applies, which is the whole point of the tag being a content hash.

    First run is EXPENSIVE: the shared ``collimate-linux-small`` base is a
    Debian image with python + node, and each harness layers its CLI on top.
    Minutes, and network."""
    import docker

    for tag, ctx, args in (
        (spec.get("base_image"), spec.get("base_context"), None),
        (spec.get("image"), spec.get("build_context"), spec.get("build_args")),
    ):
        if not tag or not ctx:
            continue
        try:
            client.images.get(tag)
            continue
        except docker.errors.ImageNotFound:
            pass
        client.images.build(
            path=ctx, tag=tag, rm=True,
            buildargs={k: str(v) for k, v in (args or {}).items()},
        )


def docker_client():
    """A docker client with a build-sized timeout — the default 60s read
    timeout expires part-way through a base image build and surfaces as a
    confusing transport error rather than "still building"."""
    import docker

    return docker.from_env(timeout=1800)


# ── live containers: labelled with their session, reaped when it is gone ──
#
# Every container the live suite places carries LIVE_CONTAINER_LABEL with this
# pytest process's pid. The fixture that placed it removes it on teardown; a
# session killed mid-run (a process exit, a Ctrl-C that outran the fixture)
# never reaches that teardown, and ``sleep infinity`` keeps the container up
# forever. So the next live session reaps what a dead session left, and the
# end of every session sweeps whatever escaped its own fixtures.

LIVE_CONTAINER_LABEL = "collimate.live.session"


def live_container_labels() -> dict:
    """The labels every container this session places must carry."""
    return {LIVE_CONTAINER_LABEL: str(os.getpid())}


def _session_alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def reap_live_containers(client, *, of_session: "str | None" = None) -> list:
    """Remove the live suite's containers that belong to ``of_session`` (a
    pid), or, with none named, to any session that is no longer running.
    Returns the names removed."""
    removed = []
    for container in client.containers.list(all=True, filters={"label": LIVE_CONTAINER_LABEL}):
        pid = (container.labels or {}).get(LIVE_CONTAINER_LABEL, "")
        stale = pid == of_session if of_session is not None else not _session_alive(pid)
        if stale:
            container.remove(force=True)
            removed.append(container.name)
    return removed


def _reap(session, **which) -> None:
    if not docker_available():
        return
    try:
        removed = reap_live_containers(docker_client(), **which)
    except Exception as exc:  # noqa: BLE001 — a backstop never fails the session
        session.config.issue_config_time_warning(
            pytest.PytestWarning(f"live container reaper: {exc}"), stacklevel=2)
        return
    if removed:
        print(f"live container reaper removed {len(removed)}: {', '.join(removed)}")


def pytest_sessionstart(session) -> None:
    _reap(session)


def pytest_sessionfinish(session) -> None:
    _reap(session, of_session=str(os.getpid()))


@pytest.fixture
def docker_env_bridge(native_bridge, one_loop_per_test, monkeypatch):
    """``native_bridge`` with its env plane wired to a real docker daemon.

    ``DockerEnvironment`` asks the bridge to *place* a container and returns a
    handle the harnesses then drive — including ``env.container``, which
    reattaches by container id through docker-py. So the stub has to hand back
    a REAL id: the ACP transport + the terminal surfaces call
    ``container.exec_run`` directly and would otherwise explode (or, worse,
    the recording bridge's default
    ``rc=0, output=""`` would make an empty reply look like a passing turn).

    This fixture closes only its own real containers through ``env_close``.
    Remaining owners are removed at fixture teardown; each carries
    :func:`live_container_labels`, so a session killed before that teardown
    leaves nothing the next session's reaper does not remove. ``DockerEnvironment.close``
    retains its existing best-effort exception handling; callback failure does
    not change that outer contract, and leaves the owner for the final sweep."""
    import tempfile
    from pathlib import PurePosixPath

    client = docker_client()
    started: dict = {}
    readonly_homes: list = []

    def place(spec: dict) -> dict:
        volumes = {
            spec["workspace"]: {
                "bind": spec.get("mount") or "/workspace",
                "mode": spec.get("mount_mode") or "rw",
            },
        }
        volumes.update(spec.get("volumes") or {})
        readonly = spec.get("readonly_files", {})
        if not isinstance(readonly, dict):
            raise RuntimeError("env_place: readonly_files must map container paths to text")
        # Docker treats repeated leading slashes as its ordinary root for
        # occupied mounts too; compare both sides under that same root.
        occupied = [PurePosixPath("/" + binding["bind"].lstrip("/"))
                    for binding in volumes.values()]
        for target, content in readonly.items():
            if (not isinstance(target, str) or not target.startswith("/")
                    or target.startswith("//")
                    or any(char in target for char in ("\0", ":", "\\"))
                    or ".." in target.split("/") or not isinstance(content, str)):
                raise RuntimeError("env_place: readonly_files requires absolute container paths and text content")
            dest = PurePosixPath(target)
            if dest == PurePosixPath("/") or any(
                    dest.is_relative_to(other) or other.is_relative_to(dest) for other in occupied):
                raise RuntimeError("env_place: readonly_files must not overlap another mount")
            occupied.append(dest)
        ensure_image(client, spec)
        readonly_home = None
        try:
            if readonly:
                readonly_home = tempfile.TemporaryDirectory(prefix="collimate-live-env-files-")
                for index, (target, content) in enumerate(readonly.items()):
                    source = Path(readonly_home.name) / str(index)
                    source.write_text(content, encoding="utf-8")
                    source.chmod(0o444)
                    volumes[str(source)] = {"bind": target, "mode": "ro"}
            container = client.containers.run(
                spec["image"],
                command=spec.get("command"),
                detach=True,
                volumes=volumes,
                environment=spec.get("environment") or {},
                working_dir=spec.get("working_dir") or None,
                extra_hosts=spec.get("extra_hosts") or None,
                ports=spec.get("ports") or None,
                name=f"{spec.get('name_prefix') or 'collimate-live'}-{uuid.uuid4().hex[:8]}",
                labels=live_container_labels(),
            )
        except BaseException:
            if readonly_home is not None:
                readonly_home.cleanup()
            raise
        started[container.id] = container
        if readonly_home is not None:
            readonly_homes.append(readonly_home)
        return {"ok": True, "env_id": container.id, "container_id": container.id,
                "error": None}

    def do_exec(env_id, command, opts: dict) -> dict:
        container = client.containers.get(env_id)
        res = container.exec_run(
            command,
            workdir=opts.get("cwd") or None,
            environment=opts.get("env") or None,
            user=opts.get("user") or "",
            detach=bool(opts.get("detach")),
        )
        if opts.get("detach"):
            return {"ok": True, "rc": 0, "output": "", "error": None}
        return {
            "ok": True,
            "rc": int(res.exit_code or 0),
            "output": (res.output or b"").decode("utf-8", "replace"),
            "error": None,
        }

    recorded_close = native_bridge.env_close

    def close_owned(env_id):
        container = started.get(env_id)
        if container is not None:
            container.remove(force=True)
            # Failed removal remains owned by the fixture for teardown retry.
            started.pop(env_id)
        return recorded_close(env_id)

    monkeypatch.setattr(native_bridge, 'env_close', close_owned)
    native_bridge.stub_env_place(place)
    native_bridge.stub_env_exec(do_exec)
    try:
        yield native_bridge
    finally:
        # The harnesses cache one container per backend for the whole "run";
        # drop the cache first so a later test in the same session provisions
        # fresh instead of reusing an id we are about to delete.
        from _agent_shared.acp_run import close_acp_sessions
        from _agent_shared.cli_container import close_all

        try:
            # This fixture owns the containers, so close their ACP sessions on
            # the still-running test loop before its synchronous cache teardown.
            one_loop_per_test.run(close_acp_sessions())
        finally:
            try:
                close_all()
            finally:
                for container in started.values():
                    try:
                        container.remove(force=True)
                    except Exception:  # noqa: BLE001 — teardown is best-effort
                        pass
                for readonly_home in readonly_homes:
                    readonly_home.cleanup()
