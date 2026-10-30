"""Coverage for runtime UI configuration.

The bundled UI gets ``window.__COLLIMATE_CONFIG__`` injected inline by
the index.html handler in main.py — that's the primary delivery path.
The /api/config.js endpoint stays around as an introspection tool that
emits the same payload as a tiny JS file. ``getApiUrl()`` in the
frontend reads the window global first and falls back to build-time
defaults if absent.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# /api/config.js — introspection endpoint
# ---------------------------------------------------------------------------

def test_config_js_returns_javascript_content_type(client):
    r = client.get("/api/config.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")


def test_config_js_sets_window_global(client):
    r = client.get("/api/config.js")
    assert r.status_code == 200
    body = r.text
    assert "window.__COLLIMATE_CONFIG__" in body
    # Sanity: it's an assignment statement, not a function definition.
    assert "window.__COLLIMATE_CONFIG__ =" in body


def test_config_js_default_api_base_is_relative(client):
    """Without an env override the endpoint should mirror the historic
    path-relative behaviour (``../api``) so an existing static-served
    deployment still works without setting anything."""
    r = client.get("/api/config.js")
    assert '"apiBase": "../api"' in r.text


def test_config_js_honors_env_override(client, monkeypatch):
    monkeypatch.setenv("COLLIMATE_PUBLIC_API_BASE", "https://api.example.com")
    r = client.get("/api/config.js")
    assert '"apiBase": "https://api.example.com"' in r.text


def test_config_js_response_is_short_cached(client):
    """Cache-Control header is present and bounded so a config change
    propagates within seconds of a hard refresh, but the same bundle
    in the same session reuses one response."""
    r = client.get("/api/config.js")
    cc = r.headers.get("cache-control", "")
    assert "max-age=" in cc


# ---------------------------------------------------------------------------
# index.html injection — the actual delivery path
# ---------------------------------------------------------------------------

def test_render_index_returns_none_when_static_missing(tmp_path, monkeypatch):
    """No api/static/index.html on disk (fresh dev checkout) — the
    helper returns None so the StaticFiles mount can take over the 404
    branch instead of raising."""
    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(tmp_path / "missing.html"))
    assert main._render_index_with_runtime_config() is None


def test_render_index_injects_window_global_before_head_close(tmp_path, monkeypatch):
    """When index.html exists, the helper should inject the runtime
    config inline immediately before ``</head>`` so the global lands
    before the bundled main.tsx module evaluates."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><title>X</title></head><body>hi</body></html>",
        encoding="utf-8",
    )

    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(static_dir / "index.html"))

    rendered = main._render_index_with_runtime_config()
    assert rendered is not None
    assert "window.__COLLIMATE_CONFIG__" in rendered
    # Snippet sits before </head>, not after.
    head_close = rendered.find("</head>")
    snippet_pos = rendered.find("window.__COLLIMATE_CONFIG__")
    assert 0 < snippet_pos < head_close


def test_render_index_falls_back_to_body_when_no_head(tmp_path, monkeypatch):
    """Defensive fallback for malformed HTML — inject before <body>
    if no </head> is present. (Real Vite output always has one;
    this just keeps the helper from silently dropping the snippet
    on a custom shell.)"""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><body>no head tag here</body>",
        encoding="utf-8",
    )

    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(static_dir / "index.html"))

    rendered = main._render_index_with_runtime_config()
    assert rendered is not None
    assert "window.__COLLIMATE_CONFIG__" in rendered
    body_pos = rendered.find("<body")
    snippet_pos = rendered.find("window.__COLLIMATE_CONFIG__")
    assert 0 < snippet_pos < body_pos


def test_render_index_injects_no_manifest_link(tmp_path, monkeypatch):
    """The PWA machinery is gone — the installable offering is the
    desktop app, so serve-time injection carries the runtime config and
    nothing else. A reappearing manifest link would re-advertise Chrome's
    install icon on the lite tier."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><title>X</title></head><body></body></html>",
        encoding="utf-8",
    )
    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(static_dir / "index.html"))

    rendered = main._render_index_with_runtime_config()
    assert rendered is not None
    assert "window.__COLLIMATE_CONFIG__" in rendered
    assert 'rel="manifest"' not in rendered


def test_render_index_uses_env_override(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head></head><body></body></html>",
        encoding="utf-8",
    )
    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(static_dir / "index.html"))
    monkeypatch.setenv("COLLIMATE_PUBLIC_API_BASE", "https://api.example.com")

    rendered = main._render_index_with_runtime_config()
    assert '"apiBase": "https://api.example.com"' in rendered


_MAP_HTML = ('<!doctype html><html><head><script type="importmap" id="collimate-mirror-map">'
             '{"imports":{"https://esm.sh/":"../api/mirror/esm.sh/",'
             '"https://cdn.jsdelivr.net/":"../api/mirror/cdn.jsdelivr.net/"}}</script>'
             '<script type="module" src="/assets/index.js"></script></head><body></body></html>')


def _mirror_map(rendered: str) -> dict:
    import json
    import re
    match = re.search(r'<script type="importmap" id="collimate-mirror-map">([^<]*)</script>', rendered)
    assert match is not None
    return json.loads(match.group(1))


def test_render_index_keeps_the_default_mirror_import_map(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(_MAP_HTML, encoding="utf-8")
    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(tmp_path / "index.html"))
    rendered = main._render_index_with_runtime_config()
    assert rendered.startswith(_MAP_HTML.split("</script>")[0])
    assert rendered.index("collimate-mirror-map") < rendered.index('type="module"')


def test_render_index_points_the_mirror_import_map_at_the_public_api_base(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text(_MAP_HTML, encoding="utf-8")
    import main
    monkeypatch.setattr(main, "_INDEX_PATH", str(tmp_path / "index.html"))
    monkeypatch.setenv("COLLIMATE_PUBLIC_API_BASE", "https://api.example.com/api/")
    assert _mirror_map(main._render_index_with_runtime_config()) == {"imports": {
        "https://esm.sh/": "https://api.example.com/api/mirror/esm.sh/",
        "https://cdn.jsdelivr.net/": "https://api.example.com/api/mirror/cdn.jsdelivr.net/",
    }}
    monkeypatch.setenv("COLLIMATE_PUBLIC_API_BASE", "https://api.example.com/</script><b>")
    rendered = main._render_index_with_runtime_config()
    assert _mirror_map(rendered)["imports"]["https://esm.sh/"] == "https://api.example.com/</script><b>/mirror/esm.sh/"
    assert rendered.index("collimate-mirror-map") < rendered.index('type="module"')
