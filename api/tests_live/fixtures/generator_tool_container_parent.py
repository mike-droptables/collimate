"""Unpaid real app parent: accepted command code, real container and host IPC."""
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import uuid

import gen

module = importlib.import_module('_agent_shared.generator_test')
sys.path.insert(0, str(Path(__file__).parent))
env_shared = importlib.import_module('_env_shared')


def _environment():
    """The shared base image with the workspace mounted read-write at /work:
    the container a container-locus test_generator runs its child in."""
    context = env_shared.base_context()
    if not context:
        raise RuntimeError('the shared base build context (_env_shared/base) is missing')
    return env_shared.DockerEnvironment(
        image=env_shared.derive_image_tag(env_shared.BASE_NAME, context), mount='/work', mount_mode='rw',
        workspace=gen.workspace_path(), build_context=context,
        name_prefix='collimate-generator-test', kind='generator_test')


async def run():
    info = gen.run_info()
    environment = await asyncio.to_thread(_environment)
    try:
        command = ['python', '-c',
            'import gen,json,os,pathlib; p=pathlib.Path(gen.__file__); '
            'print(json.dumps({"uid":os.getuid(),"main":callable(gen.main),'
            '"owner":p.stat().st_uid,"writable":os.access(p,os.W_OK),'
            '"directory_writable":os.access(p.parent,os.W_OK)})); '
            'assert callable(gen.main) and p.stat().st_uid==0 and not os.access(p,os.W_OK) '
            'and not os.access(p.parent,os.W_OK)']
        code, output = await asyncio.to_thread(environment.exec, command,
            user=f'{os.getuid()}:{os.getgid()}')
        if isinstance(output, bytes):
            output = output.decode()
        import_proof = {'exit_code': code, 'output': output}
        settings = {'overlay': 'child-value', 'marker': 'unpaid-' + uuid.uuid4().hex,
                    'parent_run_id': info.run_id, 'cell_id': info.cell_id,
                    'expected_prompt': 'private-unpaid-' + uuid.uuid4().hex,
                    'commands': gen.grant().commands, 'expected_mount': environment.mount}
        result = await module.run_test(module.CommandContext('container', env_obj=environment),
            commands=gen.grant().commands, path='draft/probe.generator',
            settings=settings, prompt=settings['expected_prompt'])
        inspect = '''import json,os
from pathlib import Path
leaks=[]
for path in Path('/proc').iterdir():
    if not path.name.isdigit() or int(path.name)==os.getpid(): continue
    try:
        command=(path/'cmdline').read_bytes().replace(b'\\0',b' ')
        state=(path/'stat').read_text().rsplit(') ',1)[1].split()[0]
    except (FileNotFoundError,PermissionError,ProcessLookupError): continue
    if state=='Z' or b'/draft/main.py' in command or (b'command.ready' in command and b'command.result' in command):
        leaks.append({'pid':int(path.name),'state':state})
print(json.dumps(leaks))
'''
        remaining = []
        for _ in range(20):
            status, text = await asyncio.to_thread(environment.exec,
                ['python', '-c', inspect], user=f'{os.getuid()}:{os.getgid()}')
            assert status == 0
            remaining = json.loads(text)
            if not remaining:
                break
            await asyncio.sleep(.05)
        root = Path(__file__).parent
        sources = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for folder in ('_agent_shared', '_env_shared')
                   for path in (root / folder).rglob('*')
                   if path.is_file() and '__pycache__' not in path.parts
                   and path.suffix not in ('.pyc', '.pyo')}
        gen.update_file('parent-result.json', json.dumps({
            'result': result, 'settings': settings, 'import_proof': import_proof,
            'source_hashes': sources, 'uid': os.getuid(),
            'remaining_child_processes': remaining}, sort_keys=True))
        await gen.commit()
        gen.phase('awaiting_input')
        await gen.wait_until_cancelled()
    finally:
        await asyncio.to_thread(environment.close)


gen.main(run)
