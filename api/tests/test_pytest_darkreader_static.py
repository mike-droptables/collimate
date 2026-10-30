"""DarkReader is served from the app's own origin to the opaque document frames.

The SDK's document view (the html renderer's page) darkens an authored page
with DarkReader loaded from ``/darkreader/<version>/darkreader.js``, a
classic ``<script src>`` with no
``crossorigin`` attribute. From a frame sandboxed without
``allow-same-origin`` that is a no-cors GET: no ``Origin`` header,
``Sec-Fetch-Site: cross-site``. The origin guard passes it like any static
asset (the ``Origin: null`` it refuses only arrives on a CORS request), so the
page darkens with no CDN and no network beyond the app.
"""
from __future__ import annotations

import pytest
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

SCRIPT = "/**\n * Dark Reader v4.9.130\n */\n"
PATH = "/darkreader/4.9.130/darkreader.js"


@pytest.fixture
def static_client(client, api_app, tmp_path):
    """The app with its static mount serving a built tree that holds the
    vendored script (api/static is a build output, absent in a bare checkout)."""
    folder = tmp_path / "darkreader" / "4.9.130"
    folder.mkdir(parents=True)
    (folder / "darkreader.js").write_text(SCRIPT)
    api_app.router.routes[:] = [r for r in api_app.router.routes if getattr(r, "name", None) != "static"]
    api_app.router.routes.append(Mount("/", StaticFiles(directory=str(tmp_path), html=True), name="static"))
    return client


def test_an_opaque_frame_loads_the_vendored_darkreader_as_a_classic_script(static_client):
    r = static_client.get(PATH, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "script"})
    assert r.status_code == 200
    assert r.text == SCRIPT
    assert "javascript" in r.headers["content-type"]


def test_the_vendored_darkreader_is_not_opened_to_cors_reads(static_client):
    # A CORS read from the opaque origin is still refused: the view loads
    # the script classically, and nothing else needs to read it.
    assert static_client.get(PATH, headers={"Origin": "null"}).status_code == 403
