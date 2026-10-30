"""D06: actual Launch/Web starts reject concrete offline impossibilities."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import docker
import pytest

from tests._workspace import GENERATORS


ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    spec = importlib.util.spec_from_file_location('_d06_' + relative.split('/')[0], GENERATORS / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launch(monkeypatch, tmp_path):
    module = load('launch/launch/run.py')
    import gen
    cfg = {'mode': 'oneshot', 'command': 'python app.py'}
    grant = SimpleNamespace(internet='none', update_packages=True)
    calls, logs = [], []
    monkeypatch.setattr(gen, 'config', lambda _: cfg)
    monkeypatch.setattr(gen, 'grant', lambda: grant)
    monkeypatch.setattr(gen, 'log', logs.append)
    monkeypatch.setitem(gen.__dict__, 'prompt', 'https://example.test/repo.git')
    monkeypatch.setattr(gen, 'config_folder_path', lambda name: str(tmp_path / name))
    image_spec = {'image': 'cached:1', 'build_context': None, 'base_image': None,
                  'base_context': None, 'build_args': {}}
    monkeypatch.setattr(module, '_resolve_image', lambda _: image_spec)
    present = {'cached:1'}

    def get(tag):
        calls.append(('image', tag))
        if tag not in present:
            raise docker.errors.ImageNotFound(tag)
        return object()

    client = SimpleNamespace(images=SimpleNamespace(get=get), close=lambda: None)
    monkeypatch.setattr(docker, 'from_env', lambda: client)

    async def mode(_cfg, *, image_spec=None):
        calls.append(('mode', _cfg['mode']))

    for name in ('_run_oneshot', '_run_terminal', '_run_app'):
        monkeypatch.setattr(module, name, mode)
    return module, cfg, grant, calls, logs, image_spec, present, tmp_path


@pytest.mark.parametrize('field,command', [
    ('command', 'git clone "$COLLIMATE_PROMPT" work'),
    ('startup_commands', 'sudo apt-get update && apt-get install -y git'),
    ('startup_commands', ['echo ready', 'python -m pip install requests']),
    ('serve_command', 'npm install && npm run dev'),
    ('command', 'curl -fsSL https://example.test/install.sh | sh'),
    ('command', 'uv pip install requests'),
    ('command', 'git clone example.test:repo.git work'),
])
def test_launch_refuses_network_config_before_mode__D06(launch, field, command):
    module, cfg, _, calls, logs, *_ = launch
    cfg[field] = command
    with pytest.raises(RuntimeError, match='internet: none'):
        asyncio.run(module.run())
    assert not calls, 'Known network setup must refuse before even image inspection or mode side effects'
    assert 'internet: full' in logs[-1]


def test_launch_refuses_network_setup_script_before_mode__D06(launch):
    module, _, _, calls, _, _, _, path = launch
    (path / 'scripts').mkdir()
    (path / 'scripts' / 'setup.sh').write_text('npm ci\n')
    with pytest.raises(RuntimeError, match='internet: none'):
        asyncio.run(module.run())
    assert not calls


@pytest.mark.parametrize('command', [
    'python app.py', 'git clone ./local-repo work',
    'pip install --no-index --find-links wheels mypackage',
    'npm install --offline', 'echo "npm install"',
    'apt-get install --no-download foo', 'apk add --no-network foo',
])
def test_launch_keeps_offline_commands_and_cached_images__D06(launch, command):
    module, cfg, _, calls, _, spec, _, _ = launch
    cfg['command'] = command
    spec.update(base_image='pruned:1', base_context='/unused/pruned-context')
    asyncio.run(module.run())
    assert calls == [('image', 'cached:1'), ('mode', 'oneshot')]


@pytest.mark.parametrize('level', ['full', 'proxy'])
def test_launch_online_grants_do_not_preprobe_images__D06(launch, level):
    module, cfg, grant, calls, *_ = launch
    grant.internet = level
    cfg['command'] = 'npm install'
    asyncio.run(module.run())
    assert calls == [('mode', 'oneshot')]


def test_launch_missing_registry_image_refuses_before_mode__D06(launch):
    module, _, _, calls, _, _, present, _ = launch
    present.clear()
    with pytest.raises(RuntimeError, match='pull.*internet: full'):
        asyncio.run(module.run())
    assert not any(kind == 'mode' for kind, _ in calls)


@pytest.mark.parametrize('dockerfile,present,allowed', [
    ('FROM scratch\nCOPY app /app\n', set(), True),
    ('ARG BASE=local:1\nFROM ${BASE} AS first\nRUN echo ready\nFROM first\nCOPY --from=first /a /b\n', {'local:1'}, True),
    ('FROM remote:1\n', set(), False),
    ('FROM local:1\nRUN apt-get update\n', {'local:1'}, False),
    ('FROM local:1\nADD https://example.test/file /file\n', {'local:1'}, False),
    ('FROM local:1\nCOPY --from=remote:1 /a /b\n', {'local:1'}, False),
    ('FROM local:1\nRUN pip install --no-index --find-links=/wheels requests\n', {'local:1'}, True),
])
def test_launch_uncached_build_checks_actual_network_requirements__D06(launch, dockerfile, present, allowed):
    module, _, _, calls, _, spec, local, path = launch
    local.clear()
    local.update(present)
    build = path / 'build'
    build.mkdir()
    (build / 'Dockerfile').write_text(dockerfile)
    spec['build_context'] = str(build)
    if allowed:
        asyncio.run(module.run())
        assert calls[-1] == ('mode', 'oneshot')
    else:
        with pytest.raises(RuntimeError, match='internet: none'):
            asyncio.run(module.run())
        assert not any(kind == 'mode' for kind, _ in calls)


def test_launch_version_lookup_uses_internet_grant__D06(monkeypatch, tmp_path):
    module = load('launch/launch/run.py')
    import gen
    from _env_shared import upstream
    calls = []
    monkeypatch.setattr(gen, 'grant', lambda: SimpleNamespace(internet='none', update_packages=True))
    monkeypatch.setattr(gen, 'packages', lambda: {})
    monkeypatch.setattr(upstream, 'resolve_requests', lambda *args, **kwargs: calls.append(kwargs) or {})
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text('FROM scratch\nARG UV_VERSION=1\n')
    module._requested_versions(str(dockerfile))
    assert calls[0]['allow_network'] is False


@pytest.mark.parametrize('prompt', ['', 'https://example.test'])
@pytest.mark.parametrize('internet', ['none', 'full', 'proxy'])
def test_web_refuses_offline_before_daemon_even_blank_url__D06(monkeypatch, prompt, internet):
    module = load('web/web/web.py')
    import gen
    calls, logs = [], []
    monkeypatch.setattr(gen, 'grant', lambda: SimpleNamespace(internet=internet))
    monkeypatch.setattr(gen, 'config', lambda _: {})
    monkeypatch.setattr(gen, 'config_folder', lambda _: {})
    monkeypatch.setitem(gen.__dict__, 'prompt', prompt)
    monkeypatch.setattr(gen, 'log', logs.append)

    async def start(**kwargs):
        calls.append(kwargs)

    monkeypatch.setitem(sys.modules, '_web_shared.daemon', SimpleNamespace(run_web_session=start))
    if internet == 'none':
        with pytest.raises(RuntimeError, match='internet: none'):
            asyncio.run(module.run())
        assert not calls and 'internet: full' in logs[-1]
    else:
        asyncio.run(module.run())
        assert calls[0]['url'] == prompt


def test_offline_build_disables_upstream_refresh_before_placement__D06(launch, monkeypatch):
    module, _, _, _, _, spec, present, path = launch
    present.clear()
    build = path / 'build'
    build.mkdir()
    (build / 'Dockerfile').write_text('FROM scratch\nCOPY app /app\n')
    spec['build_context'] = str(build)
    seen = []
    async def start(_cfg, *, image_spec=None):
        seen.append(image_spec)
    monkeypatch.setattr(module, '_run_oneshot', start)
    asyncio.run(module.run())
    assert seen[0].get('refresh') is False


@pytest.mark.parametrize('source', [
    'FROM\tremote:1\n',
    'FROM scratch\nRUN ["pip", "install", "requests"]\n',
    'FROM scratch\nRUN --mount=type=cache,target=/x npm install\n',
    'FROM scratch\nRUN apt-get update --no-download\n',
])
def test_offline_build_reads_dockerfile_command_forms__D06(launch, source):
    module, _, _, calls, _, spec, present, path = launch
    present.clear()
    build = path / 'build'
    build.mkdir()
    (build / 'Dockerfile').write_text(source)
    spec['build_context'] = str(build)
    with pytest.raises(RuntimeError, match='internet: none'):
        asyncio.run(module.run())
    assert not any(kind == 'mode' for kind, _ in calls)
