# SPDX-License-Identifier: MIT
"""LIVE multimodal input — attachment bytes really reaching a model's eyes.

The unit suite proves the SHAPES (``BinaryContent`` parts, anthropic image
blocks, openai ``input_image``); only a live model can prove the bytes survive
the trip. Every assertion here is "the model named the colour", which is
unfakeable by construction: the colour exists only in bytes this file builds.

The intake under test is the **chat-turn** path — ``turn.attachments`` (the
cell-relative refs the host flushes into the workspace before the turn) →
``_read_turn_attachments`` → the engine. It is the only way a user hands a
model bytes: a session's turns carry the attachments, and the engine's own
``attachments=`` argument (the composition caller's door) is covered where
each backend renders it.

Requires: a provider key AND a configured vision model (see
``conftest._MULTIMODAL_DEFAULT``); providers with neither skip with the reason
naming which is missing. ``groq`` has no vision model in the default lineup, so
its rows skip until ``COLLIMATE_LIVE_MULTIMODAL_MODEL_GROQ`` names one.

Cost: about four image turns per vision-capable provider. A 64x64 solid square
is ~100 image tokens, so these are among the cheapest turns in the suite —
seconds each.
"""
from __future__ import annotations

import pytest

from .conftest import (
    BLUE,
    GIF_PROVIDERS,
    RED,
    agent_config,
    assert_ran,
    assistant_replies,
    gif_bytes,
    model_account,
    multimodal_model_for,
    multimodal_params,
    png_bytes,
    run_chat_generator,
)

pytestmark = pytest.mark.live

_COLOUR_PROMPT = "What colour is the image? Reply with one word: the colour."


def _ref(name: str, media_type: str) -> dict:
    """One attachment ref as the chat renderer sends it — the cell path the
    host has already flushed the bytes to, plus the media type it recognised
    (``""`` when its vocabulary has no entry for the extension)."""
    return {"path": f"attachments/{name}", "name": name, "media_type": media_type}


def _vision_turn(provider: str, files: dict, refs: list, *,
                 prompt: str = _COLOUR_PROMPT):
    """One chat turn carrying ``refs``, with their bytes seeded where the host
    would have flushed them, against the provider's vision model."""
    return run_chat_generator(
        prompt="",
        inputs={f"attachments/{name}": data for name, data in files.items()},
        messages=[{"kind": "user_msg", "content": prompt, "attachments": refs}],
        account=model_account(provider, model=multimodal_model_for(provider)),
        config=agent_config(surface="chat"),
    )


def _only_reply(res, provider: str) -> str:
    """The turn's single assistant reply, with the recorded-error shape (which
    keeps ``res.ok`` True) ruled out first."""
    assert_ran(res, provider)
    replies = assistant_replies(res)
    assert len(replies) == 1, f"{provider}: {replies}"
    assert not replies[0].startswith("_(error:"), f"{provider}: {replies[0]}"
    return replies[0]


# ══════════════════════════════════════════════════════════════════════
# One image
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", multimodal_params())
def test_chat_turn_sees_a_red_png(provider):
    """Attachments arrive as cell-relative refs whose bytes the host has
    already flushed into the workspace, so the scripted message mirrors that
    exactly — a ref in the command, the bytes seeded as an input."""
    res = _vision_turn(provider, {"square.png": png_bytes(RED)},
                       [_ref("square.png", "image/png")])
    reply = _only_reply(res, provider)
    assert "red" in reply.lower(), f"{provider}: reply was {reply!r}"


# ══════════════════════════════════════════════════════════════════════
# Two images at once
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", multimodal_params())
def test_two_images_are_both_seen(provider):
    """Both attachments must survive — a backend that renders only the first
    part (or overwrites one with the other) still answers plausibly with one
    colour, so the assertion needs both names present."""
    res = _vision_turn(
        provider,
        {"a-square.png": png_bytes(RED), "b-square.png": png_bytes(BLUE)},
        [_ref("a-square.png", "image/png"), _ref("b-square.png", "image/png")],
        prompt=("You are given two images, each a solid colour. Name both "
                "colours. Reply with just the two colour words separated by a "
                "space."),
    )
    reply = _only_reply(res, provider).lower()
    assert "red" in reply and "blue" in reply, f"{provider}: reply was {reply!r}"


# ══════════════════════════════════════════════════════════════════════
# Format variance — GIF alongside PNG
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", multimodal_params(sorted(GIF_PROVIDERS)))
def test_gif_attachment_is_seen(provider):
    """A second on-the-wire image format, hand-rolled like the PNG.

    JPEG is deliberately absent: this tree ships no imaging dependency and no
    binary fixtures, and a hand-written baseline JPEG encoder would be far more
    code than the thing it tests. GIF87a is a few dozen bytes of header plus a
    trivially-degenerate LZW stream, so it buys format variance honestly.
    Restricted to the providers documented to accept ``image/gif`` — Gemini's
    inline-data vocabulary, for one, does not include it."""
    res = _vision_turn(provider, {"square.gif": gif_bytes(BLUE)},
                       [_ref("square.gif", "image/gif")])
    reply = _only_reply(res, provider)
    assert "blue" in reply.lower(), f"{provider}: reply was {reply!r}"


# ══════════════════════════════════════════════════════════════════════
# Non-media files are dropped, not fatal
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", multimodal_params())
def test_a_typeless_attachment_beside_an_image_is_dropped_not_fatal(provider):
    """A turn that carries a file the host could not type must still produce a
    normal image turn.

    The drop happens in the engine's shared normalizer (``iter_attachments``
    skips an entry with no media type), so the archive never reaches a provider
    that would reject the type outright. The image beside it is the evidence:
    an attachment list that faulted on the archive would take the picture down
    with it."""
    res = _vision_turn(
        provider,
        {
            "square.png": png_bytes(RED),
            # A minimal empty-archive: "PK\x05\x06" + 18 zero bytes is a valid
            # zip end-of-central-directory record, i.e. a real (empty) archive.
            "bundle.zip": b"PK\x05\x06" + b"\x00" * 18,
        },
        [_ref("square.png", "image/png"), _ref("bundle.zip", "")],
    )
    reply = _only_reply(res, provider)
    assert "red" in reply.lower(), f"{provider}: reply was {reply!r}"
