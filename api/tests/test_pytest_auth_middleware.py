"""Coverage for the opt-in bearer-token auth middleware.

The contract (auth_middleware.py): no token configured = the open loopback
server, untouched. Token configured = everything requires it as a bearer
header — routes, static assets, SSE, WebSockets — except GET /health, which
stays the pre-auth handshake. Refusals are a bare 401 (constant-time
compare), or a pre-accept ASGI rejection with 4401 (browser 1006).
POST /auth/session is the desktop
shell's probe (204 with the token, 404 when auth is off); the token is never
a cookie. An opaque ``Origin: null`` is refused even with the token, except
on the cell-file capability route.
"""
from __future__ import annotations

import pytest


TOKEN = "s3cret-token"


@pytest.fixture
def authed_client(fresh_cwd, monkeypatch):
    """A client against an app imported with COLLIMATE_TOKEN set.

    Mirrors the ``api_app``/``client`` fixtures, but the env var must be
    in place *before* main is imported (configure runs at import).
    """
    monkeypatch.setenv("COLLIMATE_TOKEN", TOKEN)
    import importlib
    from conftest import reset_api_modules

    reset_api_modules()
    main = importlib.import_module("main")

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def test_no_token_configured_is_open(client):
    """The local-first default: unset token = no auth anywhere."""
    assert client.get("/health").status_code == 200
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize('destination', ['image', 'script', 'empty'])
def test_cross_site_tokenless_plain_file_read_is_refused_P4F07(client, destination):
    path = f'/api/channels/{client.channel_id}/cells/nope/files/payload.js'
    response = client.get(path, headers={'Sec-Fetch-Site': 'cross-site',
        'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': destination})
    assert response.status_code == 403
    assert response.content == b''
    assert client.get(path, headers={'Sec-Fetch-Site': 'same-site'}).status_code == 404
    assert client.get(path).status_code == 404


def test_capability_read_retains_cross_site_access_P4F07(client):
    cap = client.get(f'/api/channels/{client.channel_id}/cells/nope/file_cap').json()['cap']
    response = client.get(f'/api/cellcap/{cap}/channels/{client.channel_id}/cells/nope/files/payload.js',
                          headers={'Sec-Fetch-Site': 'cross-site', 'Origin': 'null'})
    assert response.status_code == 404
    assert response.headers['access-control-allow-origin'] == '*'


def test_nonopaque_bearer_cross_site_read_is_unchanged_P4F07(authed_client):
    created = authed_client.post('/api/channels', params={'name': 'Bearer'},
                                 headers={'Authorization': f'Bearer {TOKEN}'})
    assert created.status_code == 200, created.text
    response = authed_client.get(f'/api/channels/{created.json()["id"]}/cells/nope/files/payload.js',
        headers={'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://foreign.test',
                 'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 404


def test_health_stays_public(authed_client):
    r = authed_client.get("/health")
    assert r.status_code == 200
    # The probe reveals only status/version/bridge.
    assert set(r.json().keys()) == {"status", "version", "bridge"}


def test_routes_401_without_token(authed_client):
    r = authed_client.get("/api/state")
    assert r.status_code == 401
    # Bare refusal: no detail for a prober to learn from.
    assert r.content == b""
    assert r.headers.get("www-authenticate") == "Bearer"


def test_ready_is_not_exempt(authed_client):
    assert authed_client.get("/ready").status_code == 401


def test_root_and_static_are_not_exempt(authed_client):
    """The UI itself is not public on a tokened server."""
    assert authed_client.get("/").status_code == 401


def test_bearer_header_authorizes(authed_client):
    r = authed_client.get("/api/state", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_wrong_token_401(authed_client):
    r = authed_client.get("/api/state", headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_scheme_must_be_bearer(authed_client):
    r = authed_client.get("/api/state", headers={"authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_websocket_refused_without_token__F1989(authed_client):
    """ASGI refuses before accept with 4401; browsers expose that rejection as 1006."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with authed_client.websocket_connect("/api/terminals/nope"):
            pass
    assert exc.value.code == 4401


def test_websocket_accepts_with_token(authed_client):
    """With the header the gate opens; the route's own 4xx (unknown
    terminal id) proves we got past auth to real application code."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with authed_client.websocket_connect(
            "/api/terminals/nope", headers={"authorization": f"Bearer {TOKEN}"}
        ) as ws:
            # The route accepts before closing (see _reject in routes/terminal.py),
            # so the app-level close code only arrives on a receive.
            ws.receive_text()
    assert exc.value.code != 4401


def test_auth_session_is_a_probe_that_sets_no_cookie__F1945(authed_client):
    """POST /auth/session answers 204 to the token and 401 without it, and
    never hands the token back as a cookie; there is no session to clear."""
    r = authed_client.post("/auth/session", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 204
    assert "set-cookie" not in r.headers
    assert authed_client.post("/auth/session").status_code == 401
    r = authed_client.delete("/auth/session", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 405


def test_the_token_as_a_cookie_authorizes_nothing__F1945(authed_client):
    authed_client.cookies.set("collimate_token", TOKEN)
    assert authed_client.get("/api/state").status_code == 401
    assert authed_client.post("/auth/session").status_code == 401


def test_session_endpoint_404_when_auth_off(client):
    """No token configured = no session concept to hand out."""
    assert client.post("/auth/session").status_code == 404


def test_non_ascii_token_matches_its_utf8_header():
    """ASGI hands header bytes over latin-1-decoded; the compare must recover
    the client's UTF-8 bytes, not re-encode the mojibake."""
    from auth_middleware import TokenAuth, _presented_token

    auth = TokenAuth()
    auth.configure("täken")
    scope = {"headers": [(b"authorization", "Bearer täken".encode("utf-8"))]}
    assert auth.matches(_presented_token(scope))


BEARER = {"authorization": f"Bearer {TOKEN}"}


def test_tokened_server_refuses_an_opaque_origin_even_with_the_token__F1936(authed_client):
    """The desktop shell presents the token on every request to the server
    origin, whichever frame sends it: a rendered page's sandboxed frame
    (Origin: null) must not ride it to the API."""
    null = {**BEARER, "Origin": "null"}
    assert authed_client.get("/api/state", headers=null).status_code == 403
    assert authed_client.post("/api/accounts/check", headers=null).status_code == 403
    # The plain cell-file path too: only the capability route is open to it.
    assert authed_client.get(
        "/api/channels/00000000-0000-0000-0000-000000000001/cells/nope/files/main.js", headers=null,
    ).status_code == 403
    # The same request from the app itself, or a proxy's public origin, passes.
    assert authed_client.get("/api/state", headers=BEARER).status_code == 200
    assert authed_client.get(
        "/api/state", headers={**BEARER, "Origin": "https://collimate.example.com"},
    ).status_code == 200


def test_tokened_server_refuses_an_opaque_origin_websocket__F1936(authed_client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with authed_client.websocket_connect(
            "/api/terminals/nope", headers={**BEARER, "Origin": "null"},
        ):
            pass
    assert exc.value.code == 4403


def test_tokened_server_serves_an_opaque_origin_the_capability_route__F1936(authed_client):
    """The html renderer's preview frame reads its cell's files through the
    capability route; with a token that still needs the token too."""
    created=authed_client.post('/api/channels',params={'name':'Capability'},headers=BEARER)
    assert created.status_code==200,created.text
    channel=created.json()['id']
    cap = authed_client.get(
        f"/api/channels/{channel}/cells/nope/file_cap", headers=BEARER,
    ).json()["cap"]
    url = f"/api/cellcap/{cap}/channels/{channel}/cells/nope/files/main.js"
    r = authed_client.get(url, headers={**BEARER, "Origin": "null"})
    assert r.status_code == 404  # reached the route: no such cell
    assert r.headers["access-control-allow-origin"] == "*"
    assert authed_client.get(url, headers={"Origin": "null"}).status_code == 401


_fixture_modules = {}


@pytest.mark.parametrize("mode", ["client", "authed", "home", "client-again"])
def test_fixture_order_rebinds_state_database_and_routers__F0380_F1906(mode, request):
    """All three app fixtures refresh the same owned modules and real storage."""
    import importlib
    from contextlib import nullcontext

    from test_pytest_docker_runner import _home_app

    headers = {"Authorization": f"Bearer {TOKEN}"} if mode == "authed" else {}
    context = _home_app() if mode == "home" else nullcontext(
        request.getfixturevalue("authed_client" if mode == "authed" else "client")
    )
    with context as current:
        names = (
            "main", "state", "database", "channel_config", "process_manager",
            "process_manager.manager", "process_manager.backends", "process_manager.finalization",
            "routes", "routes.pick_sessions", "routes.runners", "routes.generation",
        )
        modules = {name: importlib.import_module(name) for name in names}
        state = modules["state"].state
        if not state.channels:
            response = current.post("/api/channels", params={"name": "Starting"}, headers=headers)
            assert response.status_code == 200, response.text
        from models import Cell, Stack

        channel = next(c for c in state.channels if c.id == state.active_channel_id)
        cell = Cell(name=f"fixture-{mode}")
        channel.main_stacks.append(Stack(name="fixture", cells=[cell]))
        url = f"/api/channels/{channel.id}/pick_sessions"
        response = current.get(url, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {}, "a prior fixture's picker DB leaked into this fixture"
        payload = {"query": mode}
        response = current.put(f"{url}/{cell.id}", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert current.get(url, headers=headers).json() == {str(cell.id): payload}
        assert modules["channel_config"].read_ui_state_json(
            str(channel.id), modules["channel_config"].PICK_SESSIONS_KEY, None
        ) == {str(cell.id): payload}
        assert modules["routes.generation"].find_active_channel is modules["state"].find_active_channel
        for name, previous in _fixture_modules.items():
            assert modules[name] is not previous, f"{mode} reused stale owned module {name}"
        _fixture_modules.clear()
        _fixture_modules.update(modules)


def test_fixture_evicts_owned_descendants_and_preserves_unrelated_modules__F1906(request, monkeypatch):
    import importlib
    import json
    import sys
    from types import ModuleType

    owned = ModuleType("process_manager.fixture_probe.child")
    unrelated = ModuleType("process_manager_unrelated.fixture_probe.child")
    monkeypatch.setitem(sys.modules, owned.__name__, owned)
    monkeypatch.setitem(sys.modules, unrelated.__name__, unrelated)
    parent = importlib.import_module("process_manager")
    current = request.getfixturevalue("client")
    assert current.get("/api/state").status_code == 200
    assert owned.__name__ not in sys.modules, "owned package descendants survived the fixture reset"
    assert importlib.import_module("process_manager") is not parent
    assert sys.modules[unrelated.__name__] is unrelated
    assert sys.modules["json"] is json
