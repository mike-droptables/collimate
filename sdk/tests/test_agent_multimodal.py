# SPDX-License-Identifier: MIT
"""Multimodal I/O for the shared agent engine (``generators/_agent_shared``):
caller ``attachments`` as multimodal INPUT to the model. A model returns text
only: files reach the cell through the harness's own tools. Synthetic only — no
network, no on-disk generator names.
"""
from __future__ import annotations

import asyncio
import base64
import inspect


import _agent_shared as eng  # noqa: E402
from _agent_shared import agent_events  # noqa: E402


_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PDF = b"%PDF-1.4 minimal"
_MP3 = b"ID3 audio bytes"


def _atts():
    return [
        {"data": _PNG_1x1, "media_type": "image/png", "name": "shot.png"},
        {"data": _MP3, "media_type": "audio/mpeg", "name": "clip.mp3"},
        {"data": _PDF, "media_type": "application/pdf", "name": "doc.pdf"},
    ]


# ══════════════════════════════════════════════════════════════════════
# iter_attachments / binary_parts — normalize, skip-empty, oversize cap
# ══════════════════════════════════════════════════════════════════════


def test_iter_attachments_skips_empty_and_non_dict():
    out = list(eng.iter_attachments([
        {"data": _PNG_1x1, "media_type": "image/png", "name": "a.png"},
        {"data": b"", "media_type": "image/png"},        # no bytes
        {"data": _PDF, "media_type": ""},                # no media type
        "not-a-dict",
        {"media_type": "image/png"},                     # no data key
    ]))
    assert len(out) == 1
    data, mt, name = out[0]
    assert data == _PNG_1x1 and mt == "image/png" and name == "a.png"


def test_iter_attachments_names_and_str_bytes():
    # A str payload is utf-8 encoded; a missing name gets a positional default.
    out = list(eng.iter_attachments([{"data": "héllo", "media_type": "text/plain"}]))
    assert out[0][0] == "héllo".encode("utf-8")
    assert out[0][2] == "attachment-1"


def test_iter_attachments_drops_oversize(monkeypatch):
    monkeypatch.setattr(agent_events, "ATTACH_BYTE_CAP", 4)
    out = list(eng.iter_attachments([
        {"data": b"ok", "media_type": "text/plain", "name": "small"},
        {"data": b"way too big", "media_type": "text/plain", "name": "big"},
    ]))
    assert [n for _d, _m, n in out] == ["small"]


# ══════════════════════════════════════════════════════════════════════
# run_agent signature + dispatch plumbing
# ══════════════════════════════════════════════════════════════════════


def test_run_agent_signature_accepts_attachments_and_nothing_else_multimodal():
    params = inspect.signature(eng.run_agent).parameters
    assert "attachments" in params and params["attachments"].default is None
    assert "image_output" not in params and "exec_enabled" not in params


def test_run_agent_threads_attachments_to_the_acp_turn(host_free_run, monkeypatch):
    import _agent_shared.acp_run as acp_run

    async def _fake_turn(**kw):
        assert kw["attachments"] == _atts()
        assert "image_output" not in kw and "exec_enabled" not in kw
        return ("done", [], {})

    monkeypatch.setattr(acp_run, "run_acp_turn", _fake_turn)
    r = asyncio.run(eng.run_agent(prompt="p", backend="pydantic-ai", attachments=_atts()))
    assert r.text == "done" and not hasattr(r, "files")
