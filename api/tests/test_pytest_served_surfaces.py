"""D22 generic run surfaces across real host paths: a run's one inbox, its
claims, their write locks and their coordinates."""

import asyncio
import uuid

import pytest
import yaml

import test_pytest_run_folders as support
from test_pytest_run_folders import _mem
from test_pytest_run_folders import _emit

client, cell, live = support.client, support.cell, support.live
CHANNEL = support.CHANNEL


@pytest.fixture(autouse=True)
def clean_sessions():
    from sessions import chat as chat_sessions
    for sid in list(chat_sessions._sessions):
        chat_sessions.unregister_chat_session(sid)
    yield
    for sid in list(chat_sessions._sessions):
        chat_sessions.unregister_chat_session(sid)


def install_types():
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    from state import find_active_channel
    from persistence import publish_files
    manifest = {'id': 'surface-test', 'name': 'Surface', 'description': '',
                'guidance': '', 'examples': [], 'types': [{'ext': '.panel'}, {'ext': '.document'}]}
    channel=find_active_channel()
    definition=Cell(files=[VirtualFile(name='surface/main.handler',content=yaml.safe_dump(manifest))])
    add_capabilities_cell(channel,definition)
    publish_files(channel,str(definition.id),{},persist=False)


def _files(cell_id):
    from database import get_db_connection
    from persistence import snapshot_files
    from cas import read_blob
    with get_db_connection(CHANNEL) as conn:
        return {row['path']:read_blob(row['body_hash']).decode('utf-8',errors='replace') for row in snapshot_files(conn,cell_id)}


def register(run, path='status.panel', **options):
    """The SDK's surface_register frame, answered by the run's IPC op."""
    sent = []
    async def capture(body, _request):
        sent.append(body)
    run._send_ack = capture
    asyncio.run(run._op_surface_register({'path': path, 'label': 'Status', 'transport_only': False,
                                          'broadcast': False, **options}))
    return sent[-1]


def test_cell_surface_registration_and_lookup__D22(cell, live, client):
    install_types()
    run = live.start(cell)
    ack = register(run)
    assert ack['ok'], ack
    response = client.get(f'/api/channels/{CHANNEL}/cells/{cell}/surfaces/status.panel')
    assert response.status_code == 200
    assert response.json() == {'state': 'live', 'chat_id': ack['chat_id'], 'ws_path': f"/api/chats/{ack['chat_id']}",
                               'token': ack['token'], 'label': 'Status', 'run_id': run.run_id}


def test_registered_surface_lookup_uses_uuid_across_rename_and_same_label_channels(cell, client):
    from state import require_channel, state
    from models import Channel
    from served_surfaces import Surface, _store
    owner = require_channel(CHANNEL)
    surface = Surface(channel=CHANNEL, cell_id=cell, run_id='scope-fixture', path='scope.panel', label='Scope',
                      chat_id='scope-session', token='token', owner_label='scope')
    _store(surface)
    expected = surface.coordinates()
    other = Channel(name=owner.name)
    state.channels.append(other)
    path = f'/cells/{cell}/surfaces/scope.panel'
    assert client.get(f'/api/channels/{CHANNEL}{path}').json() == expected
    assert client.get(f'/api/channels/{other.id}{path}').status_code == 404
    for refused_label in ('starting', owner.name):
        assert client.get(f'/api/channels/{refused_label}{path}').status_code == 404
    renamed = client.put(f'/api/channels/{CHANNEL}/rename', params={'name': 'Renamed'})
    assert renamed.status_code == 200, renamed.text
    response = client.get(f'/api/channels/{CHANNEL}{path}')
    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert client.get(f'/api/channels/{other.id}{path}').status_code == 404
    assert client.get(f'/api/channels/{uuid.uuid4()}{path}').status_code == 404


def test_surface_claim_refuses_overlap_and_other_run_writes__D22(cell, live):
    install_types()
    first, second = live.start(cell), live.start(cell)
    assert register(first)['ok']
    assert register(second)['error'] == 'surface_taken'
    _, refused = _emit(second,artifacts=[_mem('status.panel', 'forged'), _mem('plain.txt', 'yes')])
    assert refused == [{'path': 'status.panel', 'reason': 'locked', 'owner': first.label}]
    assert _files(cell)['plain.txt'] == 'yes'


def test_a_run_registers_one_inbox(cell, live):
    """Every registration is the run's inbox: the same path again keeps it,
    another path is refused."""
    install_types()
    run = live.start(cell)
    assert register(run)['ok']
    assert register(run)['ok']
    assert register(run, 'other.panel')['error'] == 'inbox_exists'


def test_surface_metadata_release_and_independent_claims__D22(cell, live, client, monkeypatch):
    import events
    from sessions.chat import unregister_sessions_for_run
    install_types()
    run = live.start(cell)
    monkeypatch.setattr('routes.runs.queries.active_managers', live.runs)
    seen = []
    monkeypatch.setattr(events, 'broadcast', lambda channel, event: seen.append(event))
    ack = register(run)
    assert ack['ok']
    active = client.get(f'/api/channels/{CHANNEL}/runs/active').json()
    assert active[0]['surfaces'] == [{'path': 'status.panel', 'label': 'Status', 'state': 'live'}]
    unregister_sessions_for_run(run.run_id)
    assert client.get(f'/api/channels/{CHANNEL}/cells/{cell}/surfaces/status.panel').status_code == 404
    assert seen == [{'type': 'surface_changed', 'cell_id': cell, 'path': 'status.panel'}] * 2


def test_surface_claim_rechecked_after_verification__D22(cell, live, monkeypatch):
    import run_verification
    install_types()
    owner, writer = live.start(cell), live.start(cell)
    original = run_verification.RunVerifier.check
    calls = []
    def check(verifier, snap):
        result = original(verifier, snap)
        calls.append(True)
        if len(calls) == 1:
            assert register(owner)['ok']
        return result
    monkeypatch.setattr(run_verification.RunVerifier, 'check', check)
    _, refused = _emit(writer,artifacts=[_mem('status.panel', 'bad'), _mem('ok.txt', 'yes')])
    assert refused == [{'path': 'status.panel', 'reason': 'locked', 'owner': owner.label}]
    assert 'status.panel' not in _files(cell)
    assert _files(cell)['ok.txt'] == 'yes'


def test_two_concurrent_surface_registrants_have_one_owner__D22(cell, live):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    install_types()
    first, second = live.start(cell), live.start(cell)
    barrier = Barrier(2)
    def claim(run):
        barrier.wait(timeout=5)
        return register(run)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, [first, second]))
    assert sorted(value['ok'] for value in results) == [False, True]
    assert next(value for value in results if not value['ok'])['error'] == 'surface_taken'


@pytest.mark.parametrize('path', ['../escape.panel', '/absolute.panel', '.collrun/private.panel', 'a//b.panel'])
def test_surface_paths_never_escape_cell__D22(cell, live, path):
    run = live.start(cell)
    assert register(run, path)['ok'] is False


def test_surface_run_folder_rules_and_sealed_registration__D22(cell, live):
    install_types()
    run, other = live.start(cell), live.start(cell)
    assert register(run, f'{other.folder}/status.panel')['error'] == 'surface_taken'
    assert register(run, f'{run.folder}/status.panel')['error'] == 'ui_type'
    run.ui_files = ('.panel', '.document')
    assert register(run, f'{run.folder}/status.panel')['ok']
    run.sealed = True
    assert register(run, f'{run.folder}/status.panel')['error'] == 'run_ended'


def test_surface_action_resolves_registry__D22(cell, live, client, monkeypatch):
    install_types()
    run = live.start(cell)
    monkeypatch.setattr('routes.runs.control.active_managers', live.runs)
    assert register(run)['ok']
    response = client.post(f'/api/channels/{CHANNEL}/cells/{cell}/actions',
                           json={'source': 'status.panel', 'action_id': 'go'})
    assert response.status_code == 200, response.text
    assert run._pending_renderer_actions[-1]['action_id'] == 'go'


def test_surface_indexes_never_scan_unrelated_cells_runs_or_chats__D22(cell, live, monkeypatch):
    import served_surfaces as registry
    from dataclasses import replace
    from sessions.chat import unregister_chat_session
    install_types()
    run = live.start(cell)
    ack = register(run)
    target = registry.get(cell, 'status.panel')
    assert target is not None
    class NoGlobalScan(dict):
        def values(self):
            raise AssertionError('registry lookup inspected unrelated surfaces')
        def items(self):
            raise AssertionError('registry lookup inspected unrelated surfaces')
        def __iter__(self):
            raise AssertionError('registry lookup inspected unrelated surfaces')
    records = dict(registry._surfaces)
    records.update({(f'other-{i}', 'other.panel'): replace(target, cell_id=f'other-{i}', run_id=f'run-{i}')
                    for i in range(10000)})
    monkeypatch.setattr(registry, '_surfaces', NoGlobalScan(records))
    assert registry.for_run(run.run_id) == [target]
    assert registry.identity(cell) == ((target.path, run.run_id),)
    unregister_chat_session(ack['chat_id'])
    assert registry.for_run(run.run_id) == []
    assert registry.identity(cell) == ()
    assert registry.get(cell, 'status.panel') is None


def test_transport_only_inbox_needs_no_ui_grant_and_publishes_no_file__D22(cell, live, client, monkeypatch):
    import events
    from served_surfaces import for_run, get
    from sessions.chat import get_inbox
    run = live.start(cell)
    run.ui_files = ()
    monkeypatch.setattr('routes.runs.queries.active_managers', live.runs)
    seen = []
    monkeypatch.setattr(events, 'broadcast', lambda channel, event: seen.append(event))
    path = f'{run.folder}/hidden.chat'
    ack = register(run, path, transport_only=True)
    assert ack['ok'], ack
    inbox = get_inbox(run.run_id)
    assert inbox is not None and inbox.surface == path and inbox.answer_route == 'inbox'
    assert for_run(run.run_id) == [] and get(cell, path) is None
    assert client.get(f'/api/channels/{CHANNEL}/runs/active').json()[0]['surfaces'] == []
    assert client.get(f'/api/channels/{CHANNEL}/cells/{cell}/surfaces/{path}').status_code == 404
    assert path not in _files(cell)
    assert not [event for event in seen if event['type'] == 'surface_changed']
    _, refused = _emit(run,artifacts=[_mem(path, 'still requires a UI grant')])
    assert refused[0]['reason'] == 'ui_type'


@pytest.mark.parametrize('options', [
    {'path': 'cell.chat'}, {'transport_only': 'yes'}, {'broadcast': 'yes'},
    {'transport_only': False, 'broadcast': True},
])
def test_transport_only_inbox_cannot_grant_surface_capabilities__D22(cell, live, options):
    run = live.start(cell)
    run.ui_files = ('.chat',)
    fields = {'path': f'{run.folder}/hidden.chat', 'transport_only': True, **options}
    ack = register(run, **fields)
    assert ack['ok'] is False and ack['error'] == 'invalid_surface_declaration'


def test_a_broadcast_transport_only_inbox_takes_several_sockets(cell, live, client):
    """The broadcast inbox several status-pane drilldowns attach to for
    approval cards: two sockets at once, no 4409."""
    from sessions.chat import get_inbox
    run = live.start(cell)
    ack = register(run, f'{run.folder}/inbox.chat', transport_only=True, broadcast=True)
    assert ack['ok'], ack
    inbox = get_inbox(run.run_id)
    assert inbox is not None and inbox.broadcast
    url = f"{ack['ws_path']}?token={ack['token']}"
    with client.websocket_connect(url), client.websocket_connect(url):
        assert len(inbox.subscribers) == 2


def test_a_pointer_surface_is_never_broadcast(cell, live):
    from sessions.chat import get_inbox
    install_types()
    run = live.start(cell)
    assert register(run, broadcast=True)['error'] == 'invalid_surface_declaration'
    assert register(run)['ok']
    inbox = get_inbox(run.run_id)
    assert inbox is not None and not inbox.broadcast


def test_surface_transport_preserves_legal_path_whitespace__D22(cell, live, client):
    from sessions.chat import get_inbox
    install_types()
    run = live.start(cell)
    path = ' status.panel'
    ack = register(run, path)
    assert ack['ok'], ack
    inbox = get_inbox(run.run_id)
    assert inbox is not None and inbox.surface == path and inbox.chat_id == ack['chat_id']
    assert client.get(f'/api/channels/{CHANNEL}/cells/{cell}/surfaces/{path}').json()['chat_id'] == ack['chat_id']


def test_starting_claims_upgrade_on_their_runs_register_and_have_no_chat_index_GN03(cell, live, client, monkeypatch):
    """An accepted run's starting claims carry no session, so two runs'
    claims never collide in the chat index; get_surface answers starting
    coordinates; only the claiming run's register upgrades its claim (to
    live, with its session), another run's is refused; surface_changed
    fires on the claim, the upgrade and the release."""
    import events
    import served_surfaces as registry
    install_types()
    seen = []
    monkeypatch.setattr(events, 'broadcast', lambda channel, event: seen.append(event))
    first, second = live.start(cell), live.start(cell)
    registry.claim_starting(CHANNEL, cell, first.run_id, ['status.panel'], first.label, label='Runner')
    registry.claim_starting(CHANNEL, cell, second.run_id, [f'{second.folder}/x.panel'], second.label)
    assert '' not in registry._by_chat
    response = client.get(f'/api/channels/{CHANNEL}/cells/{cell}/surfaces/status.panel')
    assert response.status_code == 200
    assert response.json() == {'state': 'starting', 'run_id': first.run_id, 'label': 'Runner',
                               'chat_id': '', 'ws_path': '', 'token': ''}
    with pytest.raises(registry.SurfaceTaken):
        registry.claim_starting(CHANNEL, cell, second.run_id, ['status.panel'], second.label)
    assert register(second)['error'] == 'surface_taken'

    ack = register(first)
    assert ack['ok'] and ack['state'] == 'live' and ack['chat_id']
    live_surface = registry.get(cell, 'status.panel')
    assert live_surface is not None
    assert live_surface.state == 'live' and registry._by_chat[ack['chat_id']] is live_surface
    assert client.get(f'/api/channels/{CHANNEL}/cells/{cell}/surfaces/status.panel').json()['state'] == 'live'

    registry.release_run(second.run_id)
    assert registry.get(cell, f'{second.folder}/x.panel') is None
    assert registry.get(cell, 'status.panel') is live_surface
    changed = [e for e in seen if e['type'] == 'surface_changed']
    assert changed == [
        {'type': 'surface_changed', 'cell_id': cell, 'path': 'status.panel'},
        {'type': 'surface_changed', 'cell_id': cell, 'path': f'{second.folder}/x.panel'},
        {'type': 'surface_changed', 'cell_id': cell, 'path': 'status.panel'},
        {'type': 'surface_changed', 'cell_id': cell, 'path': f'{second.folder}/x.panel'},
    ]


def test_a_starting_claim_locks_its_cell_path_until_release_run_GN03(cell, live):
    """A starting claim on a cell-scoped path refuses another run's write
    there, as a live claim does; release_run frees it."""
    import served_surfaces as registry
    install_types()
    first, second = live.start(cell), live.start(cell)
    registry.claim_starting(CHANNEL, cell, first.run_id, ['status.document'], first.label)
    _, refused = _emit(second, artifacts=[_mem('status.document', 'forged')])
    assert refused == [{'path': 'status.document', 'reason': 'locked', 'owner': first.label}]
    registry.release_run(first.run_id)
    assert registry.get(cell, 'status.document') is None and registry.for_run(first.run_id) == []
    _, refused = _emit(second, artifacts=[_mem('status.document', 'second')])
    assert refused == []
    assert _files(cell)['status.document'] == 'second'
