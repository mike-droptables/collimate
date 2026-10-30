# SPDX-License-Identifier: MIT
"""Selected accounts, temporary CLI leases, and credential-free session caches."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import stat

import pytest

import gen
from collimate.account_taxonomy import PLAN_LOGINS, PLAN_PROVIDERS
from _agent_shared import harness_homes
from _agent_shared.harnesses.claude_code import backend as _claude_backend  # noqa: E402
from _agent_shared import login_store as ls
from _agent_shared import acp_client as ac
from tests._agent_run import in_cell  # noqa: E402


@pytest.fixture
def account_store(request, tmp_path, monkeypatch):
    ls.close_account_credentials()
    harness_homes.close_harness_homes()
    root = tmp_path / 'run-ws' / '.collrun' / 'accounts' / 'lease-one'
    root.mkdir(parents=True)
    snapshot = {'id': 'account-one', 'revision': 'one', 'credential_revision': 'one',
                'service': 'openai', 'model': 'chosen', 'auth_type': 'cli',
                'metadata': {'harness': 'codex', 'plan': 'chatgpt'},
                'secrets': {'credential_json': '{"tokens":{"refresh_token":"private"}}'}}
    events = []
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    in_cell(request, monkeypatch, tmp_path / "run-ws", "cell-one")
    # Import tests deliberately reload gen; lazy runtime imports and modules
    # which retain their original reference must receive the same fake host.
    for runtime_gen in {gen, ac.gen, importlib.import_module("gen")}:
        monkeypatch.setattr(runtime_gen, 'account', lambda role: copy.deepcopy(snapshot))
        monkeypatch.setattr(runtime_gen, 'materialize_account', lambda role: events.append(('acquire', role)) or {'path': str(root), 'lease_id': 'lease-one'})
        monkeypatch.setattr(runtime_gen, 'release_account', lambda role, lease: events.append(('release', role, lease)) or {'committed': True, 'revision': 'two'})
    yield snapshot, root, events
    ls.close_account_credentials()
    harness_homes.close_harness_homes()


def test_only_host_lease_is_used_and_account_edits_do_not_rebind(account_store):
    snapshot, root, events = account_store
    first = ls.store_path('codex', 'auth.json')
    snapshot['metadata']['harness'] = 'pi'
    snapshot['secrets']['credential_json'] = 'replacement'
    assert ls.store_path('codex', 'auth.json') == first == str(root / 'codex/auth.json')
    assert events == [('acquire', 'model')]
    with pytest.raises(ValueError, match='different CLI'):
        ls.store_path('pi', 'auth.json')
    ls.close_account_credentials()
    ls.close_account_credentials()
    assert events == [('acquire', 'model'), ('release', 'model', 'lease-one')]


def test_failed_flush_still_releases_the_lease(account_store, monkeypatch):
    _, _, events = account_store
    ls.store_root()

    def fail():
        raise OSError("write-back failed")

    monkeypatch.setattr(ls, 'flush_account_credentials', fail)
    with pytest.raises(OSError, match='write-back failed'):
        ls.close_account_credentials()
    assert events == [('acquire', 'model'), ('release', 'model', 'lease-one')]
    assert ls._LEASE_ID is None


def test_cannot_materialize_another_harness_or_traverse(account_store):
    with pytest.raises(ValueError):
        ls.store_path('codex', '..', 'other')
    with pytest.raises(ValueError):
        ls.store_path('pi', 'auth.json')


@pytest.mark.parametrize('harness', sorted(PLAN_LOGINS))
def test_the_store_hands_out_only_the_leased_login_or_a_folder_on_its_path(account_store, harness):
    # The taxonomy's lease row is the one layout: login_path is that document,
    # a leading folder of it binds, and any other spelling is refused.
    snapshot, root, _ = account_store
    snapshot['metadata']['harness'] = harness
    lease = PLAN_LOGINS[harness].lease
    assert ls.login_path(harness) == str(root.joinpath(harness, *lease))
    assert ls.store_path(harness, *lease[:-1]) == str(root.joinpath(harness, *lease[:-1]))
    for rel in [('config.json',), (*lease[:-1], 'other.json'), (*lease, 'below')]:
        with pytest.raises(ValueError, match='Unsupported'):
            ls.store_path(harness, *rel)


def test_temporary_writer_is_private_even_with_permissive_umask(tmp_path):
    target = tmp_path / 'private' / 'auth.json'
    previous = os.umask(0)
    try:
        ls.write_private_atomic(str(target), b'{"token":"private"}')
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


_LEASE_READERS = [
    ('codex', 'codex_backend', {'tokens': {'refresh_token': 'r'}}, ()),
    ('opencode', 'opencode_backend', {'openai': {'type': 'oauth', 'refresh': 'r'}}, ('chatgpt',)),
    ('pi', 'pi_backend', {'openai-codex': {'type': 'oauth', 'refresh': 'r'}}, ('chatgpt',)),
    ('cline', 'cline_backend', {'providers': {'openai-codex': {'settings': {'auth': {'accessToken': 'a'}}}}}, ()),
    ('antigravity', 'antigravity_backend', {'type': 'authorized_user', 'client_id': 'i', 'client_secret': 's', 'refresh_token': 'r', 'token_uri': 'u'}, ()),
    ('copilot', 'copilot_backend', 'github_pat_11SYNTHETIC', ()),
]


def test_every_plan_login_has_a_selected_lease_reader():
    assert {row[0] for row in _LEASE_READERS} == set(PLAN_LOGINS)


@pytest.mark.parametrize('harness,module,document,args', _LEASE_READERS)
def test_selected_readiness_uses_selected_account_lease_only_F1403_F1404_P4F36(account_store, harness, module, document, args):
    # The document sits where the taxonomy's lease layout puts it, the same
    # path the host writes, so each backend reads the file the host leased.
    snapshot, root, events = account_store
    plan = {'antigravity': 'google', 'copilot': 'github_copilot'}.get(harness, 'chatgpt')
    snapshot['metadata'].update(harness=harness, plan=plan)
    snapshot['service'] = PLAN_PROVIDERS[plan]
    target = root.joinpath(harness, *PLAN_LOGINS[harness].lease)
    target.parent.mkdir(parents=True)
    target.write_text(document if isinstance(document, str) else json.dumps(document))
    assert ls.selected_login_ready(harness, plan) is True
    target.write_text('' if PLAN_LOGINS[harness].opaque else '{}')
    assert ls.selected_login_ready(harness, plan) is False
    assert events == [('acquire', 'model')]


# The server's own token document, as the google plan's lease holds it.
TOKEN_DOC = '''{"type":"authorized_user","client_id":"i","client_secret":"s","refresh_token":"r","token_uri":"u"}'''
REFRESHED_DOC = '''{"type":"authorized_user","client_id":"i","client_secret":"s","refresh_token":"r2","token_uri":"u"}'''


def test_google_copy_is_temporary_and_returned_before_release(account_store):
    from _agent_shared.harnesses.antigravity import backend as backend
    snapshot, root, events = account_store
    snapshot['metadata'].update(harness='antigravity', plan='google')
    snapshot['service'] = 'gemini'
    original = root / 'antigravity' / backend.ACP_TOKEN_FILE
    original.parent.mkdir()
    original.write_text(TOKEN_DOC)
    volumes = backend.antigravity_volumes('google')
    home = Path(harness_homes.harness_home('antigravity'))
    copied = home / backend.ACP_STATE_DIR / backend.ACP_TOKEN_FILE
    assert copied.read_text() == TOKEN_DOC
    assert str(home / backend.ACP_STATE_DIR) in volumes
    copied.write_text(REFRESHED_DOC)
    ls.close_account_credentials()
    assert original.read_text() == REFRESHED_DOC
    assert events[-1] == ('release', 'model', 'lease-one')


@pytest.mark.parametrize('returned', [
    'refreshed-token',                                        # not JSON
    '{"type":"authorized_user","client_id":"i"}',             # lacks the sign-in's keys
    '[]',                                                     # not a login object
    b'\xff\xfe not utf-8',
])
def test_a_returned_copy_that_is_not_the_plans_login_is_never_written_back(account_store, returned):
    """The harness container writes the copy and the host commits the lease
    to the account: the flush applies the paste's own validator, so only a
    copy that still reads as the selected plan's login goes back."""
    from _agent_shared.harnesses.antigravity import backend as backend
    snapshot, root, events = account_store
    snapshot['metadata'].update(harness='antigravity', plan='google')
    snapshot['service'] = 'gemini'
    original = root / 'antigravity' / backend.ACP_TOKEN_FILE
    original.parent.mkdir()
    original.write_text(TOKEN_DOC)
    backend.antigravity_volumes('google')
    home = Path(harness_homes.harness_home('antigravity'))
    copied = home / backend.ACP_STATE_DIR / backend.ACP_TOKEN_FILE
    copied.write_bytes(returned if isinstance(returned, bytes) else returned.encode())
    ls.close_account_credentials()
    assert original.read_text() == TOKEN_DOC
    assert events[-1] == ('release', 'model', 'lease-one')


@pytest.mark.parametrize('plant', ['file-link', 'dir-link', 'fifo'])
def test_google_copy_flush_never_follows_links_out_of_the_home(account_store, tmp_path, plant):
    # The vendor home is mounted read-write in the harness container, which
    # can swap the copy (or its directory) for a link to any host file. The
    # flush must not carry that host file into the lease the host commits.
    from _agent_shared.harnesses.antigravity import backend as backend
    snapshot, root, events = account_store
    snapshot['metadata'].update(harness='antigravity', plan='google')
    snapshot['service'] = 'gemini'
    original = root / 'antigravity' / backend.ACP_TOKEN_FILE
    original.parent.mkdir()
    original.write_text(TOKEN_DOC)
    backend.antigravity_volumes('google')
    home = Path(harness_homes.harness_home('antigravity'))
    copied = home / backend.ACP_STATE_DIR / backend.ACP_TOKEN_FILE
    host_secret = tmp_path / 'host-only' / backend.ACP_TOKEN_FILE
    host_secret.parent.mkdir()
    host_secret.write_text('HOST-ONLY-SECRET')
    if plant == 'file-link':
        copied.unlink()
        copied.symlink_to(host_secret)
    elif plant == 'dir-link':
        state = home / backend.ACP_STATE_DIR
        for child in state.iterdir():
            child.unlink()
        state.rmdir()
        state.symlink_to(host_secret.parent)
    else:
        copied.unlink()
        os.mkfifo(copied)
    ls.close_account_credentials()
    assert original.read_text() == TOKEN_DOC
    assert events[-1] == ('release', 'model', 'lease-one')


def test_the_copy_flush_reads_through_the_shared_nofollow_open__F1245(account_store, monkeypatch):
    # One audited implementation of the no-follow walk: the flush reads the
    # copy through collimate.fs.open_file, relative to the vendor home, and
    # keeps its size cap (a login document is a few KB).
    from _agent_shared.harnesses.antigravity import backend as backend
    snapshot, root, events = account_store
    snapshot['metadata'].update(harness='antigravity', plan='google')
    snapshot['service'] = 'gemini'
    original = root / 'antigravity' / backend.ACP_TOKEN_FILE
    original.parent.mkdir()
    original.write_text(TOKEN_DOC)
    backend.antigravity_volumes('google')
    home = Path(harness_homes.harness_home('antigravity'))
    copied = home / backend.ACP_STATE_DIR / backend.ACP_TOKEN_FILE
    copied.write_text('refreshed-token')
    calls = []
    real_open = ls.open_file

    def _spy(root_, path, *, write, append=False):
        calls.append((root_, path, write))
        return real_open(root_, path, write=write, append=append)

    monkeypatch.setattr(ls, 'open_file', _spy)
    assert ls._read_copy_nofollow(str(home), str(copied)) == b'refreshed-token'
    assert calls == [(str(home), os.path.relpath(copied, home), False)]
    copied.write_bytes(b'x' * ls._MAX_COPY_BYTES)
    assert ls._read_copy_nofollow(str(home), str(copied)) == b'x' * ls._MAX_COPY_BYTES
    copied.write_bytes(b'x' * (ls._MAX_COPY_BYTES + 1))
    assert ls._read_copy_nofollow(str(home), str(copied)) is None
    copied.write_text(REFRESHED_DOC)
    ls.close_account_credentials()
    assert original.read_text() == REFRESHED_DOC
    assert events[-1] == ('release', 'model', 'lease-one')


def test_a_copy_that_grows_past_the_cap_once_open_reads_as_none__F1245(tmp_path, monkeypatch):
    # The container still holds the copy read-write, so it can grow the file
    # after the flush opens it. The cap is judged on the bytes read: an
    # oversized copy is never cut down to the cap and committed as a
    # (corrupt) credential.
    home = tmp_path / 'home'
    home.mkdir()
    copied = home / 'token'
    copied.write_text('small')
    real_open = ls.open_file

    def _open_then_grow(root_, path, *, write, append=False):
        handle = real_open(root_, path, write=write, append=append)
        copied.write_bytes(b'x' * (ls._MAX_COPY_BYTES + 1))
        return handle

    monkeypatch.setattr(ls, 'open_file', _open_then_grow)
    assert ls._read_copy_nofollow(str(home), str(copied)) is None


def test_copy_registration_must_stay_inside_its_home(tmp_path):
    with pytest.raises(ValueError, match='inside its harness home'):
        ls.register_materialized_copy('antigravity', str(tmp_path / 'elsewhere' / 'token'),
                                      root=str(tmp_path / 'home'))


def test_harness_home_persists_session_history_but_never_credentials(account_store, tmp_path):
    home = Path(harness_homes.harness_home('codex'))
    (home / 'sessions').mkdir()
    (home / 'sessions' / 'turn.jsonl').write_text('conversation')
    (home / 'auth.json').write_text('private-token')
    (home / 'config.toml').write_text('api_key = "private-token"')
    harness_homes.close_harness_homes()
    assert not home.exists()
    stored = tmp_path / 'cache/collimate/agent-state/cell-one/codex'
    assert (stored / 'sessions/turn.jsonl').read_text() == 'conversation'
    assert not (stored / 'auth.json').exists()
    assert not (stored / 'config.toml').exists()
    resumed = Path(harness_homes.harness_home('codex'))
    assert (resumed / 'sessions/turn.jsonl').read_text() == 'conversation'
    assert not (resumed / 'auth.json').exists()


def test_session_cache_tree_is_owner_only(account_store, tmp_path):
    # Session files keep their vendor modes (often 0644); the tree above
    # them must keep other local users out, including a pre-existing 0775
    # cache root.
    root = tmp_path / 'cache/collimate'
    root.mkdir(parents=True)
    root.chmod(0o775)
    home = Path(harness_homes.harness_home('codex'))
    (home / 'history.jsonl').write_text('conversation')
    (home / 'history.jsonl').chmod(0o644)
    harness_homes.close_harness_homes()
    stored = root / 'agent-state/cell-one/codex'
    assert (stored / 'history.jsonl').read_text() == 'conversation'
    for level in (root, root / 'agent-state', root / 'agent-state/cell-one', stored):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700, level


def test_session_cache_never_touches_directories_above_its_root(account_store, tmp_path, monkeypatch):
    # The cache root can change between opening and closing a home (atexit
    # runs after the environment moved on); closing must still tighten only
    # the tree the home was opened under, never a parent of it.
    outer = tmp_path / 'cache'
    home = Path(harness_homes.harness_home('codex'))
    (home / 'history.jsonl').write_text('conversation')
    outer.mkdir(exist_ok=True)
    outer.chmod(0o755)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'elsewhere'))
    harness_homes.close_harness_homes()
    assert (outer / 'collimate/agent-state/cell-one/codex/history.jsonl').read_text() == 'conversation'
    assert stat.S_IMODE(outer.stat().st_mode) == 0o755
    assert not (tmp_path / 'elsewhere').exists()


def test_private_cache_dir_outside_root_changes_only_that_dir(tmp_path):
    parent = tmp_path / 'parent'
    parent.mkdir()
    parent.chmod(0o755)
    harness_homes._private_cache_dir(str(tmp_path / 'root'), str(parent / 'state'))
    assert stat.S_IMODE((parent / 'state').stat().st_mode) == 0o700
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert not (tmp_path / 'root').exists()


def test_claude_subscription_uses_snapshot_and_never_prompts(account_store):
    snapshot, _, events = account_store
    snapshot['secrets'] = {'oauth_token': 'subscription-secret'}
    assert asyncio.run(_claude_backend.claude_subscription_token()) == 'subscription-secret'
    snapshot['secrets'] = {}
    with pytest.raises(RuntimeError, match='Key manager'):
        asyncio.run(_claude_backend.claude_subscription_token())
    assert not events


def test_login_requireds_point_to_manager_not_generator_terminal():
    hint = ls.login_required(plan='chatgpt', harness='codex')
    assert 'Key manager' in hint
    assert 'ChatGPT' in hint


def test_session_cache_never_follows_symlinks_to_credentials(account_store, tmp_path):
    home = Path(harness_homes.harness_home("codex"))
    sessions = home / "sessions"
    sessions.mkdir()
    (home / "auth.json").write_text("private-token")
    (sessions / "apparently-a-session.jsonl").symlink_to(home / "auth.json")
    harness_homes.close_harness_homes()
    cached = tmp_path / "cache/collimate/agent-state/cell-one/codex/sessions"
    assert not list(cached.iterdir())
