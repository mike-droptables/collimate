"""Settings schema parsing, validation, and value application.

Generators declare a main config schema. Saved runners carry its complete
values; named generator configs populate editable runner definitions.
"""

import re

import yaml

# Shared schema validation helper.
from collimate.config_values import validate_config


def config_violations(
    rel: str, fields: list[dict], resolved: dict
) -> tuple[list[dict], list[dict]]:
    """The condition-aware run gate for one resolved ``.set_yaml``.

    Returns ``(missing_required, invalid_options)``. Each ``missing_required``
    entry is ``{file, key, label}`` (a VISIBLE, effectively-required field
    whose resolved value :func:`is_empty`); each ``invalid_options`` entry is
    ``{file, key, label, value}`` (a VISIBLE dropdown whose resolved value
    selects a currently-disabled option). Both come from ONE SDK
    ``validate_config`` call, so evaluation is visibility-first: a field
    hidden by an unmet ``show_if`` never blocks and never reports an option
    violation, regardless of ``required``/``required_if`` or a stale overlay
    value. checkbox is never empty, so a (lint-WARNed) required checkbox can't
    block a run."""
    report = validate_config(fields, resolved)
    missing = [
        {"file": rel, "key": m["key"], "label": m.get("label") or m["key"]}
        for m in report["missing_required"]
    ]
    invalid = [
        {
            "file": rel,
            "key": o["key"],
            "label": o.get("label") or o["key"],
            "value": o.get("value"),
        }
        for o in report["invalid_options"]
    ]
    return missing, invalid


def overlay_set_yaml_values(text: str, overlay: dict) -> str:
    """Merge ``overlay`` into the file's top-level ``values:`` block and
    splice the merged block back in, leaving every other line (comments
    included) untouched. Raises ``ValueError`` when the document can't
    be spliced safely."""
    if not overlay:
        return text
    try:
        parsed = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise ValueError(f"unparseable .set_yaml: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError(".set_yaml is not a mapping document")
    existing = parsed.get("values")
    existing = existing if isinstance(existing, dict) else {}
    merged = {**existing, **overlay}
    return splice_top_level_yaml_block(text, "values", merged)


def splice_top_level_yaml_block(text: str, key: str, value) -> str:
    """Replace (or append) the top-level ``key:`` block of a YAML doc by
    line-splicing so every line outside the block — comments included —
    survives byte-for-byte.

    Safety reparse: the result must parse back equal to the original on
    every OTHER top-level key and carry exactly ``value`` under ``key``,
    else ``ValueError`` — callers map that to a 500 and persist nothing.
    """
    try:
        old_doc = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise ValueError(f"unparseable document: {exc}") from exc
    if old_doc is None:
        old_doc = {}
    if not isinstance(old_doc, dict):
        raise ValueError("document is not a top-level mapping")

    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^{re.escape(key)}\s*:")
    start = end = None
    for i, ln in enumerate(lines):
        if key_re.match(ln):
            start = i
            end = i + 1
            # The block runs through every following line that is blank
            # or indented; a non-blank column-0 line (incl. comments)
            # belongs to whatever comes next.
            while end < len(lines):
                nxt = lines[end]
                if nxt.strip() == "" or nxt[:1] in (" ", "\t"):
                    end += 1
                else:
                    break
            # Leave trailing blank separator lines in place.
            while end > start + 1 and lines[end - 1].strip() == "":
                end -= 1
            break

    block = yaml.safe_dump(
        {key: value}, sort_keys=False, default_flow_style=False, allow_unicode=True,
    )
    if start is None:
        new_lines = list(lines)
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(block)
    else:
        new_lines = lines[:start] + [block] + lines[end:]
    new_text = "".join(new_lines)

    try:
        new_doc = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"splice produced unparseable YAML: {exc}") from exc
    if not isinstance(new_doc, dict):
        raise ValueError("splice produced a non-mapping document")
    if {k: v for k, v in old_doc.items() if k != key} != \
            {k: v for k, v in new_doc.items() if k != key}:
        raise ValueError("splice altered unrelated top-level keys")
    if new_doc.get(key) != value:
        raise ValueError(f"spliced {key!r} block did not round-trip")
    return new_text
