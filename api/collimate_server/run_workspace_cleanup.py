"""Owned run-directory receipts and conservative post-crash reclamation.

A receipt's phase is ``staged``, ``launching``, ``running`` or
``harvested``: the last is written by crash recovery once it harvested (or
rescued) an interrupted run's git state, just before it deletes the
workspace, so a workspace whose deletion failed is only reclaimed later,
never harvested twice."""
from __future__ import annotations
import json
import logging
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from collimate import fs

import collimate_server.readonly_trees as readonly_trees
from collimate_server.process_signals import start_ticks

_MARKER_DIRECTORY = '.collrun'
_MARKER_FILE = 'run-owner.json'
MARKER = f'{_MARKER_DIRECTORY}/{_MARKER_FILE}'


@contextmanager
def _marker_parent_fd(directory, *, create=False):
    """Pin each owned path component before touching the private receipt."""
    root_fd = os.open(directory.parent, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
    try:
        directory_fd = os.open(directory.name, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW,
                               dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        if create:
            try:
                os.mkdir(_MARKER_DIRECTORY, dir_fd=directory_fd)
            except FileExistsError:
                pass
        marker_fd = os.open(_MARKER_DIRECTORY, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW,
                            dir_fd=directory_fd)
        try:
            yield directory_fd, marker_fd
        finally:
            os.close(marker_fd)
    finally:
        os.close(directory_fd)


def _refuse_symbolic_marker(marker_fd):
    try:
        mode = os.stat(_MARKER_FILE, dir_fd=marker_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ValueError('symbolic marker')


def _read_receipt(directory):
    with _marker_parent_fd(directory) as (_, marker_fd):
        marker_file_fd = os.open(_MARKER_FILE, os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK,
                                 dir_fd=marker_fd)
        with os.fdopen(marker_file_fd, 'r', encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('non-file marker')
            return json.load(stream)


def _valid_process_id(value):
    return type(value) is int and 0 < value <= 2**31 - 1


def _validate_receipt(receipt):
    if not isinstance(receipt, dict) or set(receipt) != {
        'version', 'data_dir', 'run_id', 'server_pid', 'server_start', 'phase', 'process_group',
    }:
        raise ValueError('unknown ownership receipt')
    if type(receipt['version']) is not int or receipt['version'] != 1:
        raise ValueError('unknown ownership version')
    if not isinstance(receipt['run_id'], str) or str(UUID(receipt['run_id'])) != receipt['run_id']:
        raise ValueError('invalid run identity')
    if receipt['phase'] not in ('staged', 'launching', 'running', 'harvested'):
        raise ValueError('unknown launch phase')
    if not _valid_process_id(receipt['server_pid']):
        raise ValueError('invalid server identity')
    # process_signals.start_ticks: /proc's clock ticks on Linux, or ``ps:``
    # and what ps says of the start where there is no /proc (macOS).
    start = receipt['server_start']
    if start is not None and (not isinstance(start, str) or not re.fullmatch(r'[0-9]+|ps:[ -~]{1,80}', start)):
        raise ValueError('invalid server start identity')
    group = receipt['process_group']
    if group is not None and not _valid_process_id(group):
        raise ValueError('invalid process group')


def stamp_directory(path, run_id, *, phase='staged', process_group=None):
    directory = Path(path).absolute()
    root = Path('.collimate/tmp').resolve()
    if directory.parent != root or directory.resolve() != directory:
        raise ValueError('Run directory must be an owned direct child')
    if not re.fullmatch(r'ws_' + re.escape(str(run_id)[:8]) + r'_[a-z0-9_]+', directory.name):
        raise ValueError('Run directory name does not match owner')
    receipt = dict(version=1, data_dir=str(Path.cwd().resolve()), run_id=str(run_id),
                   server_pid=os.getpid(), server_start=start_ticks(os.getpid()),
                   phase=phase, process_group=process_group)
    _validate_receipt(receipt)
    with _marker_parent_fd(directory, create=True) as (directory_fd, marker_fd):
        _refuse_symbolic_marker(marker_fd)
        temporary = f'.{_MARKER_FILE}.{secrets.token_hex(16)}.tmp'
        temporary_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fs.O_NOFOLLOW,
                               0o600, dir_fd=marker_fd)
        try:
            with os.fdopen(temporary_fd, 'w', encoding='utf-8') as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _MARKER_FILE, src_dir_fd=marker_fd, dst_dir_fd=marker_fd)
            os.fsync(marker_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=marker_fd)
            except FileNotFoundError:
                pass


def stamp_manager(manager, phase):
    """Stamp the run's workspace with ``phase`` and the process group (its
    generator tree is a shared stable tree, never a run's own)."""
    process = getattr(manager, 'process', None)
    group = None
    if process is not None and process.pid:
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            group = process.pid
    if getattr(manager, 'workspace_dir', None):
        stamp_directory(manager.workspace_dir, manager.run_id, phase=phase, process_group=group)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True


def _container_mounts():
    """None means the container ownership inventory could not be proved."""
    try:
        import docker
        client = docker.from_env()
        try:
            paths = []
            for container in client.containers.list(all=True):
                for mount in container.attrs.get('Mounts', []):
                    source = mount.get('Source')
                    if source:
                        paths.append(Path(source).resolve())
            return paths
        finally:
            client.close()
    except Exception:
        if not os.environ.get('DOCKER_HOST') and not Path('/var/run/docker.sock').exists():
            return []
        return None


def _recover(directory, receipt, recovered):
    """The recovery harvest of an interrupted run's workspace at its
    reclaim, under its channel's busy mark (none for a channel being
    created, changed or parked), then the receipt marked ``harvested`` for
    this server. False when it must wait for a later start."""
    from collimate_server.database import changing_channels, creating_channels
    from collimate_server.run_registry import channel_busy
    import collimate_server.git_harvest as git_harvest
    channel, cell_id = recovered[receipt['run_id']]
    import collimate_server.channel_paths as channel_paths
    try:
        missing = not channel_paths.db_path(channel).is_file()
    except channel_paths.ChannelUnavailable:
        missing = True
    if channel in creating_channels or channel in changing_channels or missing:
        return False
    try:
        with channel_busy(channel):
            harvested = git_harvest.recover(channel, cell_id, receipt['run_id'], str(directory))
    except Exception:
        logging.getLogger(__name__).exception('recovery harvest of run %s failed', receipt['run_id'])
        return False
    if harvested.unkept:
        logging.getLogger(__name__).warning('run %s: git history of %s neither saved nor rescued; its workspace '
                                            'waits for a later start', receipt['run_id'], ', '.join(harvested.unkept))
        return False
    stamp_directory(directory, receipt['run_id'], phase='harvested', process_group=receipt['process_group'])
    return True


def _release_recovered(recovered, run_id=None):
    """Drop ``run_id`` (every run, when None) from ``recovered`` and release
    the recovery lease of each cell no run left in it still waits on."""
    import collimate_server.repository_leases as repository_leases
    done = [run_id] if run_id is not None else list(recovered)
    cells = {recovered.pop(rid) for rid in done if rid in recovered}
    for channel, cell_id in cells - set(recovered.values()):
        lease = repository_leases.holder(channel, cell_id)
        if lease is not None and lease.label == repository_leases.RECOVERY:
            repository_leases.release_run(lease.run_id)


def reclaim_abandoned_directories(*, container_mounts=None, recovered=None, keep_unharvested=False):
    """Caller owns the data-dir lock and has reconciled interrupted runs.

    Refuse unknown markers, living owners/groups, unresolved launches, symlinks
    and directories still named by any container mount. Do not signal processes.

    ``recovered`` maps each interrupted run to recover to its ``(channel,
    cell_id)`` (``recover_interrupted_runs``): its workspace is harvested
    (``git_harvest.recover``) after every gate, just before it is deleted,
    and the cell's recovery lease released; on return every lease left (a
    kept workspace, an unproven container inventory) is released too, and
    that run's ``.git`` waits for a later start.

    ``keep_unharvested`` (Clear scratch, while the server runs) also keeps
    every workspace whose run got past ``staged`` and was not harvested:
    its git state waits for the next start's recovery.
    """
    recovered = dict(recovered or {})
    try:
        return _reclaim(container_mounts, recovered, keep_unharvested)
    finally:
        _release_recovered(recovered)


def _reclaim(container_mounts, recovered, keep_unharvested=False):
    root = Path('.collimate/tmp').absolute()
    if not root.is_dir() or root.resolve() != root:
        return dict(reclaimed=0, retained=0)
    mounts = _container_mounts() if container_mounts is None else container_mounts
    reclaimed = retained = 0
    for directory in root.iterdir():
        if not re.fullmatch(r'ws_[0-9a-f]{8}_[a-z0-9_]+', directory.name):
            continue
        if mounts is None or directory.is_symlink() or not directory.is_dir():
            retained += 1
            continue
        try:
            receipt = _read_receipt(directory)
            _validate_receipt(receipt)
            if (receipt['data_dir'] != str(Path.cwd().resolve())
                    or directory.name.split('_')[1] != str(receipt['run_id'])[:8]):
                raise ValueError('unproven owner')
            pid = receipt['server_pid']
            if _alive(pid):
                observed_start = start_ticks(pid)
                if observed_start is None or receipt['server_start'] is None or observed_start == receipt['server_start']:
                    raise ValueError('living or unproven owner')
            group = receipt['process_group']
            if receipt['phase'] == 'launching':
                raise ValueError('unresolved launch')
            if group is not None:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError('living process group')
            elif receipt['phase'] not in ('staged', 'harvested'):
                raise ValueError('unproven process exit')
            if any(directory == Path(mount) or directory in Path(mount).parents
                   or Path(mount) in directory.parents for mount in mounts):
                raise ValueError('container still references directory')
            if keep_unharvested and receipt['phase'] not in ('staged', 'harvested'):
                raise ValueError('waits for its recovery at the next start')
            if (receipt['phase'] != 'harvested'
                    and receipt['run_id'] in recovered):
                if not _recover(directory, receipt, recovered):
                    raise ValueError('channel not ready for recovery')
                _release_recovered(recovered, receipt['run_id'])
            # A workspace may hold a read-only .git.
            readonly_trees.remove_tree(str(directory))
            reclaimed += 1
        except (OSError, ValueError, KeyError, TypeError):
            retained += 1
    return dict(reclaimed=reclaimed, retained=retained)
