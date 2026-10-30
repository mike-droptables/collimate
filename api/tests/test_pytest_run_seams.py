"""Run boundary refusals and Docker probes use their declared contracts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize('docker,expected', [('none', []), ('limited', ['ended']), ('full', ['ended']), (None, ['ended'])])
def test_reap_skips_only_explicit_docker_none__F0393(monkeypatch, docker, expected):
    from routes.runs import helpers

    calls = []
    manager = SimpleNamespace(process=None, cell_id=None, permissions=SimpleNamespace(docker=docker))
    monkeypatch.setattr(helpers, '_reap_run_containers', calls.append)
    monkeypatch.setattr(helpers, '_reclaim_run_app_dir', lambda _: None)
    asyncio.run(helpers._reap_process('ended', manager))
    assert calls == expected


def test_runner_image_is_inspected_again_after_pruning__F0407(monkeypatch):
    import sandbox

    probe = AsyncMock(side_effect=[('present', ''), ('missing', ''), ('present', '')])
    build = AsyncMock()
    monkeypatch.setattr(sandbox, '_probe_docker_image', probe)
    monkeypatch.setattr(sandbox, '_build_image', build)

    async def scenario():
        for _ in range(3):
            await sandbox.ensure_image('test-pruned:latest', '/fake/context', lambda _: None)

    asyncio.run(scenario())
    assert probe.await_count == 3
    assert build.await_count == 1
    assert build.call_args.kwargs == {'refresh': True}


@pytest.mark.parametrize('status,detail', [(400, 'invalid saved settings'), (404, 'runner gone'), (503, 'stopping')])
def test_start_run_has_one_refusal_type_and_route_preserves_body__F0359(monkeypatch, status, detail):
    from fastapi import HTTPException
    from routes.runs import create
    from routes.runs.models import RunRequest
    from models import AppState, Channel
    import state

    channel = Channel(name='Starting')
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    channel_id = str(channel.id)

    async def refused(*args, **kwargs):
        raise HTTPException(status, detail)

    monkeypatch.setattr(create, '_start_run', refused)
    request = RunRequest(runner='runner', cell_id='cell')
    with pytest.raises(Exception) as caught:
        asyncio.run(create.start_run(channel_id, request))
    assert type(caught.value).__name__ == 'RunRefused'
    assert str(caught.value) == detail
    response = asyncio.run(create.create_run(channel_id, request))
    assert response.status_code == status
    assert json.loads(response.body) == {'detail': detail}
