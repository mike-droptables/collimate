# SPDX-License-Identifier: MIT
"""Model file authorization for one run: what the agent's write tools may
write, and the live mirror's skip globs."""
from __future__ import annotations

import posixpath

import gen
from collimate.pointers import is_run_path, run_card_path, run_label

AGENT_LIVE_SKIP_GLOBS = ("_agent", "_agent/**", ".pydantic-ai-harness", ".pydantic-ai-harness/**")


def _norm(path: str) -> str:
    return posixpath.normpath(str(path or "").replace("\\", "/")).lstrip("/")


def write_refusal(path: str) -> str | None:
    """Why the model may not write ``path``, or ``None`` when it may: every
    path under ``_runs/`` is refused — this run's card is the host's, its
    own folder holds only its interface files, and another run's folder is
    read-only."""
    p = _norm(path)
    if not is_run_path(p):
        return None
    info = gen.run_info()
    if p == run_card_path(info.label):
        return f"{p} is this run's card: the host writes it"
    if p.startswith(info.folder + "/"):
        return f"{p} is in your interface folder; write task output outside {info.folder}/"
    return f"{p} belongs to run {run_label(p)} and is read-only"


def write_file(path: str, content: str | bytes, *, replace_revision: int | None = None) -> str:
    """Stage the model's write of ``path``. ``replace_revision`` is its
    acknowledged second attempt over the newer revision a conflict refusal
    named (``gen.update_file``); ``ValueError`` when no refusal of ``path``
    named that revision."""
    p = _norm(path)
    reason = write_refusal(p)
    if reason:
        raise PermissionError(reason)
    gen.update_file(p, content, replace_revision=replace_revision)
    return p
