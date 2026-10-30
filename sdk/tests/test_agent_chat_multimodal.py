# SPDX-License-Identifier: MIT
"""The Agent generator's multimodal chat wiring (``generators/agent/agent/agent.py``
on the chat surface, over ``_agent_shared.session.entry``).

The chat ``on_turn`` reads the files the user attached to the turn and hands
their bytes to the engine. This tests the pure helper that does that, against a
live bridge (no LLM).
"""
from __future__ import annotations

import types


from _agent_shared.session import entry as agent  # noqa: E402

_PNG = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_read_turn_attachments_reads_workspace_bytes(native_bridge):
    import gen

    gen.update_file("attachments/photo.png", _PNG)
    turn = types.SimpleNamespace(attachments=[
        {"path": "attachments/photo.png", "name": "photo.png", "media_type": "image/png"},
        {"path": "attachments/missing.png", "name": "missing.png", "media_type": "image/png"},
        "junk",
    ])
    got = agent._read_turn_attachments(turn)
    assert len(got) == 1
    assert got[0]["media_type"] == "image/png"
    assert got[0]["name"] == "photo.png"
    assert got[0]["data"] == _PNG
