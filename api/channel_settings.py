"""Per-channel settings document — the ``channel_settings`` row of a channel
DB's ``ui_state`` table. Serialized as JSON it is also the file the Settings
dialog exports and imports (``kind: "channel-settings"``).

One logical document per channel. Its sections split by *where the datum
lives*:

**Doc-stored sections** (the ``channel_settings`` row):

- ``keyboard`` → shortcut bindings ``{"overrides": {shortcutId: comboString},
  "commands": {commandKey: comboString}}``: ``overrides`` rebinds registry
  shortcuts, ``commands`` binds Commands pane rows (keys at most 200 chars)
- ``theme``    → ``{"activeThemeId": str|None, "userThemes": [UserTheme...]}``
- ``layout``   → canvas layout ``{defaultStackWidth, commandPane, removedShelfHeight, overviewShowTags?}``
- ``editing``  → editor preferences ``{"spellcheck": bool}``
- ``remove_zones`` → the overview's drop zones for removing a cell
  ``{"zones": [{"id", "width", "action"}]}``, ordered left to right; an
  action is ``{"kind": "remove"}``, ``{"kind": "add_tag", "tag"}`` or
  ``{"kind": "remove_tag", "tag"}``. Widths are fractions summing to 1; no
  zones means one full-width plain remove.
- ``renderer_styles`` → each renderer's style choices by renderer id
  ``{rendererId: {"theme", "font", "textSize"}}``: a theme id, a font
  catalogue family name and a text-size factor, each ``None`` for the
  renderer's default (the skin, the role's skin font, its own size).
  Entries whose three values are all ``None`` are dropped.

**Composed sections** — ONE authoritative store per datum. These live in their
own ``channel_config`` / ``ui_state`` rows (the discovery + management views own
them); the settings doc merely *composes* them in on read and *routes* their
writes back to ``channel_config`` on write, so a settings export/import is
complete without ever duplicating the source of truth:

- ``renderer_availability``   → ``{rendererId: bool}``  (``channel_config``)
- ``renderer_preferences``    → ``{extension: rendererName}`` (``channel_config``)

Writes go through ``merge_channel_settings`` (section-wholesale replace under a
merge lock), or
``apply_settings_import`` (a channel copy's full replace, or a section-replace
of the provided sections). Mirrors the ``channel_config.py`` storage pattern; shares its
``read_ui_state_json`` / ``write_ui_state_json`` helpers and
``ui_state_merge_lock``.
"""
from __future__ import annotations

import math
import re

import channel_config
from cell_tags import normalize_tag
from channel_config import read_ui_state_json, write_ui_state_json

CHANNEL_SETTINGS_KEY = "channel_settings"
SETTINGS_VERSION = 1
SETTINGS_KIND = "channel-settings"

# Sections stored in the channel_settings row itself.
DOC_SECTIONS = ("keyboard", "theme", "layout", "editing", "remove_zones", "renderer_styles")
# Sections whose authoritative store is a channel_config/ui_state row; composed
# into the doc on read, routed back to channel_config on write.
COMPOSED_SECTIONS = ("renderer_availability", "renderer_preferences")
SECTIONS = DOC_SECTIONS + COMPOSED_SECTIONS

# Serializes read-modify-write merges. SHARED with channel_config so a settings
# import that routes composed sections (renderer availability, renderer
# preferences) is mutually exclusive with the per-id management
# endpoints that write those same rows — otherwise the two paths hold different
# locks and a concurrent toggle is silently lost. Lock ordering is
# one-directional (channel_config never acquires anything here), so sharing it
# cannot deadlock.
_merge_lock = channel_config.ui_state_merge_lock()


def _default_command_pane() -> dict:
    return {"open": False, "side": "left", "query": "", "recents": []}


def default_settings() -> dict:
    return {
        "version": SETTINGS_VERSION,
        "kind": SETTINGS_KIND,
        "keyboard": {"overrides": {}, "commands": {}},
        "theme": {"activeThemeId": None, "userThemes": []},
        "layout": {
            "defaultStackWidth": None,
            "commandPane": _default_command_pane(),
            "removedShelfHeight": None,
        },
        "editing": {"spellcheck": True},
        "remove_zones": {"zones": []},
        "renderer_styles": {},
        "renderer_availability": {},
        "renderer_preferences": {},
    }


# ---------------------------------------------------------------------------
# Validation / normalization
# ---------------------------------------------------------------------------

COMMAND_KEY_MAX = 200


def _validate_keyboard(value) -> dict:
    """String-to-string maps only: ``overrides`` (registry id → combo) and
    ``commands`` (pane command key → combo, keys non-empty and at most
    ``COMMAND_KEY_MAX`` chars). Anything else is dropped."""
    section = value if isinstance(value, dict) else {}

    def strings(raw, key_ok=lambda k: True) -> dict:
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and key_ok(k)}

    return {
        "overrides": strings(section.get("overrides")),
        "commands": strings(section.get("commands"), lambda k: 0 < len(k) <= COMMAND_KEY_MAX),
    }


# The skin record: every token a user skin may override. Mirrors the keys of
# THEME_CSS_VARS in frontend/src/lib/themes.ts (a test pins the parity).
SKIN_VAR_KEYS = frozenset({
    "canvas", "sheet", "sheet2", "sheet3", "line", "line2", "ink", "ink2", "ink3",
    "brass", "brassSoft", "teal", "tealSoft", "tealSheet", "red", "redSoft", "green",
    "codeBg", "proseInk", "shadow", "lift", "fontUi", "fontMono", "fontProse", "radius",
})


def _is_skin_vars(value) -> bool:
    """A skin's ``vars``: token key to CSS value, both strings, every key
    alphanumeric (``sheet2``, ``brassSoft``)."""
    return isinstance(value, dict) and all(
        isinstance(k, str) and k.isascii() and k.isalnum() and isinstance(v, str)
        for k, v in value.items()
    )


def _validate_theme(value) -> dict:
    """``userThemes`` entries are the frontend's ``UserTheme`` objects: a dict
    with a string ``user.*`` id and string-to-string ``vars``. Any other entry
    is dropped; a kept entry's ``vars`` keep only the skin record's keys
    (``SKIN_VAR_KEYS``), and its other fields are kept as sent."""
    active = None
    themes: list[dict] = []
    if isinstance(value, dict):
        raw_active = value.get("activeThemeId")
        if isinstance(raw_active, str) and raw_active.strip():
            active = raw_active.strip()
        raw_themes = value.get("userThemes")
        if isinstance(raw_themes, list):
            for t in raw_themes:
                if (
                    isinstance(t, dict)
                    and isinstance(t.get("id"), str)
                    and t["id"].startswith("user.")
                    and _is_skin_vars(t.get("vars"))
                ):
                    vars_ = {k: v for k, v in t["vars"].items() if k in SKIN_VAR_KEYS}
                    themes.append({**t, "vars": vars_})
    return {"activeThemeId": active, "userThemes": themes}


def _validate_availability(value) -> dict:
    """``{id: bool}`` on/off map for generator or renderer availability. Drops
    non-string keys / non-bool values."""
    if not isinstance(value, dict):
        return {}
    return {
        str(k): bool(v)
        for k, v in value.items()
        if isinstance(k, str) and k.strip() and isinstance(v, bool)
    }


def _validate_renderer_preferences(value) -> dict:
    """``{extension: rendererName}`` string→string map."""
    if not isinstance(value, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in value.items()
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
    }


COMMAND_PANE_RECENTS = 3
COMMAND_PANE_QUERY_MAX = 200


def _validate_command_pane(value) -> dict:
    """The Commands pane: ``open``, the dock ``side`` (left or right), the
    typed ``query`` and up to three ``recents`` (command keys, most recent
    first). Anything else falls back to the default."""
    pane = _default_command_pane()
    if not isinstance(value, dict):
        return pane
    if isinstance(value.get("open"), bool):
        pane["open"] = value["open"]
    if value.get("side") in ("left", "right"):
        pane["side"] = value["side"]
    if isinstance(value.get("query"), str):
        pane["query"] = value["query"][:COMMAND_PANE_QUERY_MAX]
    if isinstance(value.get("recents"), list):
        pane["recents"] = [k for k in value["recents"] if isinstance(k, str) and k][:COMMAND_PANE_RECENTS]
    return pane


def _px_in(value, low: int, high: int) -> int | None:
    """``value`` as whole px when it is a number in [low, high], else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
        return int(value)
    return None


def _validate_layout(value) -> dict:
    """Canvas-layout preferences. ``defaultStackWidth`` is the width the
    stack width control's Reset returns to (px; None = the built-in 500);
    ``commandPane`` is the Commands pane's state; ``removedShelfHeight`` is
    the overview's Removed shelf open height (px; None = the built-in 316;
    the shelf shuts below 110, so a smaller height is never kept);
    ``overviewShowTags`` is the overview's tag toggle, kept only as a bool
    (absent = off)."""
    if not isinstance(value, dict):
        value = {}
    layout = {
        "defaultStackWidth": _px_in(value.get("defaultStackWidth"), 100, 20000),
        "commandPane": _validate_command_pane(value.get("commandPane")),
        "removedShelfHeight": _px_in(value.get("removedShelfHeight"), 110, 20000),
    }
    if isinstance(value.get("overviewShowTags"), bool):
        layout["overviewShowTags"] = value["overviewShowTags"]
    return layout


def _validate_editing(value) -> dict:
    """Editor preferences: ``spellcheck`` underlines misspelled words in the
    editors (on by default). A non-bool value falls back to the default."""
    editing = default_settings()["editing"]
    if isinstance(value, dict) and isinstance(value.get("spellcheck"), bool):
        editing["spellcheck"] = value["spellcheck"]
    return editing


REMOVE_ZONES_MAX = 6
REMOVE_ZONE_ID_MAX = 64
_ZONE_TAG_KINDS = ("add_tag", "remove_tag")


def _validate_zone_action(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "remove":
        return {"kind": "remove"}
    if kind in _ZONE_TAG_KINDS:
        tag = normalize_tag(value.get("tag"))
        return {"kind": kind, "tag": tag} if tag is not None else None
    return None


def _validate_remove_zones(value) -> dict:
    """The overview's remove zones in order. A zone needs a unique string
    ``id`` of at most 64 characters, a number ``width`` above 0 and a valid
    action (a tag action's tag normalized by ``cell_tags.normalize_tag``);
    any other entry is dropped. The first ``REMOVE_ZONES_MAX`` kept zones stay, and
    their widths are rescaled to sum to exactly 1 in steps of 1e-4, each at
    least 1e-4 (``_width_units``)."""
    zones: list[dict] = []
    raw = value.get("zones") if isinstance(value, dict) else None
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        zone_id, width = entry.get("id"), entry.get("width")
        if (not isinstance(zone_id, str) or not zone_id or len(zone_id) > REMOVE_ZONE_ID_MAX
                or any(zone["id"] == zone_id for zone in zones)):
            continue
        if not isinstance(width, (int, float)) or isinstance(width, bool) or not 0 < width < float("inf"):
            continue
        action = _validate_zone_action(entry.get("action"))
        if action is not None:
            zones.append({"id": zone_id, "width": float(width), "action": action})
    zones = zones[:REMOVE_ZONES_MAX]
    for zone, units in zip(zones, _width_units([zone["width"] for zone in zones])):
        zone["width"] = units / _WIDTH_UNITS
    return {"zones": zones}


_WIDTH_UNITS = 10_000


def _width_units(widths: list[float]) -> list[int]:
    """``widths`` as whole 1e-4 units summing to exactly 1, each at least one
    unit: scaled by the largest first (no overflow on huge widths), then
    floored with the shortfall given to the largest remainders and any
    excess from the one-unit minimum taken from the widest zones. Units
    divided back out reproduce themselves, so validation is idempotent."""
    if not widths:
        return []
    peak = max(widths)
    shares = [width / peak for width in widths]
    total = math.fsum(shares)
    exact = [round(share / total * _WIDTH_UNITS, 6) for share in shares]
    units = [max(1, math.floor(value)) for value in exact]
    by_remainder = sorted(range(len(units)), key=lambda i: (math.floor(exact[i]) - exact[i], i))
    short = _WIDTH_UNITS - sum(units)
    for i in by_remainder[:max(short, 0)]:
        units[i] += 1
    while sum(units) > _WIDTH_UNITS:
        units[max(range(len(units)), key=lambda i: (units[i], -i))] -= 1
    return units


RENDERER_STYLES_MAX = 256
RENDERER_STYLE_THEME_MAX = 64
RENDERER_STYLE_FONT_MAX = 120
RENDERER_STYLE_TEXT_SIZE_MIN = 0.5
RENDERER_STYLE_TEXT_SIZE_MAX = 3.0
_RENDERER_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _style_string(value, limit: int) -> str | None:
    """A non-blank string of at most ``limit`` characters, stripped; else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def _style_text_size(value) -> float | None:
    """A finite number clamped into [0.5, 3] and rounded to 0.01; else None."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:  # a JSON integer too large for a float
        return None
    if not math.isfinite(number):
        return None
    clamped = min(max(number, RENDERER_STYLE_TEXT_SIZE_MIN), RENDERER_STYLE_TEXT_SIZE_MAX)
    return round(clamped, 2)


def _validate_renderer_styles(value) -> dict:
    """Each renderer's style choices keyed by renderer id (``^[A-Za-z0-9._-]{1,128}$``;
    the first ``RENDERER_STYLES_MAX`` valid ids stay). ``theme`` is a theme id of
    at most 64 characters, ``font`` a catalogue family name of at most 120,
    ``textSize`` a factor clamped into [0.5, 3] and rounded to 0.01; any other
    value is None, the renderer's default. An entry left all None is dropped."""
    styles: dict[str, dict] = {}
    if not isinstance(value, dict):
        return styles
    for renderer_id, entry in value.items():
        if len(styles) >= RENDERER_STYLES_MAX:
            break
        if not isinstance(renderer_id, str) or not _RENDERER_ID_RE.fullmatch(renderer_id):
            continue
        if not isinstance(entry, dict):
            continue
        style = {
            "theme": _style_string(entry.get("theme"), RENDERER_STYLE_THEME_MAX),
            "font": _style_string(entry.get("font"), RENDERER_STYLE_FONT_MAX),
            "textSize": _style_text_size(entry.get("textSize")),
        }
        if any(v is not None for v in style.values()):
            styles[renderer_id] = style
    return styles


_SECTION_VALIDATORS = {
    "keyboard": _validate_keyboard,
    "theme": _validate_theme,
    "layout": _validate_layout,
    "editing": _validate_editing,
    "remove_zones": _validate_remove_zones,
    "renderer_styles": _validate_renderer_styles,
    "renderer_availability": _validate_availability,
    "renderer_preferences": _validate_renderer_preferences,
}


def validate(doc) -> dict:
    """Normalize a full document (e.g. an imported settings file), including
    the composed sections. Unknown top-level keys are dropped; missing sections
    default. Raises ValueError only when ``doc`` isn't a settings-shaped object
    at all."""
    if not isinstance(doc, dict):
        raise ValueError("settings must be a JSON object")
    if "kind" in doc and doc["kind"] != SETTINGS_KIND:
        raise ValueError(f"not a channel-settings document (kind={doc['kind']!r})")
    out = {"version": SETTINGS_VERSION, "kind": SETTINGS_KIND}
    for section, validator in _SECTION_VALIDATORS.items():
        out[section] = validator(doc.get(section))
    return out


# ---------------------------------------------------------------------------
# Composition: doc-row subset ↔ channel_config-owned sections
# ---------------------------------------------------------------------------

def _doc_row_subset(doc: dict) -> dict:
    """The part of ``doc`` actually stored in the ``channel_settings`` row —
    version/kind + the doc sections. Composed sections are NOT stored here (they
    live in channel_config), so they can't drift from their source of truth."""
    out = {"version": SETTINGS_VERSION, "kind": SETTINGS_KIND}
    for section in DOC_SECTIONS:
        out[section] = doc.get(section, default_settings()[section])
    return out


def _check_composed_sections(doc: dict) -> None:
    """Refuse a document whose composed sections the channel would not take
    (a renderer preference that moves a built-in surface), before anything
    is written."""
    channel_config.check_renderer_preferences(doc.get("renderer_preferences", {}))


def _write_composed_sections(db_name: str, doc: dict, *, conn=None) -> None:
    """Route the composed sections of ``doc`` to their authoritative
    channel_config/ui_state rows (wholesale replace). Callers already hold the
    shared ``_merge_lock``, so this writes the rows directly (the values in
    ``doc`` were read live under that same lock) rather than through
    channel_config's locking setters, which would re-acquire it and deadlock."""
    write_ui_state_json(
        db_name, channel_config.RENDERER_AVAILABILITY_KEY,
        doc.get("renderer_availability", {}), conn=conn,
    )
    write_ui_state_json(
        db_name, channel_config.RENDERER_PREFERENCES_KEY,
        doc.get("renderer_preferences", {}), conn=conn,
    )


# ---------------------------------------------------------------------------
# Load / save / merge
# ---------------------------------------------------------------------------

def load_channel_settings(db_name: str, *, conn=None) -> dict:
    """The channel's full settings doc — the doc-row sections plus the composed
    sections read live from their channel_config source of truth. A channel with
    no row gets fresh defaults (nothing is materialized until the first write)."""
    raw = read_ui_state_json(db_name, CHANNEL_SETTINGS_KEY, None, conn=conn)
    if raw is None:
        doc = default_settings()
    else:
        try:
            doc = validate(raw)
        except ValueError:
            doc = default_settings()
    # Compose the authoritative composed sections over whatever the row held.
    doc["renderer_availability"] = channel_config.read_renderer_availability(db_name, conn=conn)
    doc["renderer_preferences"] = channel_config.read_renderer_preferences(db_name, conn=conn)
    return doc


def save_channel_settings(db_name: str, doc: dict) -> dict:
    """Validate and store a full document: doc sections into the settings row,
    composed sections routed to channel_config. Returns the composed doc."""
    normalized = validate(doc)
    _check_composed_sections(normalized)
    with _merge_lock:
        write_ui_state_json(db_name, CHANNEL_SETTINGS_KEY, _doc_row_subset(normalized))
        _write_composed_sections(db_name, normalized)
    return normalized


def merge_channel_settings(db_name: str, partial: dict) -> dict:
    """Replace each *provided* top-level section wholesale; omitted sections
    are untouched. Composed sections present in the body route to channel_config.
    Raises ValueError on a non-dict body or unknown sections."""
    if not isinstance(partial, dict):
        raise ValueError("settings body must be a JSON object")
    if "kind" in partial and partial["kind"] != SETTINGS_KIND:
        raise ValueError(f"not a channel-settings document (kind={partial['kind']!r})")
    unknown = [k for k in partial if k not in _SECTION_VALIDATORS and k not in ("version", "kind")]
    if unknown:
        raise ValueError(f"unknown settings section(s): {', '.join(sorted(unknown))}")
    with _merge_lock:
        doc = load_channel_settings(db_name)
        touched_composed = False
        for section, validator in _SECTION_VALIDATORS.items():
            if section in partial:
                doc[section] = validator(partial[section])
                if section in COMPOSED_SECTIONS:
                    touched_composed = True
        _check_composed_sections(doc)
        write_ui_state_json(db_name, CHANNEL_SETTINGS_KEY, _doc_row_subset(doc))
        if touched_composed:
            _write_composed_sections(db_name, doc)
        return doc


def apply_settings_import(db_name: str, doc: dict, *, replace: bool, conn=None) -> dict:
    """Apply a settings document, replacing all or only its provided sections.

    A fresh-channel seed replaces the whole document. Loading into an existing
    channel replaces each provided section and leaves absent sections unchanged.
    Composed sections route to their authoritative channel_config rows.
    """
    if not isinstance(doc, dict):
        raise ValueError("settings must be a JSON object")
    if "kind" in doc and doc["kind"] != SETTINGS_KIND:
        raise ValueError(f"not a channel-settings document (kind={doc['kind']!r})")
    with _merge_lock:
        if replace:
            final = validate(doc)
        else:
            final = load_channel_settings(db_name, conn=conn)
            for section, validator in _SECTION_VALIDATORS.items():
                if section in doc:
                    final[section] = validator(doc[section])
        _check_composed_sections(final)
        write_ui_state_json(db_name, CHANNEL_SETTINGS_KEY, _doc_row_subset(final), conn=conn)
        _write_composed_sections(db_name, final, conn=conn)
        return final
