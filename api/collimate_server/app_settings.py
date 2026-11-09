"""Server-wide settings document — the ``app_settings`` row of ``app.db``'s
``ui_state`` table, shared by every channel on this server (one data dir).

One section today:

- ``theme`` → the theme library ``{"defaultThemeId": str|None, "userThemes":
  [UserTheme...]}``. ``defaultThemeId`` is the theme of the channels page and
  of every channel whose own doc does not override it (``channel_settings``'
  ``theme.override``); None means the built-in default. It is not checked
  against the library here: the client resolves an unknown id to the built-in
  default. A user theme overrides its base's tokens (``vars``) and scene
  (``scene``: the host-only ground, pattern, glows, vignette, selection edge
  and picture).

A scene picture is a body in the installation's store (``cas.app_store()``,
``.collimate/app-objects/``), uploaded through ``POST /api/app-settings/images``.
Each write admits the pictures the library names (``admit_scene_images``), and
the collector keeps them live while the row names them (``cas_gc``).

Writes go through ``merge_app_settings``: each provided section replaces the
stored one wholesale, under this module's lock. A missing or unreadable row
reads as the defaults; nothing is written until the first change.
"""
from __future__ import annotations

import logging
import math
import re
import threading

from collimate_server.cas import is_cas_hash
from collimate_server.database import read_global_json, write_global_json

logger = logging.getLogger(__name__)

APP_SETTINGS_KEY = "app_settings"
APP_SETTINGS_VERSION = 1
APP_SETTINGS_KIND = "app-settings"

# Serializes read-modify-write merges of the one row.
_merge_lock = threading.Lock()


def default_app_settings() -> dict:
    return {
        "version": APP_SETTINGS_VERSION,
        "kind": APP_SETTINGS_KIND,
        "theme": {"defaultThemeId": None, "userThemes": []},
    }


# ---------------------------------------------------------------------------
# The theme library
# ---------------------------------------------------------------------------

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


# The scene record's menus. Mirror SCENE_PATTERNS, SCENE_PITCHES and
# SELECTION_EDGES in frontend/src/lib/themes.ts (a test pins the parity).
SCENE_PATTERNS = ("none", "grid", "dots", "fine-dots", "scanlines")
SCENE_PITCHES = (16, 20, 24)
SELECTION_EDGES = (1, 2)

_SCENE_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _scene_color(value) -> bool:
    return isinstance(value, str) and _SCENE_COLOR.fullmatch(value) is not None


def _unit(value) -> float | None:
    """A finite number clamped to 0..1, else None (booleans are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # a JSON integer too large for a float
        return None
    if not math.isfinite(number):
        return None
    return min(1.0, max(0.0, number))


def _menu(value, options) -> bool:
    return not isinstance(value, bool) and value in options


_DROP = object()


def _validate_glow(value):
    """``None`` (no glow) or ``{"color": "#rrggbb", "alpha": 0..1}``; anything
    else is ``_DROP``."""
    if value is None:
        return None
    if isinstance(value, dict) and _scene_color(value.get("color")):
        alpha = _unit(value.get("alpha"))
        if alpha is not None:
            return {"color": value["color"], "alpha": alpha}
    return _DROP


def _validate_scene(value) -> dict:
    """A user theme's scene overrides, the fields of the frontend's
    ``ThemeScene`` it validly sets: ``base`` (``#rrggbb`` or ``None``, the
    canvas), ``pattern``, ``ink`` (``#rrggbb``), ``pitch`` and ``edge`` from
    their menus, ``strength`` and ``vignette`` clamped to 0..1, the two
    glows, the two reach options ``behindTopBar`` and ``behindRemoved``
    (booleans), and ``image`` (the CAS hash of an uploaded picture, or
    ``None`` for none; see ``admit_scene_images`` for its bytes). Every
    other key or value is dropped, as ``normalizeScene``
    (frontend/src/lib/userThemes.ts) drops it."""
    if not isinstance(value, dict):
        return {}
    scene: dict = {}
    if "base" in value and (value["base"] is None or _scene_color(value["base"])):
        scene["base"] = value["base"]
    if _menu(value.get("pattern"), SCENE_PATTERNS):
        scene["pattern"] = value["pattern"]
    if _scene_color(value.get("ink")):
        scene["ink"] = value["ink"]
    if _menu(value.get("pitch"), SCENE_PITCHES):
        scene["pitch"] = value["pitch"]
    if _menu(value.get("edge"), SELECTION_EDGES):
        scene["edge"] = value["edge"]
    for key in ("strength", "vignette"):
        unit = _unit(value.get(key))
        if unit is not None:
            scene[key] = unit
    for key in ("glowWarm", "glowCool"):
        if key in value:
            glow = _validate_glow(value[key])
            if glow is not _DROP:
                scene[key] = glow
    for key in ("behindTopBar", "behindRemoved"):
        if isinstance(value.get(key), bool):
            scene[key] = value[key]
    if "image" in value and (value["image"] is None or is_cas_hash(value["image"])):
        scene["image"] = value["image"]
    return scene


def validate_theme_library(value) -> dict:
    """The ``theme`` section. ``defaultThemeId`` is a non-blank string,
    stripped, else None. ``userThemes`` entries are the frontend's
    ``UserTheme`` objects: a dict with a string ``user.*`` id and
    string-to-string ``vars``. Any other entry is dropped; a kept entry's
    ``vars`` keep only the skin record's keys (``SKIN_VAR_KEYS``), its
    ``scene`` only its valid overrides (``_validate_scene``), and its other
    fields are kept as sent."""
    default = None
    themes: list[dict] = []
    if isinstance(value, dict):
        raw_default = value.get("defaultThemeId")
        if isinstance(raw_default, str) and raw_default.strip():
            default = raw_default.strip()
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
                    themes.append({**t, "vars": vars_, "scene": _validate_scene(t.get("scene"))})
    return {"defaultThemeId": default, "userThemes": themes}


_SECTION_VALIDATORS = {
    "theme": validate_theme_library,
}


def scene_image_hashes(doc) -> list[str]:
    """The CAS hashes the user themes' scenes of a settings document name as
    their pictures (``theme.userThemes[].scene.image``), in order; strings
    only, whatever their shape (the collector checks them)."""
    found: list[str] = []
    theme = doc.get("theme") if isinstance(doc, dict) else None
    themes = theme.get("userThemes") if isinstance(theme, dict) else None
    for entry in themes if isinstance(themes, list) else []:
        scene = entry.get("scene") if isinstance(entry, dict) else None
        image = scene.get("image") if isinstance(scene, dict) else None
        if isinstance(image, str):
            found.append(image)
    return found


def admit_scene_images(doc: dict) -> None:
    """Admit each scene picture of ``doc``'s user themes as a live reference
    in the installation's store before the document is written
    (``cas.retain_blob``: the upload's publication age restarts, so the
    collector leaves it), and drop one whose bytes the store no longer has,
    so the row never names a picture a reader cannot get."""
    from collimate_server.cas import app_store
    store = app_store()
    for entry in doc.get("theme", {}).get("userThemes", []):
        scene = entry.get("scene")
        if not isinstance(scene, dict) or not isinstance(scene.get("image"), str):
            continue
        try:
            store.retain_blob(scene["image"])
        except OSError as exc:
            logger.warning("user theme %s: its scene picture %s is not in the store (%s); dropped",
                           entry.get("id"), scene["image"], exc)
            del scene["image"]


# ---------------------------------------------------------------------------
# Load / merge
# ---------------------------------------------------------------------------

def _validate(raw) -> dict:
    """Normalize a stored document: each section through its validator, a
    missing one defaulted; a row of another kind or shape reads as the
    defaults."""
    if not isinstance(raw, dict) or raw.get("kind", APP_SETTINGS_KIND) != APP_SETTINGS_KIND:
        return default_app_settings()
    out = {"version": APP_SETTINGS_VERSION, "kind": APP_SETTINGS_KIND}
    for section, validator in _SECTION_VALIDATORS.items():
        out[section] = validator(raw.get(section))
    return out


def load_app_settings() -> dict:
    """The server's settings document; the defaults while no row exists or
    the row cannot be read."""
    try:
        raw = read_global_json(APP_SETTINGS_KEY)
    except ValueError:  # a row that is not JSON
        logger.warning("app settings row is not JSON; reading the defaults")
        raw = None
    return _validate(raw)


def merge_app_settings(partial: dict) -> dict:
    """Replace each *provided* section wholesale; omitted sections are
    untouched. Returns the stored document. Raises ValueError on a non-dict
    body, another ``kind``, an unknown section or a provided section that is
    not an object (a stored one of any shape reads as its defaults, but a
    write never replaces the library with them)."""
    if not isinstance(partial, dict):
        raise ValueError("settings body must be a JSON object")
    if "kind" in partial and partial["kind"] != APP_SETTINGS_KIND:
        raise ValueError(f"not an app-settings document (kind={partial['kind']!r})")
    unknown = [k for k in partial if k not in _SECTION_VALIDATORS and k not in ("version", "kind")]
    if unknown:
        raise ValueError(f"unknown settings section(s): {', '.join(sorted(unknown))}")
    malformed = [k for k in _SECTION_VALIDATORS if k in partial and not isinstance(partial[k], dict)]
    if malformed:
        raise ValueError(f"settings section(s) must be JSON objects: {', '.join(sorted(malformed))}")
    with _merge_lock:
        doc = load_app_settings()
        for section, validator in _SECTION_VALIDATORS.items():
            if section in partial:
                doc[section] = validator(partial[section])
        admit_scene_images(doc)
        write_global_json(APP_SETTINGS_KEY, doc)
        return doc
