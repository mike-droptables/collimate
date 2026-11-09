"""Coverage for the server-wide settings document (app_settings.py: the theme
library and the default theme, in app.db) and its routes
(routes/app_settings.py: the document, the theme pictures, the broadcast).

All fixtures are synthetic: made-up theme ids, colours and picture bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest


# ---------------------------------------------------------------------------
# The document: defaults, load, merge
# ---------------------------------------------------------------------------

def test_defaults_shape(fresh_cwd):
    from collimate_server.app_settings import default_app_settings, load_app_settings
    assert default_app_settings() == {
        "version": 1, "kind": "app-settings", "theme": {"defaultThemeId": None, "userThemes": []}}
    assert load_app_settings() == default_app_settings()


def test_merge_is_section_wholesale_and_persists(fresh_cwd):
    """A merge replaces the theme section whole (an omitted field defaults)
    and the next load reads it back from app.db."""
    from collimate_server.app_settings import load_app_settings, merge_app_settings
    theme = {"defaultThemeId": "user.synth", "userThemes": [
        {"id": "user.synth", "label": "Synth", "baseThemeId": "night", "vars": {"ink": "#eee"}}]}
    merged = merge_app_settings({"theme": theme})
    assert merged["theme"]["defaultThemeId"] == "user.synth"
    assert load_app_settings() == merged
    merge_app_settings({"theme": {"defaultThemeId": "paper"}})
    assert load_app_settings()["theme"] == {"defaultThemeId": "paper", "userThemes": []}
    # A body naming no section changes nothing.
    assert merge_app_settings({}) == load_app_settings()


def test_merge_rejects_unknown_sections_non_dict_and_wrong_kind(fresh_cwd):
    """A bad body changes nothing: a malformed section is refused rather
    than normalized, so a write never empties the library."""
    from collimate_server.app_settings import load_app_settings, merge_app_settings
    merge_app_settings({"theme": {"defaultThemeId": "paper"}})
    for bad in (
        {"bogus": {}}, ["not", "a", "dict"], {"kind": "channel-settings", "theme": {}},
        {"theme": None}, {"theme": "paper"}, {"theme": []},
    ):
        with pytest.raises(ValueError):
            merge_app_settings(bad)
    assert load_app_settings()["theme"] == {"defaultThemeId": "paper", "userThemes": []}


def test_default_theme_id_is_stripped_or_none(fresh_cwd):
    from collimate_server.app_settings import validate_theme_library
    assert validate_theme_library({"defaultThemeId": "  paper "})["defaultThemeId"] == "paper"
    for bad in ("", "   ", None, 3, ["paper"], {"id": "paper"}):
        assert validate_theme_library({"defaultThemeId": bad})["defaultThemeId"] is None
    for bad_section in (None, "paper", [], 3):
        assert validate_theme_library(bad_section) == {"defaultThemeId": None, "userThemes": []}


def test_an_unreadable_or_foreign_row_reads_as_the_defaults(fresh_cwd):
    """A row that is not JSON, not an object or of another kind reads as the
    defaults; an older field (``activeThemeId``) is dropped."""
    from collimate_server.app_settings import APP_SETTINGS_KEY, default_app_settings, load_app_settings
    from collimate_server.database import get_global_db_connection, write_global_json
    for row in ([1, 2], {"kind": "channel-settings", "theme": {"defaultThemeId": "paper"}}):
        write_global_json(APP_SETTINGS_KEY, row)
        assert load_app_settings() == default_app_settings()
    write_global_json(APP_SETTINGS_KEY, {"theme": {"activeThemeId": "paper"}})
    assert load_app_settings() == default_app_settings()
    conn = get_global_db_connection()
    try:
        conn.execute("UPDATE ui_state SET value = ? WHERE key = ?", ("{not json", APP_SETTINGS_KEY))
        conn.commit()
    finally:
        conn.close()
    assert load_app_settings() == default_app_settings()


# ---------------------------------------------------------------------------
# The theme library's user themes
# ---------------------------------------------------------------------------

def test_validate_theme_keeps_userthemes_verbatim(fresh_cwd):
    from collimate_server.app_settings import validate_theme_library
    theme = {
        "defaultThemeId": "user.abc123",
        "userThemes": [
            {"id": "user.abc123", "label": "Mine", "scheme": "dark",
             "vars": {"canvas": "#112233"}, "baseThemeId": "night"},
            {"id": "builtin-imposter", "vars": {}},  # not user.* → dropped
            "garbage",                               # not a dict → dropped
        ],
    }
    out = validate_theme_library(theme)
    assert out["defaultThemeId"] == "user.abc123"
    assert len(out["userThemes"]) == 1
    assert out["userThemes"][0]["vars"] == {"canvas": "#112233"}


def test_validate_theme_requires_string_to_string_skin_vars_S02(fresh_cwd):
    """A user skin's ``vars`` must map alphanumeric string keys to string
    values; any other shape drops the skin."""
    from collimate_server.app_settings import validate_theme_library
    theme = {"userThemes": [
        {"id": "user.ok", "vars": {"sheet2": "#222", "brassSoft": "rgba(0,0,0,.1)"}},
        {"id": "user.empty", "vars": {}},
        {"id": "user.numeric", "vars": {"radius": 8}},
        {"id": "user.kebab", "vars": {"sheet-2": "#222"}},
        {"id": "user.cssname", "vars": {"--sheet": "#222"}},
        {"id": "user.list", "vars": ["#222"]},
        {"id": "user.missing"},
    ]}
    out = validate_theme_library(theme)
    assert [t["id"] for t in out["userThemes"]] == ["user.ok", "user.empty"]
    assert out["userThemes"][0]["vars"] == {"sheet2": "#222", "brassSoft": "rgba(0,0,0,.1)"}


def test_validate_theme_keeps_only_the_skin_record_keys_S02(fresh_cwd):
    """A kept skin's ``vars`` lose every key outside the skin record, so a
    stale or unknown key never reaches the settings row."""
    from collimate_server.app_settings import validate_theme_library
    theme = {"userThemes": [
        {"id": "user.old", "label": "Old", "baseThemeId": "night",
         "vars": {"ink": "#eee", "amberGlow": "#f90", "backdropBase": "#000",
                  "backdropImage": "data:image/png;base64,AAAA", "someFutureToken": "1px"}},
    ]}
    out = validate_theme_library(theme)
    assert out["userThemes"] == [
        {"id": "user.old", "label": "Old", "baseThemeId": "night", "vars": {"ink": "#eee"}, "scene": {}},
    ]


def test_validate_theme_keeps_a_valid_scene_override(fresh_cwd):
    """A user theme's ``scene`` keeps every field it validly sets, ``None``
    for the base (the canvas) and for a glow (switched off) included."""
    from collimate_server.app_settings import validate_theme_library
    scene = {
        "base": "#203040", "pattern": "scanlines", "ink": "#A0B0C0", "pitch": 24, "strength": 0.3,
        "glowWarm": {"color": "#ffeedd", "alpha": 0.2}, "glowCool": None, "vignette": 0.4, "edge": 2,
        "behindTopBar": True, "behindRemoved": False,
    }
    theme = {"userThemes": [
        {"id": "user.a", "vars": {}, "scene": scene},
        {"id": "user.b", "vars": {}, "scene": {"base": None}},
    ]}
    out = validate_theme_library(theme)
    assert out["userThemes"][0]["scene"] == scene
    assert out["userThemes"][1]["scene"] == {"base": None}


def test_validate_theme_accepts_every_scene_pattern(fresh_cwd):
    """Every pattern on the menu, ``fine-dots`` included, is kept."""
    from collimate_server.app_settings import SCENE_PATTERNS, validate_theme_library
    assert "fine-dots" in SCENE_PATTERNS
    theme = {"userThemes": [
        {"id": f"user.{i}", "vars": {}, "scene": {"pattern": pattern}}
        for i, pattern in enumerate(SCENE_PATTERNS)
    ]}
    out = validate_theme_library(theme)
    assert [t["scene"] for t in out["userThemes"]] == [{"pattern": p} for p in SCENE_PATTERNS]


def test_validate_theme_drops_an_invalid_scene_field_and_clamps_strengths(fresh_cwd):
    """Unknown keys, colours other than ``#rrggbb``, values off the pattern,
    pitch and edge menus, booleans and malformed glows are dropped;
    strengths are clamped to 0..1; a scene that is not an object is empty.
    The skin itself is kept."""
    from collimate_server.app_settings import validate_theme_library
    theme = {"userThemes": [
        {"id": "user.a", "vars": {"ink": "#eee"}, "scene": {
            "base": "red", "pattern": "stripes", "ink": "#abc", "pitch": 18, "edge": 3, "image": "data:x",
            "strength": 4, "vignette": -1, "glowWarm": {"color": "#ffffff"}, "glowCool": {"color": "blue", "alpha": 0.5},
        }},
        {"id": "user.b", "vars": {}, "scene": {
            "strength": float("nan"), "vignette": "0.5", "pitch": "16", "edge": True, "glowWarm": {"color": "#102030", "alpha": 7},
        }},
        {"id": "user.c", "vars": {}, "scene": ["grid"]},
        # An integer too large for a float (json.loads keeps it exact) is
        # dropped, never an OverflowError that would surface as a 500.
        {"id": "user.d", "vars": {}, "scene": {
            "strength": 10**400, "vignette": json.loads("1" + "0" * 400),
            "glowWarm": {"color": "#000000", "alpha": 10**400}, "pattern": "grid",
        }},
    ]}
    out = validate_theme_library(theme)
    assert [t["scene"] for t in out["userThemes"]] == [
        {"strength": 1.0, "vignette": 0.0},
        {"glowWarm": {"color": "#102030", "alpha": 1.0}},
        {},
        {"pattern": "grid"},
    ]
    assert out["userThemes"][0]["vars"] == {"ink": "#eee"}


def test_validate_theme_keeps_a_scene_picture_that_is_a_cas_hash(fresh_cwd):
    """``image`` is kept as a CAS hash (an upload) or ``None`` (no picture,
    overriding a base); any other value, a data URL or a path among them, is
    dropped, as ``normalizeScene`` drops it."""
    from collimate_server.app_settings import validate_theme_library
    digest = "c" * 64
    theme = {"userThemes": [
        {"id": "user.a", "vars": {}, "scene": {"image": digest}},
        {"id": "user.b", "vars": {}, "scene": {"image": None}},
        {"id": "user.c", "vars": {}, "scene": {"image": "data:image/png;base64,AAAA"}},
        {"id": "user.d", "vars": {}, "scene": {"image": digest.upper()}},
        {"id": "user.e", "vars": {}, "scene": {"image": "/api/blob/" + digest}},
    ]}
    out = validate_theme_library(theme)
    assert [t["scene"] for t in out["userThemes"]] == [{"image": digest}, {"image": None}, {}, {}, {}]


def test_validate_theme_keeps_only_boolean_reach_options(fresh_cwd):
    """``behindTopBar`` and ``behindRemoved`` are kept as booleans; any other
    value (a string, a number, ``None``) is dropped, as ``normalizeScene``
    drops it."""
    from collimate_server.app_settings import validate_theme_library
    theme = {"userThemes": [
        {"id": "user.a", "vars": {}, "scene": {"behindTopBar": False, "behindRemoved": True}},
        {"id": "user.b", "vars": {}, "scene": {"behindTopBar": "true", "behindRemoved": 1}},
        {"id": "user.c", "vars": {}, "scene": {"behindTopBar": None, "behindRemoved": False}},
    ]}
    out = validate_theme_library(theme)
    assert [t["scene"] for t in out["userThemes"]] == [
        {"behindTopBar": False, "behindRemoved": True},
        {},
        {"behindRemoved": False},
    ]


def test_merging_admits_a_scene_picture_in_the_app_store_and_drops_one_it_lacks(fresh_cwd):
    """A merge retains each picture's body in the installation's store (its
    publication age restarts, so the collector leaves it) and drops a
    reference whose bytes are gone, so the row never names a picture a
    reader cannot get."""
    from collimate_server.app_settings import load_app_settings, merge_app_settings, scene_image_hashes
    from collimate_server.cas import app_store
    store = app_store()
    present = store.hash_and_store_bytes(b"\x89PNG synthetic picture")
    gone = "d" * 64
    old = time.time() - 7 * 24 * 3600
    os.utime(store.blob_path(present), (old, old))
    themes = [
        {"id": "user.a", "vars": {}, "scene": {"image": present, "pattern": "grid"}},
        {"id": "user.b", "vars": {}, "scene": {"image": gone, "pattern": "dots"}},
    ]
    out = merge_app_settings({"theme": {"userThemes": themes}})
    assert [t["scene"] for t in out["theme"]["userThemes"]] == [{"image": present, "pattern": "grid"}, {"pattern": "dots"}]
    assert os.path.getmtime(store.blob_path(present)) > old + 3600
    assert scene_image_hashes(load_app_settings()) == [present]


def test_scene_menus_match_the_frontend_scene_record():
    """``SCENE_PATTERNS``, ``SCENE_PITCHES`` and ``SELECTION_EDGES`` are the
    menus of the same names in frontend/src/lib/themes.ts."""
    import re
    from pathlib import Path
    from collimate_server.app_settings import SCENE_PATTERNS, SCENE_PITCHES, SELECTION_EDGES
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "themes.ts").read_text()

    def menu(name):
        block = re.search(rf"export const {name} = \[(.*?)\] as const;", src, re.S)
        assert block, f"themes.ts has no {name}"
        return tuple(re.findall(r"'([^']+)'|(\d+)", block.group(1)))

    assert tuple(a for a, _ in menu("SCENE_PATTERNS")) == SCENE_PATTERNS
    assert tuple(int(b) for _, b in menu("SCENE_PITCHES")) == SCENE_PITCHES
    assert tuple(int(b) for _, b in menu("SELECTION_EDGES")) == SELECTION_EDGES


def test_skin_var_keys_match_the_frontend_skin_record_S02():
    """``SKIN_VAR_KEYS`` is exactly the key set of ``THEME_CSS_VARS`` in
    frontend/src/lib/themes.ts."""
    import re
    from pathlib import Path
    from collimate_server.app_settings import SKIN_VAR_KEYS
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "themes.ts").read_text()
    block = re.search(r"export const THEME_CSS_VARS = \{(.*?)\n\}", src, re.S)
    assert block, "themes.ts has no THEME_CSS_VARS record"
    keys = set(re.findall(r"^\s*(\w+):\s*'--[\w-]+',?\s*$", block.group(1), re.M))
    assert keys and keys == SKIN_VAR_KEYS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_get_and_put_round_trip_and_broadcast(client, monkeypatch):
    """GET answers the defaults; a PUT replaces the theme section, answers
    the stored document and tells every channel stream
    ``app_settings_changed``. An unknown section, another kind or a section
    that is not an object is a 400 (the merge's refusal), a body that is not
    an object a 422 (FastAPI's body check); neither broadcasts anything."""
    import collimate_server.events as events
    heard: list = []
    monkeypatch.setattr(events, "broadcast_all", heard.append)
    assert client.get("/api/app-settings").json() == {
        "version": 1, "kind": "app-settings", "theme": {"defaultThemeId": None, "userThemes": []}}
    theme = {"defaultThemeId": "user.synth", "userThemes": [
        {"id": "user.synth", "label": "Synth", "vars": {"ink": "#eee"}, "scene": {"pattern": "grid"}}]}
    put = client.put("/api/app-settings", json={"theme": theme})
    assert put.status_code == 200, put.text
    assert put.json()["theme"] == theme
    assert client.get("/api/app-settings").json() == put.json()
    assert heard == [{"type": "app_settings_changed"}]
    unknown = client.put("/api/app-settings", json={"editing": {}})
    assert unknown.status_code == 400 and "editing" in unknown.json()["detail"]
    assert client.put("/api/app-settings", json={"kind": "channel-settings"}).status_code == 400
    assert client.put("/api/app-settings", json={"theme": None}).status_code == 400
    assert client.put("/api/app-settings", json=["theme"]).status_code == 422
    assert heard == [{"type": "app_settings_changed"}]
    assert client.get("/api/app-settings").json()["theme"] == theme


def test_the_library_is_shared_by_every_channel(client):
    """The library is the server's: a second channel reads the same document,
    and a channel's own settings carry only its override."""
    other = client.post("/api/channels", params={"name": "synth-other"}).json()["id"]
    put = client.put("/api/app-settings", json={"theme": {"defaultThemeId": "paper", "userThemes": []}})
    assert put.status_code == 200, put.text
    for channel in (client.channel_id, other):
        assert client.get(f"/api/channels/{channel}/settings").json()["theme"] == {"override": None}
    assert client.get("/api/app-settings").json()["theme"]["defaultThemeId"] == "paper"


def test_image_upload_stores_in_the_app_store_and_get_serves_it(client):
    from collimate_server.cas import app_store
    body = b"\x89PNG synthetic picture bytes \x00\x01"
    up = client.post("/api/app-settings/images", content=body)
    assert up.status_code == 200, up.text
    digest = hashlib.sha256(body).hexdigest()
    assert up.json() == {"blob_hash": digest, "size": len(body), "encoding": "binary"}
    assert app_store().has(digest)
    got = client.get(f"/api/app-settings/images/{digest}")
    assert got.status_code == 200
    assert got.content == body
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in got.headers["cache-control"]


def test_image_get_refuses_a_bad_hash_an_unknown_one_and_a_service_worker(client):
    assert client.get("/api/app-settings/images/not-a-hash").status_code == 400
    assert client.get(f"/api/app-settings/images/{'e' * 64}").status_code == 404
    up = client.post("/api/app-settings/images", content=b"synthetic")
    digest = up.json()["blob_hash"]
    refused = client.get(f"/api/app-settings/images/{digest}", headers={"Service-Worker": "script"})
    assert refused.status_code == 403


def test_image_upload_over_the_cap_is_413_and_stores_nothing(client, monkeypatch):
    import collimate_server.cas as cas
    monkeypatch.setattr(cas, "IMPORT_MAX_FILE_BYTES", 4096)

    def body():
        for _ in range(4):
            yield b"x" * 2048

    response = client.post("/api/app-settings/images", content=body())
    assert response.status_code == 413, response.text
    declared = client.post("/api/app-settings/images", content=b"x" * 5000)
    assert declared.status_code == 413
    assert not cas.app_store().has(hashlib.sha256(b"x" * 8192).hexdigest())


def test_image_upload_is_a_streaming_route(fresh_cwd):
    """The upload writes its body to disk as it arrives, so the buffered
    body cap does not apply to it (``STREAMING_UPLOAD_ROUTES``); no other
    method or longer path shares the exemption."""
    from collimate_server.main import is_streaming_upload
    assert is_streaming_upload("POST", "/api/app-settings/images")
    assert not is_streaming_upload("PUT", "/api/app-settings/images")
    assert not is_streaming_upload("POST", "/api/app-settings/images/extra")
    assert not is_streaming_upload("POST", "/api/app-settings")
