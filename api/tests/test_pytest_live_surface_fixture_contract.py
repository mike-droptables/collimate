"""Browser live fixtures look surfaces up through their live metadata."""
from tests_live import conftest as live


def test_live_surface_lookup_uses_metadata_and_only_404_means_detached_D22():
    api = object.__new__(live.LiveApi)
    api.chq = 'channel'
    calls = []
    session = {'chat_id': 'chat', 'run_id': 'run', 'label': 'Review'}
    def request(method, path):
        calls.append((method, path))
        return session
    api.request = request
    assert api.surface('cell', '_runs/live-12345678/chat.chat') == session
    assert calls == [('GET', '/api/channels/channel/cells/cell/surfaces/_runs/live-12345678/chat.chat')]
    def missing(*args):
        raise live.ApiError('GET', 'surface', 404, 'not live')
    api.request = missing
    assert api.surface('cell', 'chat.chat') is None
    def failed(*args):
        raise live.ApiError('GET', 'surface', 503, 'unavailable')
    api.request = failed
    import pytest
    with pytest.raises(live.ApiError):
        api.surface('cell', 'chat.chat')
