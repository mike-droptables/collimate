"""Coverage for the per-channel settings doc (channel_settings.py) and the
global-account separation.

All fixtures are synthetic — no on-disk generator names, no real keychain
(keystore is monkeypatched where values matter).
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# channel_settings — validate / merge / round-trip
# ---------------------------------------------------------------------------

def test_defaults_shape(fresh_cwd):
    from collimate_server.channel_settings import default_settings
    doc = default_settings()
    assert doc["kind"] == "channel-settings"
    assert "sync" not in doc
    assert "keys" not in doc
    assert doc["keyboard"] == {"overrides": {}, "commands": {}}
    assert doc["theme"] == {"override": None}
    assert "editing" not in doc
    assert doc["remove_zones"] == {"zones": []}
    assert doc["renderer_styles"] == {}
    # v1 composed sections default empty.
    assert doc["renderer_availability"] == {}
    assert doc["renderer_preferences"] == {}
    # Per-generator availability is gone (relays own picking).
    assert "generator_availability" not in doc


def test_missing_row_serves_defaults_without_materializing(fresh_cwd):
    from collimate_server.channel_settings import load_channel_settings, default_settings
    assert load_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3") == default_settings()


def test_merge_is_section_wholesale(fresh_cwd):
    from collimate_server.channel_settings import merge_channel_settings, load_channel_settings
    merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", {"layout": {"removedShelfHeight": 300}})
    merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", {"keyboard": {"overrides": {"a": "Mod+A"}}})
    doc = load_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3")
    # The layout merge filled missing fields with defaults; the keyboard merge
    # did not touch layout.
    assert doc["layout"]["removedShelfHeight"] == 300
    assert doc["layout"]["commandPane"]["side"] == "left"
    assert doc["keyboard"]["overrides"] == {"a": "Mod+A"}


def test_validate_keyboard_keeps_string_maps_and_bounds_command_keys(fresh_cwd):
    """The keyboard section keeps ``overrides`` and ``commands`` as string to
    string maps; a command key must be non-empty and at most 200 chars;
    non-string values, non-dict maps and unknown keys are dropped (CP-2)."""
    from collimate_server.channel_settings import validate
    assert validate({})["keyboard"] == {"overrides": {}, "commands": {}}
    assert validate({"keyboard": "junk"})["keyboard"] == {"overrides": {}, "commands": {}}
    long_key = "k" * 201
    doc = validate({"keyboard": {
        "overrides": {"stack.run": "Alt+r", "bad": 3},
        "commands": {"log": "Alt+l", "": "Alt+x", long_key: "Alt+y", "k" * 200: "Alt+z", "num": 1},
        "extra": {"a": "b"},
    }})
    assert doc["keyboard"] == {
        "overrides": {"stack.run": "Alt+r"},
        "commands": {"log": "Alt+l", "k" * 200: "Alt+z"},
    }
    assert validate({"keyboard": {"overrides": {}, "commands": ["log"]}})["keyboard"]["commands"] == {}


def test_merge_rejects_unknown_sections_and_non_dict(fresh_cwd):
    from collimate_server.channel_settings import merge_channel_settings
    with pytest.raises(ValueError):
        merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", {"bogus": {}})
    with pytest.raises(ValueError):
        merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", ["not", "a", "dict"])


def test_native_settings_do_not_store_global_account_or_secret_data(fresh_cwd):
    from collimate_server.channel_settings import validate, merge_channel_settings
    doc = validate({"kind": "channel-settings", "keys": {"TOKEN": {"alias": "account", "value": "private"}}})
    assert "keys" not in doc
    with pytest.raises(ValueError):
        merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", {"keys": {"TOKEN": {"alias": "account"}}})


def test_validate_rejects_wrong_kind(fresh_cwd):
    from collimate_server.channel_settings import validate
    with pytest.raises(ValueError):
        validate({"kind": "colchan"})


def test_theme_section_holds_only_the_override(fresh_cwd):
    """The theme section is the channel's override: a theme id, stripped, of
    at most 64 characters; anything else is None (the server's default). The
    themes themselves and the default are server-wide (app_settings), so no
    user theme or default id survives here."""
    from collimate_server.channel_settings import validate
    assert validate({"theme": {"override": " paper "}})["theme"] == {"override": "paper"}
    assert validate({"theme": {"override": "t" * 64}})["theme"] == {"override": "t" * 64}
    for bad in ("t" * 65, "", "   ", 3, None, ["paper"], {"id": "paper"}):
        assert validate({"theme": {"override": bad}})["theme"] == {"override": None}
    for bad_section in (None, "paper", [], {}):
        assert validate({"theme": bad_section})["theme"] == {"override": None}
    old = validate({"theme": {"activeThemeId": "user.a", "userThemes": [{"id": "user.a", "vars": {}}]}})
    assert old["theme"] == {"override": None}


def test_an_old_shape_row_reads_as_the_new_defaults(fresh_cwd):
    """A row written before the theme library moved to the server and the
    editing preferences moved to the profile: its theme reads as no override,
    its editing section is dropped, its other sections stay, and the next
    write stores the current shape."""
    import collimate_server.channel_config as channel_config
    from collimate_server.channel_settings import CHANNEL_SETTINGS_KEY, load_channel_settings, merge_channel_settings
    db = "639e2b64-9d6e-4849-9f67-07896c065ac3"
    channel_config.write_ui_state_json(db, CHANNEL_SETTINGS_KEY, {
        "version": 1, "kind": "channel-settings",
        "keyboard": {"overrides": {"a": "Mod+A"}, "commands": {}},
        "theme": {"activeThemeId": "user.synth", "userThemes": [
            {"id": "user.synth", "label": "Synth", "vars": {"ink": "#eee"}, "scene": {"image": "c" * 64}}]},
        "editing": {"spellcheck": False, "status_on_run": False},
    })
    doc = load_channel_settings(db)
    assert doc["theme"] == {"override": None}
    assert "editing" not in doc
    assert doc["keyboard"]["overrides"] == {"a": "Mod+A"}
    merge_channel_settings(db, {"layout": {"removedShelfHeight": 300}})
    stored = channel_config.read_ui_state_json(db, CHANNEL_SETTINGS_KEY)
    assert stored["theme"] == {"override": None}
    assert "editing" not in stored


def test_put_theme_override_round_trips_and_editing_is_refused(client):
    """A PUT of ``{theme: {override}}`` round-trips; the editing section is
    no longer the channel's (the profile holds it), so a PUT naming it is a
    400 that changes nothing."""
    r = client.post("/api/channels", params={"name": "synth-override"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    put = client.put(route, json={"theme": {"override": "paper"}})
    assert put.status_code == 200, put.text
    assert put.json()["theme"] == {"override": "paper"}
    assert client.get(route).json()["theme"] == {"override": "paper"}
    refused = client.put(route, json={"editing": {"spellcheck": False}})
    assert refused.status_code == 400
    assert "editing" in refused.json()["detail"]
    assert "editing" not in client.get(route).json()
    put = client.put(route, json={"theme": {"override": None}})
    assert put.status_code == 200, put.text
    assert client.get(route).json()["theme"] == {"override": None}


# ---------------------------------------------------------------------------
# Composed sections: renderer_availability / renderer_preferences live in
# channel_config; the doc COMPOSES them on read and ROUTES them to
# channel_config on write. The settings row must never store them (no drift
# from their source of truth). (Per-generator availability is gone — relays
# own picking.)
# ---------------------------------------------------------------------------

def test_composed_sections_read_live_from_channel_config(fresh_cwd):
    import collimate_server.channel_config as channel_config
    from collimate_server.channel_settings import load_channel_settings

    channel_config.set_renderer_availability("639e2b64-9d6e-4849-9f67-07896c065ac3", "synth_ren", False)
    channel_config.write_renderer_preferences("639e2b64-9d6e-4849-9f67-07896c065ac3", {".md": "synth_md"})

    doc = load_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3")
    assert doc["renderer_availability"] == {"synth_ren": False}
    assert doc["renderer_preferences"] == {".md": "synth_md"}


def test_merge_routes_composed_to_channel_config_not_the_row(fresh_cwd):
    import collimate_server.channel_config as channel_config
    from collimate_server.channel_settings import (
        merge_channel_settings, CHANNEL_SETTINGS_KEY, COMPOSED_SECTIONS,
    )

    merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", {
        "kind": "channel-settings",
        "renderer_availability": {"synth_ren": True},
        "renderer_preferences": {".txt": "synth_text"},
    })

    # Authoritative stores got the values.
    assert channel_config.read_renderer_availability("639e2b64-9d6e-4849-9f67-07896c065ac3") == {"synth_ren": True}
    assert channel_config.read_renderer_preferences("639e2b64-9d6e-4849-9f67-07896c065ac3") == {".txt": "synth_text"}

    # The settings row itself stores ONLY the doc sections — never the composed
    # ones (they'd drift from the source of truth otherwise).
    stored = channel_config._read_json("639e2b64-9d6e-4849-9f67-07896c065ac3", CHANNEL_SETTINGS_KEY, None)
    assert stored is not None
    for section in COMPOSED_SECTIONS:
        assert section not in stored


def test_merge_routes_composed_section(fresh_cwd):
    import collimate_server.channel_config as channel_config
    from collimate_server.channel_settings import merge_channel_settings

    merge_channel_settings("639e2b64-9d6e-4849-9f67-07896c065ac3", {"renderer_availability": {"synth_ren": False}})
    assert channel_config.read_renderer_availability("639e2b64-9d6e-4849-9f67-07896c065ac3") == {"synth_ren": False}


def test_put_settings_composes_renderer_availability(client):
    """End-to-end: PUT settings with a composed section routes to channel_config,
    and GET settings composes it back."""
    r = client.post("/api/channels", params={"name": "synth-composed"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    put = client.put(route,
                     json={"renderer_availability": {"synth_ren": False}})
    assert put.status_code == 200, put.text
    assert put.json()["renderer_availability"] == {"synth_ren": False}
    got = client.get(route).json()
    assert got["renderer_availability"] == {"synth_ren": False}


def test_channel_theme_accent_route_is_gone_S06(client):
    """The per-channel accent route that tinted the top bar is deleted: no
    route serves it and a GET reads nothing. The path is assembled so the W1
    exit grep for the retired name finds nothing in api/. The path still has
    the shape of ``DELETE /api/channels/{channel_id}``, so a GET is refused
    as a method that path does not take (405), never answered."""
    retired = "/api/channels/theme" + "_accents"
    assert all(getattr(route, "path", "") != retired for route in client.app.routes)
    assert client.get(retired).status_code in (404, 405)


def test_layout_keeps_the_command_pane_K01(client):
    """The Commands pane's state rides the layout section, per channel: a PUT
    of the whole section round-trips it next to the stack width; a bad side,
    a non-bool open, a long query and extra recents are normalized."""
    r = client.post("/api/channels", params={"name": "synth-pane"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    assert client.get(route).json()["layout"] == {
        "defaultStackWidth": None,
        "commandPane": {"open": False, "side": "left", "query": "", "recents": []},
        "removedShelfHeight": None,
    }
    pane = {"open": True, "side": "right", "query": "fan", "recents": ["run", "log", "zoom"]}
    put = client.put(route,
                     json={"layout": {"defaultStackWidth": 640, "commandPane": pane}})
    assert put.status_code == 200, put.text
    assert client.get(route).json()["layout"] == {
        "defaultStackWidth": 640, "commandPane": pane, "removedShelfHeight": None,
    }
    from collimate_server.channel_settings import validate
    odd = validate({"layout": {"commandPane": {
        "open": "yes", "side": "top", "query": "q" * 500, "recents": ["a", 3, "b", "c", "d"],
    }}})["layout"]["commandPane"]
    assert odd == {"open": False, "side": "left", "query": "q" * 200, "recents": ["a", "b", "c"]}



def test_layout_keeps_the_removed_shelf_height_O05(client):
    """The overview's Removed shelf height rides the layout section, per
    channel, next to the other layout keys; a height below the shelf's
    110px minimum, a bool or a string is dropped (None: the built-in 316)."""
    r = client.post("/api/channels", params={"name": "synth-shelf"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    assert client.get(route).json()["layout"]["removedShelfHeight"] is None
    put = client.put(route,
                     json={"layout": {"defaultStackWidth": None, "removedShelfHeight": 240.6}})
    assert put.status_code == 200, put.text
    layout = client.get(route).json()["layout"]
    assert layout["removedShelfHeight"] == 240
    assert layout["commandPane"]["side"] == "left"
    from collimate_server.channel_settings import validate
    for bad in (109, True, "300", -1, None):
        assert validate({"layout": {"removedShelfHeight": bad}})["layout"]["removedShelfHeight"] is None


def test_layout_keeps_the_overview_tag_toggle(client):
    """The overview's tag toggle rides the layout section, per channel: absent
    by default, a PUT of the whole section keeps it next to the other layout
    keys, and anything but a bool is dropped (absent: off)."""
    r = client.post("/api/channels", params={"name": "synth-show-tags"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    assert "overviewShowTags" not in client.get(route).json()["layout"]
    put = client.put(route, json={"layout": {
        "defaultStackWidth": None, "removedShelfHeight": 240, "overviewShowTags": True,
    }})
    assert put.status_code == 200, put.text
    layout = client.get(route).json()["layout"]
    assert layout["overviewShowTags"] is True
    assert layout["removedShelfHeight"] == 240
    assert layout["commandPane"]["side"] == "left"
    put = client.put(route, json={"layout": {**layout, "overviewShowTags": False}})
    assert put.status_code == 200, put.text
    assert client.get(route).json()["layout"]["overviewShowTags"] is False
    from collimate_server.channel_settings import validate
    for bad in (1, "true", None, [True]):
        assert "overviewShowTags" not in validate({"layout": {"overviewShowTags": bad}})["layout"]

def test_validate_remove_zones_drops_bad_entries_and_normalizes_TG06(fresh_cwd):
    """Each zone needs a unique id of at most 64 characters, a positive number
    width and a valid action; a tag action's tag is normalized and an empty
    one drops the zone. At most six zones stay, their widths rescaled to sum
    to exactly 1 in steps of 1e-4, none below 1e-4; validating again changes
    nothing."""
    from collimate_server.channel_settings import REMOVE_ZONES_MAX, validate
    zones = [
        {"id": "a", "width": 2, "action": {"kind": "add_tag", "tag": "  needs   review "}},
        {"id": "b", "width": 1, "action": {"kind": "remove", "tag": "ignored"}},
        {"id": "c", "width": 1, "action": {"kind": "remove_tag", "tag": "Draft"}, "extra": True},
        {"id": "a", "width": 1, "action": {"kind": "remove"}},                  # duplicate id
        {"id": "x" * 65, "width": 1, "action": {"kind": "remove"}},             # id too long
        {"id": "", "width": 1, "action": {"kind": "remove"}},                   # empty id
        {"id": 5, "width": 1, "action": {"kind": "remove"}},                    # id not a string
        {"id": "w0", "width": 0, "action": {"kind": "remove"}},                 # width not > 0
        {"id": "w1", "width": True, "action": {"kind": "remove"}},              # width a bool
        {"id": "w2", "width": "1", "action": {"kind": "remove"}},               # width a string
        {"id": "w3", "width": float("nan"), "action": {"kind": "remove"}},
        {"id": "t0", "width": 1, "action": {"kind": "add_tag", "tag": "   "}},  # empty tag
        {"id": "t1", "width": 1, "action": {"kind": "add_tag"}},                # no tag
        {"id": "t2", "width": 1, "action": {"kind": "remove_tag", "tag": "a\x00b"}},
        {"id": "k0", "width": 1, "action": {"kind": "archive"}},
        {"id": "k1", "width": 1, "action": "remove"},
        "garbage",
    ]
    assert validate({"remove_zones": {"zones": zones}})["remove_zones"] == {"zones": [
        {"id": "a", "width": 0.5, "action": {"kind": "add_tag", "tag": "needs review"}},
        {"id": "b", "width": 0.25, "action": {"kind": "remove"}},
        {"id": "c", "width": 0.25, "action": {"kind": "remove_tag", "tag": "Draft"}},
    ]}
    many = [{"id": f"z{i}", "width": 1, "action": {"kind": "remove"}} for i in range(REMOVE_ZONES_MAX + 2)]
    capped = validate({"remove_zones": {"zones": many}})["remove_zones"]["zones"]
    assert [z["id"] for z in capped] == [f"z{i}" for i in range(REMOVE_ZONES_MAX)]
    assert [z["width"] for z in capped] == [0.1667] * 4 + [0.1666] * 2

    def widths(*values):
        doc = {"remove_zones": {"zones": [{"id": f"w{i}", "width": w, "action": {"kind": "remove"}}
                                          for i, w in enumerate(values)]}}
        once = validate(doc)
        assert validate(once)["remove_zones"] == once["remove_zones"]
        got = [z["width"] for z in once["remove_zones"]["zones"]]
        assert round(sum(got) * 10_000) == 10_000 and min(got) >= 0.0001
        return got
    assert widths(1e-6, 1) == [0.0001, 0.9999]               # a sliver keeps one step
    assert widths(1e308, 1e308) == [0.5, 0.5]                # no overflow to inf
    assert widths(1, 1, 1) == [0.3334, 0.3333, 0.3333]       # the remainder goes first
    assert widths(5e-324, 1e308, 1) == [0.0001, 0.9998, 0.0001]
    for empty in (None, [], {}, {"zones": "no"}, {"zones": [None]}):
        assert validate({"remove_zones": empty})["remove_zones"] == {"zones": []}


def test_put_remove_zones_is_section_wholesale_TG06(client):
    r = client.post("/api/channels", params={"name": "synth-zones"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    assert client.get(route).json()["remove_zones"] == {"zones": []}
    zones = [{"id": "z1", "width": 3, "action": {"kind": "add_tag", "tag": "later"}},
             {"id": "z2", "width": 1, "action": {"kind": "remove"}}]
    put = client.put(route, json={"remove_zones": {"zones": zones}})
    assert put.status_code == 200, put.text
    assert client.put(route, json={"keyboard": {"overrides": {"a": "Mod+A"}}}).status_code == 200
    got = client.get(route).json()
    assert got["remove_zones"] == {"zones": [
        {"id": "z1", "width": 0.75, "action": {"kind": "add_tag", "tag": "later"}},
        {"id": "z2", "width": 0.25, "action": {"kind": "remove"}}]}
    assert got["keyboard"]["overrides"] == {"a": "Mod+A"}
    put = client.put(route, json={"remove_zones": {"zones": [zones[1]]}})
    assert put.status_code == 200, put.text
    assert client.get(route).json()["remove_zones"] == {"zones": [
        {"id": "z2", "width": 1.0, "action": {"kind": "remove"}}]}


def test_validate_renderer_styles_drops_bad_ids_clamps_and_drops_nulls_RS02(fresh_cwd):
    from collimate_server.channel_settings import RENDERER_STYLES_MAX, validate

    def styles(value):
        return validate({"renderer_styles": value})["renderer_styles"]

    for empty in (None, [], "x", 3, {}):
        assert styles(empty) == {}
    # Ids: the pattern ^[A-Za-z0-9._-]{1,128}$ only; a trailing newline or a
    # 129-character id is refused, a 128-character one kept.
    good_long = "r" * 128
    got = styles({
        "milkdown": {"theme": "nord"},
        "bad id": {"theme": "nord"},
        "bad/id": {"theme": "nord"},
        "tail\n": {"theme": "nord"},
        "": {"theme": "nord"},
        "r" * 129: {"theme": "nord"},
        good_long: {"textSize": 1},
        "not-a-dict": "nord",
    })
    assert got == {
        "milkdown": {"theme": "nord", "font": None, "textSize": None},
        good_long: {"theme": None, "font": None, "textSize": 1.0},
    }
    # Values: strings stripped and capped (theme 64, font 120), anything else
    # None; textSize a finite number clamped into [0.5, 3] and rounded to 0.01.
    got = styles({
        "a": {"theme": " frame ", "font": "Lora", "textSize": 1.234},
        "b": {"theme": "t" * 65, "font": "f" * 121, "textSize": 9},
        "c": {"theme": "t" * 64, "font": "f" * 120, "textSize": 0.1},
        "d": {"theme": 3, "font": ["Lora"], "textSize": True},
        "e": {"theme": "", "font": "   ", "textSize": float("nan")},
        "f": {"theme": None, "font": None, "textSize": None},
        "g": {"textSize": float("inf")},
        "h": {"textSize": "1.1"},
        "i": {"theme": "darkened", "extra": 1},
        # An integer too large for a float (json.loads keeps it exact) is
        # dropped, never an OverflowError that would surface as a 500.
        "j": {"textSize": 10**400},
        "k": {"textSize": json.loads("1" + "0" * 400)},
    })
    assert got == {
        "a": {"theme": "frame", "font": "Lora", "textSize": 1.23},
        "b": {"theme": None, "font": None, "textSize": 3.0},
        "c": {"theme": "t" * 64, "font": "f" * 120, "textSize": 0.5},
        "i": {"theme": "darkened", "font": None, "textSize": None},
    }
    # At most RENDERER_STYLES_MAX ids stay, the first ones in order.
    many = {f"r{i}": {"textSize": 1} for i in range(RENDERER_STYLES_MAX + 10)}
    kept = styles(many)
    assert len(kept) == RENDERER_STYLES_MAX
    assert list(kept)[:2] == ["r0", "r1"] and f"r{RENDERER_STYLES_MAX}" not in kept
    # Validation is idempotent.
    assert styles(got) == got


def test_put_renderer_styles_is_section_wholesale_RS02(client):
    """renderer_styles rides the doc row: a PUT replaces the whole section,
    other sections are untouched, the composed renderer_preferences section is
    a different datum, and an all-null entry put back disappears."""
    r = client.post("/api/channels", params={"name": "synth-styles"})
    assert r.status_code == 200, r.text
    route = f'/api/channels/{r.json()["id"]}/settings'
    assert client.get(route).json()["renderer_styles"] == {}
    body = {"milkdown": {"theme": "nord", "font": "Lora", "textSize": 1.25},
            "html": {"theme": "darkened", "font": None, "textSize": None}}
    put = client.put(route, json={"renderer_styles": body})
    assert put.status_code == 200, put.text
    assert put.json()["renderer_styles"] == body
    assert client.put(route, json={"keyboard": {"overrides": {"a": "Mod+A"}}}).status_code == 200
    got = client.get(route).json()
    assert got["renderer_styles"] == body
    assert got["keyboard"]["overrides"] == {"a": "Mod+A"}
    assert got["renderer_preferences"] == {}
    put = client.put(route, json={"renderer_styles": {
        "milkdown": {"theme": None, "font": None, "textSize": None},
        "html": body["html"]}})
    assert put.status_code == 200, put.text
    assert client.get(route).json()["renderer_styles"] == {"html": body["html"]}
    # A textSize integer too large for a float is dropped (200), not a 500.
    raw = '{"renderer_styles": {"html": {"theme": "darkened", "textSize": 1' + "0" * 400 + '}}}'
    put = client.put(route, content=raw, headers={"Content-Type": "application/json"})
    assert put.status_code == 200, put.text
    assert put.json()["renderer_styles"] == {"html": body["html"]}


def test_merge_renderer_styles_round_trips_RS02(fresh_cwd):
    from collimate_server.channel_settings import load_channel_settings, merge_channel_settings
    db = "639e2b64-9d6e-4849-9f67-07896c065ac3"
    styles = {"monaco": {"theme": "vs-dark", "font": "JetBrains Mono", "textSize": 1.1}}
    merged = merge_channel_settings(db, {"renderer_styles": styles})
    assert merged["renderer_styles"] == styles
    merge_channel_settings(db, {"layout": {"removedShelfHeight": 300}})
    doc = load_channel_settings(db)
    assert doc["renderer_styles"] == styles
    assert merge_channel_settings(db, {"renderer_styles": doc["renderer_styles"]}) == doc
