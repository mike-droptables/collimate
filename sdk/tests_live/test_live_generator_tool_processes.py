"""P4F31 unpaid actual Docker process-tree cleanup, with no model or fake IPC."""
import asyncio
import importlib
import json
import os
from types import SimpleNamespace

import pytest

from .conftest import live_container_labels

pytestmark = pytest.mark.live


@pytest.mark.parametrize('ending', ['normal', 'cancel', 'timeout'])
def test_actual_container_reaps_direct_and_adopted_descendants_P4F31(tmp_path, ending):
    image = os.environ.get('COLLIMATE_TEST_COMMAND_IMAGE')
    if not image or os.environ.get('COLLIMATE_LIVE_DOCKER') != '1':
        pytest.skip('Set explicit command image and COLLIMATE_LIVE_DOCKER=1')
    import docker
    module = importlib.import_module('_agent_shared.generator_test')
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    descendant = ('import os,time\nfrom pathlib import Path\n'
                  'Path("descendant.pid").write_text(str(os.getpid()))\n'
                  'while True: time.sleep(.02)\n')
    source = ('import os,subprocess,sys,time\nfrom pathlib import Path\n'
              'Path("child.pid").write_text(str(os.getpid()))\n'
              f'subprocess.Popen([sys.executable,"-c",{descendant!r}],start_new_session=True)\n'
              'while not Path("descendant.pid").exists(): time.sleep(.01)\n'
              + ('print("normal complete")\n' if ending == 'normal' else 'while True: time.sleep(.02)\n'))
    (tmp_path / 'child.py').write_text(source)
    client = docker.from_env()
    container = None
    try:
        # Deliberately no init/reaper: the supervisor must own its descendants.
        container = client.containers.run(image, ['sleep', 'infinity'], detach=True,
            volumes={str(tmp_path): {'bind': '/work', 'mode': 'rw'}}, labels=live_container_labels())
        class Environment:
            def exec(self, argv, **kwargs):
                command = argv
                if kwargs.get('timeout'):
                    command = ['timeout', str(kwargs['timeout']), *argv]
                result = container.exec_run(command, workdir=kwargs.get('cwd'),
                    environment=kwargs.get('env'), user=kwargs.get('user'),
                    detach=kwargs.get('detach', False))
                if kwargs.get('detach'):
                    return 0, ''
                return result.exit_code, result.output.decode()
        child = SimpleNamespace(argv=['python', '/work/child.py'], cwd='/work', env={},
            run_dir='/work/private', host_run_dir=str(private))
        async def check():
            task = asyncio.create_task(module._container_command(Environment(), child))
            for _ in range(150):
                if (tmp_path / 'descendant.pid').exists():
                    break
                await asyncio.sleep(.02)
            assert (tmp_path / 'descendant.pid').exists()
            if ending == 'normal':
                assert await task == (0, 'normal complete\n')
            elif ending == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, timeout=.05)
        asyncio.run(check())
        ready = json.loads((private / 'command.ready').read_text())
        pids = [ready['pid'], int((tmp_path / 'child.pid').read_text()),
                int((tmp_path / 'descendant.pid').read_text())]
        inspect = ('import json,sys; from pathlib import Path; '
                   'print(json.dumps({p:(Path("/proc")/p).exists() for p in sys.argv[1:]}))')
        result = container.exec_run(['python', '-c', inspect, *map(str, pids)])
        assert result.exit_code == 0
        assert json.loads(result.output) == {str(pid): False for pid in pids}, result.output
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
