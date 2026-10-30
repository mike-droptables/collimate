"""Every nullable/required route lookup follows the shared channel rules."""
import asyncio

import pytest
from fastapi import HTTPException


def test_nullable_active_lookup_agrees_with_required_lookup__F0280(monkeypatch):
    import state
    from models import Channel

    channel = Channel(name='Live')
    monkeypatch.setattr(state.state, 'channels', [channel])
    for identity in [None, 'stale']:
        monkeypatch.setattr(state.state, 'active_channel_id', identity)
        assert state.find_active_channel() is None
        with pytest.raises(HTTPException) as caught:
            state.get_active_channel()
        assert caught.value.status_code == 404
    monkeypatch.setattr(state.state, 'active_channel_id', channel.id)
    assert state.find_active_channel() is channel
    assert state.get_active_channel() is channel


def test_settings_routes_use_the_shared_required_channel_guard__P0F04(monkeypatch):
    import channel_settings
    from models import Channel
    from routes import channels

    seen = []
    channel = Channel(name='Selected')

    def required(value):
        seen.append(value)
        return channel

    monkeypatch.setattr(channels, 'require_channel', required, raising=False)
    monkeypatch.setattr(channel_settings, 'load_channel_settings', lambda slug: {'slug': slug})
    monkeypatch.setattr(channel_settings, 'merge_channel_settings', lambda slug, body: {'slug': slug, **body})
    assert asyncio.run(channels.get_channel_settings('requested')) == {'slug': str(channel.id)}
    assert asyncio.run(channels.put_channel_settings('requested', {'theme': {}})) == {'slug': str(channel.id), 'theme': {}}
    assert seen == ['requested', 'requested']
