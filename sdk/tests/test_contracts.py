# SPDX-License-Identifier: MIT
"""The channel SSE records' projections and the IPC op table's are generated,
byte for byte (``scripts/sync_contracts.py``): the JSON Schema in
``shared/contracts/``, the app's TypeScript union and ``ipc-ops.json``. The
fixture the dispatcher tests replay conforms to the schema."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from collimate._host.channel_events import EVENT_TYPES
from collimate._host.ipc import OPS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_contracts.py"
FIXTURE = ROOT / "shared" / "contracts" / "channel-events.json"


def _sync():
    spec = importlib.util.spec_from_file_location("sync_contracts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_check_passes():
    checked = subprocess.run([sys.executable, str(SCRIPT), "--check"],
                             capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_every_projection_equals_what_the_records_generate():
    sync = _sync()
    stale = [str(path.relative_to(sync.WORKSPACE)) for path, text in sync.expected().items()
             if not path.is_file() or path.read_text(encoding="utf-8") != text]
    assert stale == [], f"run {sync.COMMAND}: " + ", ".join(stale)


def test_every_record_is_an_interface_with_its_fields_in_order():
    sync = _sync()
    interfaces = {name: re.findall(r"(?m)^  (\w+): ", body) for name, body in
                  re.findall(r"(?ms)^export interface (\w+) \{\n(.*?)^\}", sync.typescript_source())}
    union = re.search(r"(?s)export type ChannelEvent =(.*?);", sync.typescript_source()).group(1)
    assert re.findall(r"\| (\w+)", union) == [cls.__name__ for cls in EVENT_TYPES.values()]
    for tag, cls in EVENT_TYPES.items():
        assert interfaces[cls.__name__] == ["type", *(f.name for f in fields(cls))], tag
        assert f"type: '{tag}';" in sync.typescript_source()


def test_the_op_table_projection_is_the_ops_table_in_order():
    sync = _sync()
    assert sync.OPS_JSON == ROOT / "shared" / "contracts" / "ipc-ops.json"
    table = json.loads(sync.ops_source())
    assert table["ops"] == [{"name": name, "kind": spec.kind, "capability": spec.capability}
                            for name, spec in OPS.items()]
    assert {op["kind"] for op in table["ops"]} == {"request", "notify"}


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "object": dict, "array": list, "null": type(None)}


def _conforms(value, rule: dict, defs: dict) -> bool:
    if "$ref" in rule:
        return _conforms(value, defs[rule["$ref"].rsplit("/", 1)[1]], defs)
    if "anyOf" in rule:
        return any(_conforms(value, option, defs) for option in rule["anyOf"])
    if "const" in rule:
        return value == rule["const"]
    kinds = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
    if not any(isinstance(value, _JSON_TYPES[kind])
               and not (isinstance(value, bool) and kind in ("integer", "number")) for kind in kinds):
        return False
    if isinstance(value, list):
        return all(_conforms(item, rule["items"], defs) for item in value)
    if isinstance(value, dict) and "properties" in rule:
        return set(value) == set(rule["required"]) and all(
            _conforms(value[key], option, defs) for key, option in rule["properties"].items())
    return True


def test_the_fixture_conforms_to_the_schema__F0308():
    schema = json.loads(_sync().schema_source())
    defs = schema["$defs"]
    assert [ref["$ref"].rsplit("/", 1)[1] for ref in schema["oneOf"]] == [
        cls.__name__ for cls in EVENT_TYPES.values()]
    for sample in json.loads(FIXTURE.read_text(encoding="utf-8")):
        record = EVENT_TYPES[sample["type"]].__name__
        assert _conforms(sample, defs[record], defs), sample["type"]
    # The checker refuses what the schema refuses.
    assert not _conforms({"type": "annotations_changed", "cell_id": "c", "context_id": "x", "rev": "1"},
                         defs["AnnotationsChanged"], defs)
    assert not _conforms({"type": "connected", "extra": 1}, defs["Connected"], defs)


def test_a_check_reports_a_hand_edit(tmp_path, monkeypatch):
    sync = _sync()
    target = tmp_path / "frontend" / "channelEvents.ts"
    target.parent.mkdir()
    target.write_text(sync.typescript_source().replace("rev: number", "rev: string"))
    monkeypatch.setattr(sync, "WORKSPACE", tmp_path)
    monkeypatch.setattr(sync, "TS", target)
    monkeypatch.setattr(sync, "SCHEMA", tmp_path / "missing.schema.json")
    monkeypatch.setattr(sync, "OPS_JSON", tmp_path / "missing-ops.json")
    assert sync.main(["--check"]) == 1


def test_a_type_table_entry_must_name_a_field(monkeypatch):
    sync = _sync()
    monkeypatch.setitem(sync.TS_TYPES, ("RunStatus", "verdict"), "RunOutcome")
    with pytest.raises(SystemExit, match="verdict"):
        sync.typescript_source()
