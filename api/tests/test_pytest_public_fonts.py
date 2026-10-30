"""The bundled fonts are the one static path an opaque origin may read.

Renderer frames, the html preview and the status frames run without
``allow-same-origin``; their ``@font-face`` loads arrive in CORS mode with
``Origin: null``. ``/fonts/`` answers them with
``Access-Control-Allow-Origin: *``; everything else keeps the origin guard.
"""
from __future__ import annotations

import pytest
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

CSS = "@font-face { font-family: 'Hanken Grotesk'; }\n"


@pytest.fixture
def fonts_client(client, api_app, tmp_path):
    """The app with its static mount serving a built tree that holds
    fonts/fonts.css (api/static is a build output, absent in a bare checkout)."""
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "fonts.css").write_text(CSS)
    (tmp_path / "app.js").write_text("app")
    api_app.router.routes[:] = [r for r in api_app.router.routes if getattr(r, "name", None) != "static"]
    api_app.router.routes.append(Mount("/", StaticFiles(directory=str(tmp_path), html=True), name="static"))
    return client


@pytest.mark.parametrize("origin", [None, "null", "http://testserver", "https://evil.example"])
def test_fonts_css_carries_the_open_cors_header_S03(fonts_client, origin):
    headers = {"Origin": origin} if origin else {}
    r = fonts_client.get("/fonts/fonts.css", headers=headers)
    assert r.status_code == 200
    assert r.text == CSS
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_the_rest_of_the_static_tree_keeps_the_origin_guard_S03(fonts_client):
    assert fonts_client.get("/app.js", headers={"Origin": "null"}).status_code == 403
    assert fonts_client.post("/fonts/fonts.css", headers={"Origin": "null"}).status_code == 403
    same = fonts_client.get("/app.js")
    assert same.status_code == 200
    assert "access-control-allow-origin" not in same.headers


def test_a_dot_segment_never_leaves_the_fonts_directory_S03(api_app):
    # HTTP clients normalise dot segments before sending; a raw request
    # need not, so the middleware refuses one itself.
    import main
    scope = {"type": "http", "method": "GET", "path": "/fonts/../app.js", "headers": []}
    assert not main.PublicFontsMiddleware._font_read(scope)
    assert main.PublicFontsMiddleware._font_read({**scope, "path": "/fonts/inter/a.woff2"})


def test_fonts_refuse_a_rebinding_host_S03(fonts_client):
    r = fonts_client.get("/fonts/fonts.css", headers={"Host": "evil.example", "Origin": "null"})
    assert r.status_code == 403


def test_fonts_stay_behind_the_token_S03(fonts_client):
    import auth_middleware
    auth_middleware.auth.configure("s3cret")
    try:
        assert fonts_client.get("/fonts/fonts.css", headers={"Origin": "null"}).status_code == 401
        r = fonts_client.get("/fonts/fonts.css", headers={
            "Origin": "null", "Authorization": "Bearer s3cret"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"
    finally:
        auth_middleware.auth.configure(None)
