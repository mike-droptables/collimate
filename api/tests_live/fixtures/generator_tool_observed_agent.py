"""Test-only entrypoint; the accepted agent generator, its model and its tools
execute unchanged."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load observed worker module')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_sources(root, provenance):
    """Verify the actual materialized accepted bytes before importing them."""
    for relative, expected in provenance['sources'].items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise RuntimeError('Unsafe observed source path')
        target = root / path
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('Observed source escapes its package')
        if digest(target.read_bytes()) != expected:
            raise RuntimeError('Observed worker source hash differs')


def private_log(path, content, *, category='log'):
    """Use the accepted anchored 0600 writer before normal log staging."""
    import gen
    hygiene = importlib.import_module('_agent_shared.secret_hygiene')
    files = importlib.import_module('_agent_shared.harness_files')
    hygiene.refresh()
    content = hygiene.scrub_persisted(path, content)
    files.write_native_file(gen.workspace_path(), path, content.encode('utf-8'))
    return hygiene.write_log(path, content, category=category)


class HostChildObserver:
    """Observe the real host command boundary; never replace its return value."""
    def __init__(self, identity, write_log, provenance):
        self.identity = identity
        self.write_log = write_log
        self.provenance = provenance
        self.binding = None
        self.executions = []

    def retain(self):
        body = {**self.identity(), 'provenance': self.provenance, 'binding': self.binding,
                'executions': self.executions}
        self.write_log('_agent/d23-host/observation.json', json.dumps(body), category='log')

    def location(self):
        import gen
        from importlib.resources import files
        package = files('collimate')
        assert isinstance(package, Path) and package.is_dir()
        body = {**self.identity(), 'provenance': self.provenance,
                'workspace': gen.workspace_path(), 'sdk': str(package.resolve().parent)}
        self.write_log('_agent/d23-host/location.json', json.dumps(body), category='log')

    def prepared(self, context, profile):
        import gen
        from importlib.resources import files
        assert context['auth'] == 'api_key' and context['provider'] == 'anthropic'
        assert context['runtime'] == 'standard'
        assert context['model'] == 'claude-haiku-4-5-20251001'
        host = context['host_harness']
        assert set(host) == {'path', 'binary', 'version'}
        assert host['binary'] == 'claude' and host['version'] == '2.1.281'
        assert Path(host['path']).is_absolute()
        assert profile.locus == 'host' and profile.cwd == gen.workspace_path()
        assert profile.env_obj is None and profile.adapter_version == '0.81.1'
        package = files('collimate')
        assert isinstance(package, Path) and package.is_dir()
        binding = {'workspace': profile.cwd, 'sdk': str(package.resolve().parent),
                   'host_harness': dict(host), 'model': context['model'],
                   'adapter_version': profile.adapter_version, 'locus': 'host'}
        assert self.binding is None or self.binding == binding
        self.binding = binding
        self.retain()

    def wrap(self, original):
        async def observed(child):
            assert self.binding is not None
            workspace = Path(self.binding['workspace'])
            assert child.cwd == str(workspace / 'draft')
            assert child.argv[:3] == ['uv', 'run', '--no-project']
            assert child.argv[-1] == str(workspace / 'draft/main.py')
            assert child.env['PYTHONPATH'] == self.binding['sdk']
            assert Path(child.host_run_dir).parent == workspace / '.collrun'
            assert Path(child.host_run_dir).name.startswith('forward-')
            assert child.run_dir == child.host_run_dir
            record = {'argv': list(child.argv), 'cwd': child.cwd,
                      'run_dir': child.host_run_dir, 'sdk': self.binding['sdk'],
                      'spawn': None, 'returned': False}
            self.executions.append(record)
            self.retain()
            spawn = asyncio.create_subprocess_exec
            async def own_spawn(*argv, **kwargs):
                process = await spawn(*argv, **kwargs)
                if list(argv) == child.argv and kwargs.get('cwd') == child.cwd:
                    try:
                        assert record['spawn'] is None
                        assert kwargs.get('start_new_session') is True
                        # Read only this exact returned process's identity,
                        # never arbitrary argv/environ or provider material.
                        raw = Path(f'/proc/{process.pid}/stat').read_text()
                        record['spawn'] = {'pid': process.pid,
                            'start': raw.rsplit(') ', 1)[1].split()[19]}
                        self.retain()
                    except Exception as exc:
                        # The real owner must receive its process and reap it.
                        # Fail the evidence gate only after that owner settles.
                        record['observation_error'] = type(exc).__name__
                return process
            asyncio.create_subprocess_exec = own_spawn
            failure = None
            try:
                result = await original(child)
                assert 'observation_error' not in record
                assert record['spawn'] is not None
                record.update(returned=True, exit_code=result[0])
                return result
            except BaseException as exc:
                failure = exc
                raise
            finally:
                asyncio.create_subprocess_exec = spawn
                try:
                    self.retain()
                except Exception:
                    if failure is None:
                        raise
                    failure.add_note('D23 host observation could not be staged')
        return observed


class TurnObserver:
    def __init__(self, capture_module, *, backend, provenance, identity, scrub, write_log):
        class SelectedCapture(capture_module.Capture):
            def _scrub(self, value):
                return scrub(value)
        self.capture_type = SelectedCapture
        self.backend = backend
        self.provenance = provenance
        self.identity = identity
        self.write_log = write_log
        self.current = None
        self.ordinal = 0

    def observe(self, direction, message):
        # No filesystem, host IPC, account lookup, task or queue here. The
        # existing observer deep-copies selected records before scrubbing.
        if self.current is not None:
            self.current.observe(direction, message)

    def wrap(self, original):
        async def observed(*args, **kwargs):
            if self.current is not None:
                raise RuntimeError('overlapping observed native turns')
            if kwargs.get('backend') != self.backend:
                raise RuntimeError('Observed native backend differs')
            capture = self.capture_type()
            self.current = capture
            self.ordinal += 1
            ordinal = self.ordinal
            outcome, failure = 'normal', None
            try:
                return await original(*args, **kwargs)
            except BaseException as exc:
                failure = exc
                outcome = 'cancel' if isinstance(exc, asyncio.CancelledError) else 'error'
                raise
            finally:
                self.current = None
                try:
                    body = {**self.identity(), 'backend': self.backend, 'ordinal': ordinal,
                            'provenance': self.provenance, 'outcome': outcome,
                            'records': capture.records,
                            'permission_records': capture.permission_records}
                    self.write_log(f'_agent/d23-native/turn-{ordinal:04d}.json',
                                   json.dumps(body, ensure_ascii=False), category='log')
                except Exception:
                    if failure is None:
                        raise
                    # Preserve the original cancellation/transport exception;
                    # absent evidence will independently fail the parent test.
                    failure.add_note('D23 native observation could not be staged')
        return observed


async def run_observed(session, here, acp_run, capture_module, provenance, *,
                       identity, scrub, write_log):
    """Run the accepted agent generator at ``here`` on the surface its
    runner sets (chat: D23 drives its turns over the served chat), with
    every turn observed: the session's engine turn
    (``session.entry.run_agent``) and the backend's ACP profile are wrapped
    for the run and restored after it."""
    backend = provenance['backend']
    router = TurnObserver(capture_module, backend=backend, provenance=provenance,
                          identity=identity, scrub=scrub, write_log=write_log)
    entry = session.entry
    original_turn = entry.run_agent
    original_profile = acp_run._PROFILES[backend]
    host = HostChildObserver(identity, write_log, provenance) if backend == 'claude-code' else None
    child_owner, original_command = None, None
    if host is not None:
        child_owner = importlib.import_module('_agent_shared.generator_test')
        original_command = child_owner._host_command
        setattr(child_owner, '_host_command', host.wrap(original_command))

    async def prepare(context):
        profile = await original_profile(context)
        if host is not None:
            host.prepared(context, profile)
            # Publish the existing real zero-child snapshot before a native
            # permission card can pause the first turn's normal update flush.
            import gen
            await gen.commit()
        return capture_module.observed_profile(profile, router)

    entry.run_agent = router.wrap(original_turn)
    acp_run._PROFILES[backend] = prepare
    try:
        if host is not None:
            host.location()
        return await session.run(here=here)
    finally:
        entry.run_agent = original_turn
        acp_run._PROFILES[backend] = original_profile
        if host is not None:
            assert child_owner is not None
            setattr(child_owner, '_host_command', original_command)


def main():
    here = Path(__file__).resolve().parent
    provenance = json.loads((here / 'd23-observation.json').read_text())
    root = here
    for _ in range(provenance['root_depth']):
        root = root.parent
    verify_sources(root, provenance)
    capture = load_module(here / 'd23_acp_capture.py', '_d23_native_capture')
    accepted = root / provenance['entrypoint']
    # Loading the accepted entrypoint runs its imports, never its gen.main.
    load_module(accepted, '_d23_accepted_agent')
    import gen
    session = importlib.import_module('_agent_shared.session')
    acp_run = importlib.import_module('_agent_shared.acp_run')
    hygiene = importlib.import_module('_agent_shared.secret_hygiene')

    def identity():
        info = gen.run_info()
        return {'run_id': info.run_id, 'cell_id': info.cell_id}

    async def run():
        return await run_observed(session, str(accepted.parent), acp_run, capture, provenance,
                                  identity=identity, scrub=hygiene.scrub_object, write_log=private_log)
    gen.main(run)


if __name__ == '__main__':
    main()
