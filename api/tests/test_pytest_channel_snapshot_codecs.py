"""Snapshot codecs are trusted per channel, by admission.

A renderer declares its codec descriptors under ``snapshot_codecs:``; once
the renderer is on a channel's Capabilities (Add to, or any other door), the
list the browser checks ``registerSnapshotCodec`` against for that channel's
pages (``GET /api/channels/{ch}/snapshot-codecs``) names its codec after the
SDK runtime's own. Another channel's list does not. Nothing is stored.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from collimate_server.models import Cell, Stack, VirtualFile

SOURCE = "self.onmessage=e=>self.postMessage({type:'materialized',requestId:e.data.requestId,body:String(e.data.payload)});"
DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()
CODEC = {"id": "acme.notes", "version": 1, "implementationHash": DIGEST, "source": SOURCE}


def _renderer(codec: dict, renderer_id: str = "acme-notes") -> list:
    return [
        VirtualFile(name="notes/view.renderer", content=(
            f"id: {renderer_id}\nname: Acme notes\nsummary: Edits Acme notes\nentrypoint: main.tsx\n"
            "embed: native\nextensions: [.acmenote]\nsnapshot_codecs: [notes.codec.json]\n")),
        VirtualFile(name="notes/main.tsx", content=(
            "import { defineRenderer } from '/api/sdk/react.js';\nimport codec from './notes.codec.json';\n"
            "export default defineRenderer({ component: () => codec.id });\n")),
        VirtualFile(name="notes/notes.codec.json", content=json.dumps(codec)),
    ]


def _add(client, files):
    """Add to Capabilities from a work cell of the active channel."""
    from collimate_server.cell_commits import commit_source_cell
    from collimate_server.state import find_active_channel, state_lock
    channel = find_active_channel()
    cell = Cell(name="Work", files=files)
    with state_lock:
        channel.main_stacks.append(Stack(name="Work", cells=[cell]))
    commit_source_cell(channel, cell, cell.files, persist=False)
    request = {"source_cell_id": str(cell.id), "definition_path": "notes/view.renderer", "target_cell_id": None}
    preview = client.post(f"/api/channels/{channel.id}/packages/preview", json=request)
    if preview.status_code != 200:
        return preview, None
    added = client.post(f"/api/channels/{channel.id}/packages/add", json=request | {
        "expected_content_hash": preview.json()["content_hash"],
        "expected_target_revision": preview.json()["target_revision"]})
    return preview, added


def _trusted(client, channel_id: str) -> list[dict]:
    answer = client.get(f"/api/channels/{channel_id}/snapshot-codecs")
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store", answer.text
    return answer.json()["codecs"]


def _shipped() -> list[dict]:
    from collimate.shared import shared_path
    return json.loads(open(shared_path("renderer-runtime", "snapshot-codecs.json")).read())["codecs"]


def test_every_channel_trusts_the_sdk_runtimes_codecs(client):
    shipped = _shipped()
    assert any(codec["id"] == "collimate.milkdown-pm" for codec in shipped)
    assert _trusted(client, client.channel_id) == shipped


def test_a_renderer_added_to_one_channel_brings_its_codec_there_and_not_to_another(client):
    preview, added = _add(client, _renderer(CODEC))
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"]["snapshot_codecs"] == [f"acme.notes v1 ({DIGEST})"]
    assert added.status_code == 200, added.text
    first = client.channel_id
    assert _trusted(client, first) == _shipped() + [{"id": "acme.notes", "version": 1, "implementationHash": DIGEST}]

    other = client.post("/api/channels", params={"name": "Other"})
    assert other.status_code == 200, other.text
    assert _trusted(client, other.json()["id"]) == _shipped()
    # The first channel keeps its own list.
    assert DIGEST in {codec["implementationHash"] for codec in _trusted(client, first)}


def test_a_renderer_that_reached_capabilities_another_way_brings_its_codec_too(client):
    from conftest import add_capabilities_cell
    from collimate_server.state import require_channel
    add_capabilities_cell(require_channel(client.channel_id), Cell(name="Notes", files=_renderer(CODEC)))
    assert DIGEST in {codec["implementationHash"] for codec in _trusted(client, client.channel_id)}


def test_the_codec_leaves_with_its_renderer(client):
    from conftest import add_capabilities_cell
    from collimate_server.cell_placement import capabilities_stack
    from collimate_server.state import require_channel
    channel = require_channel(client.channel_id)
    cell = Cell(name="Notes", files=_renderer(CODEC))
    add_capabilities_cell(channel, cell)
    assert DIGEST in {codec["implementationHash"] for codec in _trusted(client, client.channel_id)}
    capabilities_stack(channel).cells.remove(cell)
    assert _trusted(client, client.channel_id) == _shipped()


@pytest.mark.parametrize("change, detail", [
    ({"implementationHash": "0" * 64}, "implementationHash is not the sha256 of its source"),
    ({"version": 0}, "version is a positive integer"),
    ({"id": "Acme Notes"}, "id is a lowercase name"),
])
def test_a_malformed_codec_refuses_the_add(client, change, detail):
    preview, _ = _add(client, _renderer({**CODEC, **change}))
    assert preview.status_code == 400 and detail in preview.json()["detail"], preview.text


def test_a_malformed_codec_on_capabilities_is_not_trusted(client):
    from conftest import add_capabilities_cell
    from collimate_server.state import require_channel
    add_capabilities_cell(require_channel(client.channel_id),
                          Cell(name="Notes", files=_renderer({**CODEC, "implementationHash": "0" * 64})))
    assert _trusted(client, client.channel_id) == _shipped()


def test_an_unknown_channel_is_404(client):
    assert client.get("/api/channels/00000000-0000-4000-8000-000000000000/snapshot-codecs").status_code == 404
