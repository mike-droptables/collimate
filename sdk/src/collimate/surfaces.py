"""Generic served-file declarations shared by generators and hosts — public.

:func:`surface_path` validates a file a run serves and
:func:`surface_declaration` the run's inbox registration;
:func:`resolve_opens` reads a manifest's ``opens`` into the files a run
opens, the one reading the host (which creates them as starting
placeholders) and the run (which serves them) share.
"""
from __future__ import annotations

import re
from typing import Any

from collimate.pointers import is_run_path, run_label, run_card_path


def surface_path(value: str, *, folder: str) -> str:
    """A canonical cell path or a member of the declaring run's folder."""
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..", ".collrun") for part in value.split("/"))):
        raise ValueError("surface path must be a relative cell file path")
    if is_run_path(value) and (not run_label(value) or not value.startswith(folder + "/")):
        raise ValueError("surface path in a run folder must belong to this run")
    if run_label(value) and value == run_card_path(run_label(value)):
        raise ValueError("the host's run card cannot be a served surface")
    return value


def surface_declaration(path: str, *, folder: str, label: str) -> dict:
    """Validate the run's inbox registration without touching files:
    ``{"path", "label"}``."""
    path = surface_path(path, folder=folder)
    if not isinstance(label, str) or not label.strip():
        raise ValueError("surface label must be nonempty text")
    return {"path": path, "label": label}


_OPENS_TOKEN = re.compile(r"\{([^{}]*)\}")


def resolve_opens(manifest: dict, fields: list, values: dict, *, folder: str) -> list[str]:
    """The cell-relative paths a run of this generator opens, in declaration
    order: each ``opens`` entry whose ``if`` holds for the main-config
    ``values`` (evaluated as the host evaluates ``prompt_show_if``), with
    ``{run}`` replaced by the run ``folder`` (``_runs/<label>``) and
    ``{<key>}`` by that key's resolved value.

    ``fields`` is the main config's ``fields:`` list and ``values`` its
    resolved values. Raises ``ValueError`` for a malformed entry, a template
    naming a key with no text value, a path :func:`surface_path` refuses, or
    a run-folder path that does not end in a declared ``ui_files`` suffix.
    The host creates these files as starting placeholders; the generator
    derives its own surface paths from the same call, so the two never
    disagree.
    """
    from collimate.config_values import compute_visibility, matches, type_index
    entries = manifest.get("opens") or []
    if not isinstance(entries, list):
        raise ValueError("opens must be a list of {path, if?} mappings")
    fields = fields if isinstance(fields, list) else []
    values = values if isinstance(values, dict) else {}
    visible = compute_visibility(fields, values)
    types = type_index(fields)
    suffixes = [s for s in manifest.get("ui_files") or [] if isinstance(s, str)]
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("an opens entry must be a mapping with a path")
        cond = entry.get("if")
        if cond is not None and not matches(cond, values, visible, types):
            continue
        template = entry["path"]

        def substitute(match: Any) -> str:
            name = match.group(1)
            if name == "run":
                return folder
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"opens path {template!r} needs a value for {name!r}")
            return value.strip()

        path = surface_path(_OPENS_TOKEN.sub(substitute, template), folder=folder)
        if path.startswith(folder + "/") and not any(path.endswith(s) for s in suffixes):
            raise ValueError(f"opens path {path!r} is in the run folder but does not end "
                             "in a declared ui_files suffix")
        if path not in out:
            out.append(path)
    return out


__all__ = ["resolve_opens", "surface_declaration", "surface_path"]
