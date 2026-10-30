"""HTTP reverse proxy (routes/proxy.py) error handling."""
import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.proxy as proxy
import run_registry as runs

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "proxy:primary"))
OTHER_CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "proxy:other"))


@pytest.fixture(autouse=True)
def registered_channels(fresh_cwd, monkeypatch):
    import state
    from models import AppState, Channel
    primary = Channel(id=CHANNEL, name="Primary")
    other = Channel(id=OTHER_CHANNEL, name="Other")
    monkeypatch.setattr(state, "state", AppState(channels=[primary, other], active_channel_id=primary.id))


_PORT = 45123


@pytest.fixture(autouse=True)
def _live_run():
    runs.active_managers["run1"] = SimpleNamespace(channel=CHANNEL, bound_ports={_PORT}, backend=None)
    yield
    runs.active_managers.pop("run1", None)


def _app_with_failing_upstream(monkeypatch, exc, **client_kwargs):
    """A bare app carrying only the proxy router, whose upstream raises
    ``exc``; returns (TestClient, list of clients that were closed)."""

    def handler(request):
        raise exc

    real_client = httpx.AsyncClient
    closed = []

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        c = real_client(**kwargs)
        orig_aclose = c.aclose

        async def aclose():
            closed.append(c)
            await orig_aclose()

        c.aclose = aclose
        return c

    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)
    app = FastAPI()
    app.include_router(proxy.router)
    return TestClient(app, **client_kwargs), closed


def test_upstream_protocol_error_is_502_and_closes_client(monkeypatch):
    client, closed = _app_with_failing_upstream(
        monkeypatch,
        httpx.RemoteProtocolError("Server disconnected without sending a response"),
    )
    r = client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/index.html")
    assert r.status_code == 502
    assert len(closed) == 1


def test_unexpected_error_still_closes_client(monkeypatch):
    client, closed = _app_with_failing_upstream(
        monkeypatch, ValueError("boom"), raise_server_exceptions=False,
    )
    r = client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/index.html")
    assert r.status_code == 500
    assert len(closed) == 1


def _app_with_upstream(monkeypatch, handler):
    """A bare app carrying only the proxy router, whose upstream is ``handler``."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)
    app = FastAPI()
    app.include_router(proxy.router)
    return TestClient(app)


@pytest.fixture
def _server_token():
    from auth_middleware import auth
    saved = auth._token
    auth.configure("s3cret-token")
    yield "s3cret-token"
    auth._token = saved


def test_server_credentials_never_reach_the_upstream(monkeypatch, _server_token):
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, text="ok")

    client = _app_with_upstream(monkeypatch, handler)
    r = client.get(
        f"/proxy/{CHANNEL}/run1/{_PORT}/index.html",
        headers={"Authorization": f"Bearer {_server_token}", "Cookie": "other=1"},
    )
    assert r.status_code == 200
    assert "authorization" not in seen[0]
    assert seen[0]["cookie"] == "other=1"


def test_proxied_apps_own_authorization_passes_through(monkeypatch, _server_token):
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, text="ok")

    client = _app_with_upstream(monkeypatch, handler)
    r = client.get(
        f"/proxy/{CHANNEL}/run1/{_PORT}/index.html",
        headers={"Authorization": "Basic x", "Cookie": "app_session=1"},
    )
    assert r.status_code == 200
    assert seen[0]["authorization"] == "Basic x"
    assert seen[0]["cookie"] == "app_session=1"


def test_each_upstream_set_cookie_stays_a_separate_header(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers=[("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")],
            text="ok",
        )

    client = _app_with_upstream(monkeypatch, handler)
    r = client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/index.html")
    assert r.status_code == 200
    assert r.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]


def test_origin_wide_headers_never_reach_the_browser__F1935(monkeypatch):
    """A proxied app shares the app's origin: it may not widen a service
    worker's scope past its run and port, wipe the
    app's storage, or install a Network Error Logging policy that reports
    the URLs of later app requests to its own endpoint. Its own cookies and
    headers pass through."""
    def handler(request):
        return httpx.Response(
            200,
            headers=[
                ("service-worker-allowed", "/"),
                ("clear-site-data", '"*"'),
                ("nel", '{"report_to":"x","max_age":31536000,"success_fraction":1.0}'),
                ("report-to", '{"group":"x","max_age":31536000,"endpoints":[{"url":"https://e.test/r"}]}'),
                ("reporting-endpoints", 'x="https://e.test/r"'),
                ("set-cookie", "session=app; Path=/"),
                ("x-app-header", "kept"),
            ],
            text="ok",
        )

    client = _app_with_upstream(monkeypatch, handler)
    r = client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/sw.js")
    assert r.status_code == 200
    assert r.headers['service-worker-allowed'] == f'/proxy/{CHANNEL}/run1/{_PORT}/'
    for dropped in ("clear-site-data",
                    "nel", "report-to", "reporting-endpoints"):
        assert dropped not in r.headers, dropped
    assert r.headers.get_list("set-cookie") == ["session=app; Path=/"]
    assert r.headers["x-app-header"] == "kept"


@pytest.mark.parametrize('requested', ['/', '/proxy/other/run2/6000/', 'https://other.test/'])
def test_service_worker_scope_is_this_run_and_port_P4F06(monkeypatch, requested):
    client = _app_with_upstream(monkeypatch, lambda request: httpx.Response(
        200, headers={'Service-Worker-Allowed': requested}, text='worker'))
    response = client.get(f'/proxy/{CHANNEL}/run1/{_PORT}/nested/sw.js')
    assert response.headers['service-worker-allowed'] == f'/proxy/{CHANNEL}/run1/{_PORT}/'
    assert 'clear-site-data' not in response.headers


def test_an_exited_run_answers_410_through_its_run_end_cleanup__F0321(monkeypatch):
    """A stopped run stays registered until its run-end cleanup is done, but
    once its process has exited its ports are free for the next run: a stale
    renderer URL naming it must miss (410), never reach a new run's daemon
    that rebound the port."""
    def handler(request):
        return httpx.Response(200, text="new run's daemon")

    client = _app_with_upstream(monkeypatch, handler)
    proc = SimpleNamespace(returncode=None)
    runs.active_managers["run1"] = SimpleNamespace(
        channel=CHANNEL, bound_ports={_PORT}, backend=None, process=proc)
    assert client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/index.html").status_code == 200
    proc.returncode = -15
    assert client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/index.html").status_code == 410


def test_wrong_channel_cannot_proxy_a_live_run__F0374(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text="ok")

    runs.active_managers["run1"].channel = CHANNEL
    client = _app_with_upstream(monkeypatch, handler)
    assert client.get(f"/proxy/{OTHER_CHANNEL}/run1/{_PORT}/x").status_code == 410
    assert seen == []
    assert client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/x").status_code == 200


def test_request_clients_share_transport_without_closing_it__F0328(monkeypatch):
    class Transport(httpx.MockTransport):
        def __init__(self):
            self.closed = False
            super().__init__(lambda request: httpx.Response(200, text="ok"))

        async def handle_async_request(self, request):
            assert not self.closed
            return await super().handle_async_request(request)

        async def aclose(self):
            self.closed = True

    transport = Transport()
    clients = []
    pool_options = []
    real_client = httpx.AsyncClient

    def pool_factory(**kwargs):
        pool_options.append(kwargs)
        return transport

    def factory(**kwargs):
        clients.append(dict(kwargs))
        kwargs.setdefault("transport", httpx.MockTransport(lambda request: httpx.Response(200)))
        return real_client(**kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncHTTPTransport", pool_factory)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)
    monkeypatch.setattr(proxy, "_transport", None, raising=False)
    runs.active_managers["run1"].channel = CHANNEL
    app = FastAPI()
    app.include_router(proxy.router)
    with TestClient(app) as client:
        for _ in range(2):
            assert client.get(f"/proxy/{CHANNEL}/run1/{_PORT}/x").status_code == 200
        assert not transport.closed
    assert len(clients) == 2
    assert all(c.get("transport") is not None and c.get("trust_env") is False for c in clients)
    assert clients[0]["transport"] is clients[1]["transport"]
    assert len(pool_options) == 1 and pool_options[0]["trust_env"] is False
    asyncio.run(proxy.close_proxy_transport())
    assert transport.closed
