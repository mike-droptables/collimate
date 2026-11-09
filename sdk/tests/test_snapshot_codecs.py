"""collimate._host.snapshot_codecs: the codec descriptors a renderer declares."""
from __future__ import annotations

import hashlib
import json

import pytest

from collimate._host.snapshot_codecs import declared, descriptor

_SOURCE = "self.onmessage=()=>{};"
_CODEC = {"id": "acme.notes", "version": 1, "implementationHash": hashlib.sha256(_SOURCE.encode()).hexdigest(),
          "source": _SOURCE}


def test_a_codec_descriptor_is_checked_and_named_by_its_implementation_hash():
    codec = descriptor("notes.codec.json", json.dumps(_CODEC))
    assert codec.public() == {"id": "acme.notes", "version": 1, "implementationHash": _CODEC["implementationHash"]}
    assert declared({"snapshot_codecs": ["notes.codec.json"]}, {"notes.codec.json": json.dumps(_CODEC)}.get) == [codec]
    assert declared({}, {}.get) == []
    with pytest.raises(ValueError, match="missing or not text"):
        declared({"snapshot_codecs": ["gone.codec.json"]}, {}.get)
    with pytest.raises(ValueError, match="declared twice"):
        declared({"snapshot_codecs": ["a.json", "b.json"]}, {"a.json": json.dumps(_CODEC), "b.json": json.dumps(_CODEC)}.get)


@pytest.mark.parametrize("change, message", [
    ({"implementationHash": "0" * 64}, "not the sha256 of its source"),
    ({"version": True}, "positive integer"),
    ({"id": "Notes"}, "lowercase name"),
    ({"source": ""}, "worker script"),
    ({"extra": 1}, "holds exactly"),
])
def test_a_malformed_codec_descriptor_is_refused(change, message):
    with pytest.raises(ValueError, match=message):
        descriptor("notes.codec.json", json.dumps({**_CODEC, **change}))


def test_the_shipped_milkdown_descriptor_is_a_valid_codec_of_the_runtimes_list():
    from pathlib import Path
    from collimate.shared import shared_path
    root = Path(__file__).resolve().parents[2]
    text = (root / "content/renderers/documents/milkdown/snapshot-codec.ts").read_text()
    body = text.split("export const milkdownSnapshotCodec = ", 1)[1].rstrip().rstrip(";")
    shipped = json.loads(Path(shared_path("renderer-runtime", "snapshot-codecs.json")).read_text())["codecs"]
    assert descriptor("milkdown.codec.json", body).public() in shipped


def test_collimate_lint_reports_a_missing_or_malformed_codec_descriptor(tmp_path):
    from collimate.lint import lint_renderer_manifest
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "main.tsx").write_text("export default {};\n")
    (folder / "view.renderer").write_text("id: notes\nname: Notes\nsummary: Notes\nentrypoint: main.tsx\n"
                                          "snapshot_codecs: [notes.codec.json]\n")

    def errors():
        return [issue.code for issue in lint_renderer_manifest(folder / "view.renderer") if issue.level.name == "ERROR"]
    assert errors() == ["snapshot-codec-missing"]
    (folder / "notes.codec.json").write_text(json.dumps({**_CODEC, "implementationHash": "0" * 64}))
    assert errors() == ["snapshot-codec"]
    (folder / "notes.codec.json").write_text(json.dumps(_CODEC))
    assert errors() == []
