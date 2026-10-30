"""Host environment placement stays inside the run's declared filesystem scope."""
import asyncio
import json
from pathlib import Path
import socket

import pytest

from runner_permissions import RunnerPermissions
from test_pytest_environments import _FakeClient, _Writer


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    import process_manager.environments as environments
    from process_manager.ipc import IPCMixin
    monkeypatch.chdir(tmp_path)

    class Run(IPCMixin):
        pass

    root = tmp_path / 'run'
    workspace = root / 'workspace'
    app_root = root / 'app'
    generator = app_root / 'generator'
    for path in (workspace, generator, app_root / '_shared', tmp_path / 'outside', root / 'workspace-sibling'):
        path.mkdir(parents=True)
    run = Run()
    run.run_id = 'run-boundary'
    run.workspace_dir = str(workspace)
    run.app_dir = str(generator)
    run.app_root_dir = str(app_root)
    run.env_passthrough_sockets = frozenset()
    run.permissions = RunnerPermissions.from_definition({'docker': 'full'})
    run.log_buffer = []
    run.bound_ports = set()
    run._ipc_writer = _Writer()
    client = _FakeClient(present=('present:latest',))
    calls = []
    def docker():
        calls.append('docker')
        return client
    monkeypatch.setattr(environments, '_relax_perms', lambda path: calls.append(('chmod', path)))
    run._environment_service = environments.EnvironmentService(run, client_factory=docker)
    return run, client, calls


def place(run, **fields):
    asyncio.run(run._handle_ipc(json.dumps({"req_id": "test-request", 'op': 'env_place', 'spec': {'image': 'present:latest', **fields}})))
    return run._ipc_writer.frames[-1]


def test_failed_account_collection_revokes_mount_authority__D05(boundary, tmp_path, monkeypatch):
    import accounts
    run, _client, calls = boundary
    lease = tmp_path / 'retained-lease'
    lease.mkdir()
    (lease / 'login.json').write_text('synthetic refresh')
    run._account_leases = {'lease': 'selected'}
    run.host_bindings = {'lease': {'path': str(lease), 'mode': 'rw'}}

    def fail_release(lease_id, owner):
        raise OSError('synthetic collection failure')

    monkeypatch.setattr(accounts, 'release_cli_account', fail_release)
    asyncio.run(run._op_account_release({'req_id': 'release', 'role': 'selected', 'lease_id': 'lease'}))
    assert run._ipc_writer.frames[-1]['ok'] is False
    assert 'synthetic collection failure' in run._ipc_writer.frames[-1]['error']
    assert run._account_leases == {} and run.host_bindings == {}
    assert (lease / 'login.json').read_text() == 'synthetic refresh'
    result = place(run, volumes={str(lease): {'bind': '/home/tool/login', 'mode': 'rw'}})
    assert result['ok'] is False
    assert calls == []


@pytest.mark.parametrize('field', ['workspace', 'build_context', 'base_context'])
@pytest.mark.parametrize('escape', ['outside', 'prefix_sibling', 'symlink'])
def test_placement_refuses_host_paths_before_docker_or_chmod__D05(boundary, tmp_path, field, escape):
    run, client, calls = boundary
    target = tmp_path / 'outside'
    if escape == 'prefix_sibling':
        target = Path(run.workspace_dir).with_name('workspace-sibling')
    elif escape == 'symlink':
        link = Path(run.workspace_dir) / 'escape'
        link.symlink_to(target, target_is_directory=True)
        target = link
    ack = place(run, mount='/work', **{field: str(target)})
    assert ack['ok'] is False
    assert field in ack['error']
    assert calls == []
    assert client.containers.ran == []


@pytest.mark.parametrize('kind', ['workspace_directory', 'declared_file', 'undeclared_socket'])
def test_placement_refuses_every_extra_volume_except_declared_sockets__D05(boundary, tmp_path, kind):
    run, client, calls = boundary
    opened = None
    if kind == 'workspace_directory':
        source = Path(run.workspace_dir)
    elif kind == 'declared_file':
        source = tmp_path / 'ordinary-file'
        source.write_text('not a socket')
        run.env_passthrough_sockets = frozenset({str(source)})
    else:
        source = tmp_path / 'undeclared.sock'
        opened = socket.socket(socket.AF_UNIX)
        opened.bind(source.name)
    try:
        ack = place(run, volumes={str(source): {'bind': '/extra', 'mode': 'rw'}})
        assert ack['ok'] is False
        assert 'socket' in ack['error']
        assert calls == []
        assert client.containers.ran == []
    finally:
        if opened:
            opened.close()


def test_placement_uses_workspace_and_entire_staged_app_tree__D05(boundary):
    run, client, calls = boundary
    ack = place(run, mount='/work', workspace=run.workspace_dir,
                build_context=run.app_dir, base_context=str(Path(run.app_root_dir) / '_shared'))
    assert ack['ok'] is True
    assert client.containers.ran[0].run_kwargs['volumes'] == {
        run.workspace_dir: {'bind': '/work', 'mode': 'rw'},
    }
    assert calls == ['docker', ('chmod', run.workspace_dir)]


def test_declared_real_socket_is_the_only_extra_volume__D05(boundary, tmp_path):
    run, client, _calls = boundary
    source = tmp_path / 'declared.sock'
    with socket.socket(socket.AF_UNIX) as opened:
        opened.bind(source.name)
        run.env_passthrough_sockets = frozenset({str(source)})
        ack = place(run, volumes={str(source): {'bind': '/ssh.sock', 'mode': 'rw'}})
    assert ack['ok'] is True
    assert client.containers.ran[0].run_kwargs['volumes'] == {
        str(source): {'bind': '/ssh.sock', 'mode': 'rw'},
    }


def test_manifest_socket_allowlist_uses_captured_host_env__D05(tmp_path, monkeypatch):
    from process_manager.manager import ProcessManager
    from test_pytest_subprocess_env_scrubbing import _mgr
    monkeypatch.chdir(tmp_path)

    source = tmp_path / 'declared.sock'
    ordinary = tmp_path / 'file'
    ordinary.write_text('not a socket')
    with socket.socket(socket.AF_UNIX) as opened:
        opened.bind(source.name)
        monkeypatch.setenv('SSH_AUTH_SOCK', str(source))
        monkeypatch.setenv('ORDINARY_FILE', str(ordinary))
        manager = _mgr(tmp_path)
        ProcessManager._build_subprocess_env(manager, {'env_passthrough': ['SSH_AUTH_SOCK', 'ORDINARY_FILE']})
        assert manager.env_passthrough_sockets == frozenset({str(source)})
        monkeypatch.setenv('SSH_AUTH_SOCK', str(tmp_path / 'changed.sock'))
        assert manager.env_passthrough_sockets == frozenset({str(source)})


@pytest.mark.parametrize('field,value', [
    ('mount', 'relative'), ('mount', '/work:rw'), ('mount', '/work/../host'),
    ('mount', '/work\0tail'), ('mount_mode', 'rw,shared'),
])
def test_container_mount_shape_is_refused_before_docker__D05(boundary, field, value):
    run, _client, calls = boundary
    ack = place(run, **{field: value})
    assert ack['ok'] is False
    assert field in ack['error']
    assert calls == []


def test_workspace_placement_keeps_private_run_files_owner_only__D05(tmp_path):
    from process_manager.environments import _relax_perms

    private = tmp_path / '.collrun' / 'accounts'
    private.mkdir(parents=True, mode=0o700)
    login = private / 'credential'
    login.write_text('selected-token')
    login.chmod(0o600)
    public = tmp_path / 'output.txt'
    public.write_text('output')
    public.chmod(0o600)
    _relax_perms(str(tmp_path))
    _relax_perms(str(private))
    assert private.stat().st_mode & 0o777 == 0o700
    assert login.stat().st_mode & 0o777 == 0o600
    assert public.stat().st_mode & 0o777 == 0o644


def test_selected_account_lease_binding_is_allowed_and_revocable__D05(boundary, tmp_path):
    run, client, _calls = boundary
    lease = tmp_path / 'shared-lease'
    child = lease / 'login'
    child.mkdir(parents=True)
    run.host_bindings = {'lease-one': {'path': str(lease), 'mode': 'rw'}}
    volume = {str(child): {'bind': '/home/login', 'mode': 'rw'}}
    assert place(run, volumes=volume)['ok'] is True
    assert client.containers.ran[-1].run_kwargs['volumes'] == volume
    run.host_bindings.pop('lease-one')
    assert place(run, volumes=volume)['ok'] is False


def test_account_binding_never_follows_a_symlink_outside_lease__D05(boundary, tmp_path):
    run, _client, calls = boundary
    lease = tmp_path / 'shared-lease'
    lease.mkdir()
    (lease / 'escape').symlink_to(tmp_path / 'outside', target_is_directory=True)
    run.host_bindings = {'lease-one': {'path': str(lease), 'mode': 'rw'}}
    assert place(run, volumes={str(lease / 'escape'): {'bind': '/home/login'}})['ok'] is False
    assert calls == []


def test_readonly_files_are_host_materialized_and_removed_on_close__D05(boundary):
    run, client, _calls = boundary
    ack = place(run, readonly_files={'/etc/run-policy.json': '{"enabled":true}'})
    assert ack['ok'] is True
    volumes = client.containers.ran[-1].run_kwargs['volumes']
    [(source, binding)] = volumes.items()
    path = Path(source)
    assert binding == {'bind': '/etc/run-policy.json', 'mode': 'ro'}
    assert path.read_text() == '{"enabled":true}'
    assert not path.is_relative_to(Path(run.workspace_dir))
    assert path.parent.stat().st_mode & 0o777 == 0o700
    run.environment_service.close(ack['env_id'])
    assert not path.exists() and not path.parent.exists()


def test_readonly_files_are_removed_when_container_start_fails__D05(boundary, monkeypatch):
    run, client, _calls = boundary
    paths = []

    def fail(*args, **kwargs):
        paths.extend(Path(path) for path in kwargs['volumes'])
        raise RuntimeError('fake placement failure')

    monkeypatch.setattr(client.containers, 'run', fail)
    ack = place(run, readonly_files={'/etc/run-policy.json': '{}'})
    assert ack['ok'] is False
    assert len(paths) == 1
    assert all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.parametrize('target,value', [
    ('relative', '{}'), ('/work/policy', '{}'), ('/work', '{}'), ('/', '{}'),
    ('/etc/policy:rw', '{}'), ('/etc/../policy', '{}'), ('/etc/policy', {'host_path': '/secret'}),
])
def test_readonly_file_destination_cannot_shadow_workspace__D05(boundary, target, value):
    run, _client, calls = boundary
    ack = place(run, mount='/work', readonly_files={target: value})
    assert ack['ok'] is False
    assert calls == []


@pytest.mark.parametrize('case', ['readonly_workspace', 'readonly_root', 'mount', 'volume'])
def test_double_slash_container_paths_refuse_before_docker_P4F40(boundary, tmp_path, case):
    run, _client, calls = boundary
    fields = {'mount': '/work'}
    if case == 'readonly_workspace':
        fields['readonly_files'] = {'//work/policy': '{}'}
    elif case == 'readonly_root':
        fields['readonly_files'] = {'//': '{}'}
    elif case == 'mount':
        fields['mount'] = '//work'
    else:
        lease = tmp_path / 'selected-lease'
        lease.mkdir()
        run.host_bindings = {'selected': {'path': str(lease), 'mode': 'rw'}}
        fields['volumes'] = {str(lease): {'bind': '//account/data', 'mode': 'rw'}}
    ack = place(run, **fields)
    try:
        assert ack['ok'] is False
        assert calls == []
    finally:
        if ack['ok']:
            run.environment_service.close_all()


def test_boundary_uses_current_modules_after_api_reset__F0380_F1906(request):
    """Collection-time classes must not outlive the fixture's patched module."""
    from conftest import reset_api_modules

    reset_api_modules()
    current = request.getfixturevalue('boundary')
    test_placement_uses_workspace_and_entire_staged_app_tree__D05(current)
    from process_manager.environments import EnvironmentService as CurrentService
    from process_manager.ipc import IPCMixin as CurrentIPC

    run, _client, _calls = current
    assert type(run._environment_service) is CurrentService
    assert type(run).__bases__ == (CurrentIPC,)
