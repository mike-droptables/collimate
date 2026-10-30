"""A tokenless loopback server refuses cross-site use.

With no token, anything that reaches the port can list and read local files
and start runs, and a web page in the user's browser can reach it. CORS must
not hand such a page the responses, the server must refuse a foreign Origin
outright (CORS never covers WebSockets), and a non-loopback Host (DNS
rebinding) gets nothing. The Vite dev server's origins are trusted only
under COLLIMATE_DEV=1, and an opaque origin reads cell files only through
the capability route.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect
from test_pytest_handlers import client as client

EVIL = "https://evil.example"


def test_foreign_origin_gets_no_cors_grant_and_no_answer(client):
    pre = client.options("/api/accounts/check", headers={
        "Origin": EVIL, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.headers.get("access-control-allow-origin") != EVIL
    assert pre.status_code >= 400
    for method, path in (("get", "/api/state"), ("post", "/api/accounts/check")):
        r = getattr(client, method)(path, headers={"Origin": EVIL})
        assert r.status_code == 403
        assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("origin", ["null", "http://127.0.0.1:9999", "http://localhost:8888"])
def test_other_local_origins_are_refused(client, origin):
    # A page on another loopback port (a dev server, an app an agent built)
    # is still another site.
    assert client.get("/api/state", headers={"Origin": origin}).status_code == 403


def test_own_origin_still_works__F1940(client):
    assert client.get("/api/state", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("destination", ["image", "script", "empty"])
def test_real_file_bytes_are_private_from_cross_site_loads_P4F07(client, destination):
    cid = _seed_cell({"payload.js": "private bytes"})
    path = f"/api/channels/{_channel_id()}/cells/{cid}/files/payload.js"
    assert client.get(path, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                                     "Sec-Fetch-Dest": destination}).status_code == 403
    same_site = client.get(path, headers={"Sec-Fetch-Site": "same-site"})
    assert same_site.status_code == 200
    assert same_site.text == "private bytes"
    cap = client.get(f"/api/channels/{_channel_id()}/cells/{cid}/file_cap").json()["cap"]
    shared = client.get(f"/api/cellcap/{cap}/channels/{_channel_id()}/cells/{cid}/files/payload.js",
                        headers={"Sec-Fetch-Site": "cross-site", "Origin": "null"})
    assert shared.status_code == 200
    assert shared.text == "private bytes"


@pytest.fixture
def plain_client(monkeypatch, request):
    """The ``client`` fixture's app, imported with neither COLLIMATE_DEV nor
    COLLIMATE_CORS_ORIGINS set, whatever the shell running the tests
    exports (CORS takes the origin list at import)."""
    monkeypatch.delenv("COLLIMATE_DEV", raising=False)
    monkeypatch.delenv("COLLIMATE_CORS_ORIGINS", raising=False)
    app = request.getfixturevalue("api_app")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("dev", ["http://localhost:5173", "http://127.0.0.1:5174"])
def test_dev_origins_are_foreign_without_the_dev_flag__F1940(plain_client, dev):
    """Any Vite project the user runs sits on these ports; a server not
    started for frontend development must treat it as another site."""
    r = plain_client.get("/api/state", headers={"Origin": dev})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


@pytest.fixture
def dev_client(monkeypatch, request):
    """The ``client`` fixture's app, imported with COLLIMATE_DEV=1 (CORS
    takes the origin list at import)."""
    monkeypatch.setenv("COLLIMATE_DEV", "1")
    app = request.getfixturevalue("api_app")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def test_dev_origins_are_trusted_under_the_dev_flag__F1940(dev_client):
    dev = dev_client.get("/api/state", headers={"Origin": "http://localhost:5173"})
    assert dev.status_code == 200
    assert dev.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert dev_client.get("/api/state", headers={"Origin": EVIL}).status_code == 403


def _channel_id() -> str:
    from state import find_active_channel
    return str(find_active_channel().id)


def _seed_cell(files: dict[str, str]) -> str:
    """Accepted live files in a registered channel, without a history fallback."""
    from database import get_db_connection
    from models import VirtualFile, language_for
    from persistence import initialize_files
    conn = get_db_connection(_channel_id())
    try:
        cid = str(uuid.uuid4())
        initialize_files(conn, cid, [VirtualFile(name=path, content=body, language=language_for(path))
                                     for path, body in files.items()])
        conn.commit()
        return cid
    finally:
        conn.close()


def _cap(client, cell_id: str, channel: str | None = None) -> str:
    identity = channel if channel is not None else _channel_id()
    r = client.get(f"/api/channels/{identity}/cells/{cell_id}/file_cap")
    assert r.status_code == 200, r.text
    return r.json()["cap"]


def test_null_origin_cannot_read_a_cell_file_by_its_plain_url__F1941(client):
    """Any page can make a null origin (a sandboxed frame), and seeded cell
    ids are derived from public names: the plain cell-file URL must not
    answer it."""
    cid = _seed_cell({"runner.yaml": "secret: 1\n"})
    null = {"Origin": "null"}
    assert client.get(f"/api/channels/{_channel_id()}/cells/{cid}/files/runner.yaml", headers=null).status_code == 403
    assert client.head(f"/api/channels/{_channel_id()}/cells/{cid}/files/runner.yaml", headers=null).status_code == 403
    # The app itself still reads it.
    r = client.get(f"/api/channels/{_channel_id()}/cells/{cid}/files/runner.yaml")
    assert r.status_code == 200 and r.text == "secret: 1\n"


def test_null_origin_reads_cell_files_through_the_capability_route__F1941(client):
    """The html renderer's opaque preview frame: module scripts, fonts and
    fetch() carry Origin: null (and Sec-Fetch-Site: cross-site, like any other
    page's sandboxed frame), so the read is gated on the cap in the path.
    Readable (``*``, no credentials), and relative URLs under it inherit it."""
    cid = _seed_cell({"index.html": "<p>hi</p>", "js/util.js": "export const x = 1;\n"})
    other = _seed_cell({"runner.yaml": "secret: 1\n"})
    cap = _cap(client, cid)
    for name in ("Starting", "starting"):
        assert client.get(f"/api/channels/{name}/cells/{cid}/file_cap").status_code == 404
    null = {"Origin": "null"}
    base = f"/api/cellcap/{cap}/channels/{_channel_id()}/cells/{cid}/files/"
    r = client.get(base + "js/util.js", headers=null)
    assert r.status_code == 200, r.text
    assert r.text == "export const x = 1;\n"
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers
    assert client.head(base + "index.html", headers=null).status_code != 403
    # The cap is good for its own cell only, for reads only, and must be exact.
    assert client.get(f"/api/cellcap/{cap}/channels/{_channel_id()}/cells/{other}/files/runner.yaml", headers=null).status_code == 403
    assert client.post(base + "js/util.js", headers=null).status_code == 403
    assert client.get(base.replace(cap, cap[:-1] + ("A" if cap[-1] != "A" else "B")) + "js/util.js", headers=null).status_code == 403
    assert client.get(base.replace(cap, _cap(client, other)) + "js/util.js", headers=null).status_code == 403
    # Nothing else answers a null origin, and a rebinding Host gets nothing.
    assert client.get("/api/accounts", headers=null).status_code == 403
    assert client.get(f"/api/channels/{_channel_id()}/cells/{cid}/file_cap", headers=null).status_code == 403
    rebind = {**null, "Host": "rebind.evil.example:5001"}
    assert client.get(base + "js/util.js", headers=rebind).status_code == 403


def test_capability_is_stable_across_restarts_and_private_to_the_data_dir__F1941(client, tmp_path):
    from auth_middleware import CellFileCaps
    cid = str(uuid.uuid4())
    cap = _cap(client, cid)
    # A fresh process reads the same key back: a page's cached URLs stay good.
    assert CellFileCaps().cap(_channel_id(), cid) == cap
    key = Path(".collimate") / "cell_file_caps.key"
    assert key.stat().st_mode & 0o077 == 0
    # Another data dir has a key of its own: the same channel and cell get
    # another cap there, and this data dir's cap still matches here.
    other = tmp_path / "other-data-dir"
    other.mkdir()
    home, home_key = os.getcwd(), key.read_bytes()
    os.chdir(other)
    try:
        elsewhere = CellFileCaps().cap(_channel_id(), cid)
        assert (other / ".collimate" / "cell_file_caps.key").read_bytes() != home_key
    finally:
        os.chdir(home)
    assert elsewhere != cap
    assert CellFileCaps().cap(_channel_id(), cid) == cap
    # A key cut short (a crash mid-write) is replaced, never used.
    key.write_bytes(b"")
    fresh = CellFileCaps()
    assert fresh.cap(_channel_id(), cid) != cap
    assert len(key.read_bytes()) == 32


def test_the_key_write_follows_no_link_planted_beside_it__F1941(fresh_cwd):
    """A fresh key is written to a new temporary file (O_EXCL | O_NOFOLLOW)
    and renamed into place: a link at the old fixed temporary name is
    neither followed nor truncated, and no temporary file is left behind."""
    from auth_middleware import CellFileCaps
    os.makedirs(".collimate")
    target = fresh_cwd / "not-the-key.txt"
    target.write_text("keep me\n")
    planted = f"cell_file_caps.key.{os.getpid()}.tmp"
    os.symlink(target, os.path.join(".collimate", planted))
    CellFileCaps()._key()
    assert target.read_text() == "keep me\n"
    key = fresh_cwd / ".collimate" / "cell_file_caps.key"
    assert not key.is_symlink() and len(key.read_bytes()) == 32
    assert sorted(os.listdir(".collimate")) == sorted(["cell_file_caps.key", planted])


def test_dns_rebinding_host_is_refused(client):
    assert client.get("/health", headers={"Host": "rebind.evil.example:5001"}).status_code == 403
    assert client.get("/health", headers={"Host": "127.0.0.1:5001"}).status_code == 200
    assert client.get("/health", headers={"Host": "[::1]:5001"}).status_code == 200


def test_foreign_origin_websocket_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/api/terminals/nope?token=x", headers={"Origin": EVIL}):
            pass
    assert refused.value.code == 4403


def test_origin_check_matches_port_not_just_loopback_name():
    from auth_middleware import OriginGuardMiddleware
    allowed = OriginGuardMiddleware._allowed
    assert allowed("127.0.0.1:5001", "http://localhost:5001")
    assert not allowed("127.0.0.1:5001", "http://127.0.0.1:5002")
    assert not allowed("127.0.0.1:5001", "http://127.0.0.1.evil.example:5001")
