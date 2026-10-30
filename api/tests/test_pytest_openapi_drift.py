"""Contract drift: FastAPI's auto-generated /openapi.json must match a
committed snapshot.

The frontend and the collimate-sdk wheel both depend on specific API
shapes. An accidental rename or removal elsewhere silently breaks
them — the snapshot diff turns that silent break into an explicit PR
change that a reviewer must sign off on.

Workflow:
  * Legitimate API change → run
    ``UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/test_pytest_openapi_drift.py``
    to refresh the snapshot and commit it.
  * Accidental change → test fails with a diff of added/removed paths
    and methods.

The snapshot is deliberately coarse (paths + methods per operation + a
pruned schema set); we don't lock every description string, so prose
updates don't thrash the snapshot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).resolve().parent / "_snapshots" / "openapi.json"


def _summarize(openapi: dict) -> dict:
    """Reduce the full OpenAPI doc to the shape bits we care about.

    Keeping only paths + methods + operation param names + request
    body schema refs keeps the snapshot from bouncing on every
    docstring edit while still catching every shape-affecting change.
    """
    summary: dict = {"paths": {}, "components": {}}

    for path, methods in (openapi.get("paths") or {}).items():
        summary["paths"][path] = {}
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            param_names = sorted(
                p.get("name") for p in (op.get("parameters") or []) if p.get("name")
            )
            req_ref = None
            rb = op.get("requestBody")
            if isinstance(rb, dict):
                content = rb.get("content") or {}
                for _mt, spec in content.items():
                    schema = (spec or {}).get("schema") or {}
                    req_ref = schema.get("$ref") or (schema.get("type"))
                    break
            summary["paths"][path][method] = {
                "parameters": param_names,
                "requestBody": req_ref,
                # Intentionally not capturing responses — status codes
                # shift often and the frontend already asserts them in
                # e2e tests.
            }

    schemas = (openapi.get("components") or {}).get("schemas") or {}
    # Keep just the schema names + required-field sets; property details
    # are too noisy.
    for name, schema in schemas.items():
        summary["components"][name] = {
            "type": schema.get("type"),
            "required": sorted(schema.get("required") or []),
            "properties": sorted((schema.get("properties") or {}).keys()),
        }

    return summary


def test_openapi_snapshot_matches(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200, r.text
    live = _summarize(r.json())

    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        return

    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            f"missing {SNAPSHOT_PATH}; seed it with UPDATE_OPENAPI_SNAPSHOT=1 "
            "uv run pytest tests/test_pytest_openapi_drift.py and commit it"
        )

    committed = json.loads(SNAPSHOT_PATH.read_text())
    if live == committed:
        return

    # Nice diff on failure: show path/method adds + removes before
    # drowning the test output in schema property details.
    live_paths = {
        f"{m.upper()} {p}"
        for p, methods in live["paths"].items()
        for m in methods
    }
    old_paths = {
        f"{m.upper()} {p}"
        for p, methods in committed.get("paths", {}).items()
        for m in methods
    }
    added = sorted(live_paths - old_paths)
    removed = sorted(old_paths - live_paths)

    live_schemas = set(live["components"].keys())
    old_schemas = set(committed.get("components", {}).keys())
    added_s = sorted(live_schemas - old_schemas)
    removed_s = sorted(old_schemas - live_schemas)

    msg = [
        "OpenAPI contract drift detected.",
        f"  added paths:    {added or '-'}",
        f"  removed paths:  {removed or '-'}",
        f"  added schemas:  {added_s or '-'}",
        f"  removed schemas:{removed_s or '-'}",
        "",
        "If intentional: UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest "
        "tests/test_pytest_openapi_drift.py",
    ]
    assert live == committed, "\n".join(msg)
