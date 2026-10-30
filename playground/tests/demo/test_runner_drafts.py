"""The demo seed boots through the real app.

``channel.yaml``'s runner drafts travel in ``POST /api/channels`` with the
demo suggestion exactly as the shim sends them; the generators come from
``playground/demo/generators`` staged with the demo suggestion the way the
build vendors them into the staged api. Proves ``api/runners.py`` accepts every manifest
(``generator_index`` / ``generator_payload``), every draft saves, and every
runner resolves as runnable.
"""
from __future__ import annotations

import pytest
import yaml

from conftest import create_demo_channel
from fake_demo import DEMO_DIR, GENERATORS_DIR, demo_generators

SEED = yaml.safe_load((DEMO_DIR / "channel.yaml").read_text())
EXPECTED = set(demo_generators())


@pytest.fixture
def client(app_client):
    return app_client(GENERATORS_DIR)


@pytest.fixture
def seed(client, monkeypatch):
    """``seed()`` runs boot.seed_channel against ``client``, the shim's
    in-process requests sent through the TestClient. ``seed.calls`` records
    every ``(method, path, json_body)``, ``seed.log`` every boot log line;
    the busy retries pause for hundredths of a second."""
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace

    shim = Path(__file__).resolve().parents[2] / "shim"
    monkeypatch.syspath_prepend(str(shim))
    from collimate_playground import boot

    async def request(_app, method, path, *, query="", json_body=None, body=b""):
        seed.calls.append((method, path, json_body))
        url = path + ("?" + query if query else "")
        response = (client.request(method, url, json=json_body) if json_body is not None
                    else client.request(method, url, content=body))
        return SimpleNamespace(status=response.status_code, text=response.text, json=response.json)

    monkeypatch.setattr(boot.bridge, "request", request)
    monkeypatch.setattr(boot, "CHANNEL_BUSY_PAUSES", (0.01, 0.02, 0.03, 0.04))

    def seed():
        return asyncio.run(boot.seed_channel(client.app, str(DEMO_DIR), seed.log.append))

    seed.calls, seed.log, seed.boot = [], [], boot
    return seed


def _verifier_answers(monkeypatch, *refusals: str):
    """The app's verifier refuses its first calls with ``refusals``, in
    order, then verifies for real."""
    import verification
    real, pending = verification.verify_source, list(refusals)

    def verify_source(*args, **kwargs):
        if pending:
            return {"valid": False, "error": pending.pop(0)}
        return real(*args, **kwargs)

    monkeypatch.setattr(verification, "verify_source", verify_source)


def _creations(seed) -> int:
    return sum(1 for method, path, _ in seed.calls if (method, path) == ("POST", "/api/channels"))


def test_seed_pins_through_each_cells_meta_on_the_explicit_channel__F0287(seed):
    result = seed()
    assert set(result) == {"id", "name", "cells"}
    puts = [(path, body) for method, path, body in seed.calls if method == "PATCH" and path.endswith("/meta")]
    assert puts
    assert {path for path, _body in puts} == {
        f"/api/channels/{result['id']}/cells/{cell['cell_id']}/meta" for cell in result["cells"]}
    assert all(set(body) == {"pinned"} for _path, body in puts)


def test_channel_creation_retries_while_the_verifier_is_busy(seed, monkeypatch):
    from state import state
    busy = seed.boot.BUSY
    _verifier_answers(monkeypatch, busy, busy)

    result = seed()
    assert _creations(seed) == 3
    (channel,) = state.channels
    assert result["id"] == str(channel.id)
    assert [c["name"] for c in result["cells"]] == [c["name"] for c in SEED["cells"]]
    assert [line for line in seed.log if "verifier busy" in line] == [
        "channel creation: verifier busy (attempt 1 of 5); retrying in 0.01 s",
        "channel creation: verifier busy (attempt 2 of 5); retrying in 0.02 s",
    ]


def test_boot_fails_clearly_once_the_busy_retries_are_spent(seed, monkeypatch):
    from state import state
    busy = seed.boot.BUSY
    _verifier_answers(monkeypatch, *[busy] * 5)

    with pytest.raises(RuntimeError) as failed:
        seed()
    assert str(failed.value) == (
        "channel creation failed after 5 attempts, the verifier stayed busy: "
        'HTTP 400 {"detail":"Verifier busy, try again (deadline exceeded)"}')
    assert _creations(seed) == 5
    assert len([line for line in seed.log if "verifier busy" in line]) == 4
    assert state.channels == []
    assert not any("/import" in path for _method, path, _body in seed.calls)


def test_a_refusal_other_than_busy_fails_channel_creation_at_once(seed, monkeypatch):
    from state import state
    _verifier_answers(monkeypatch, "accepted verifier refused package")

    with pytest.raises(RuntimeError, match=r'^channel creation failed: HTTP 400 .*accepted verifier refused package'):
        seed()
    assert _creations(seed) == 1
    assert not any("verifier busy" in line for line in seed.log)
    assert state.channels == []


def test_channel_boots_with_the_demo_runners__D04(client):
    import runners
    from cell_placement import capabilities_stack_cells
    from state import state

    created = create_demo_channel(client, SEED["name"], SEED["runners"])
    assert created.status_code == 200, created.text
    (channel,) = state.channels
    assert channel.name == "Demo"

    # The demo's generator cell sits on the hidden Capabilities stack, beside
    # the SDK's renderer cells.
    assert not any(s.stack_type == "capabilities" for s in channel.main_stacks)
    generator_cells = [c for c in capabilities_stack_cells(channel)
                       if any(f.name.endswith(".generator") for f in c.files)]
    assert [c.name for c in generator_cells] == ["Demo generators"]
    (cell,) = generator_cells
    assert cell.icon == "sparkles"
    names = {f.name for f in cell.files}
    for gen_id in EXPECTED:
        assert {f"{gen_id}/definition.generator", f"{gen_id}/timeline.py", f"{gen_id}/settings.set_yaml"} <= names
    assert not any(name.endswith(".pyc") for name in names)
    # The cell.yaml travels with the cell; it names no shared helper.
    assert "cell.yaml" in names and not any(name.startswith("_") for name in names)

    index = runners.generator_index(generator_cells)
    assert set(index) == EXPECTED
    for gen_id, gi in index.items():
        payload = runners.generator_payload(gi)
        assert payload["id"] == gen_id and payload["main_config"] == "settings.set_yaml"
        assert payload["configs"] == [{"id": "default", "name": "Default", "revision": payload["configs"][0]["revision"]}]
        assert {f["key"] for f in payload["fields"]} == set(payload["default"]["runner"]["settings"]["settings.set_yaml"])

    catalog = client.get("/api/runners")
    assert catalog.status_code == 200, catalog.text
    entries = {e["id"]: e for e in catalog.json()}
    assert set(entries) == {d["runner"]["id"] for d in SEED["runners"]} == EXPECTED
    for rid, entry in entries.items():
        assert "invalid" not in entry, (rid, entry.get("invalid"))
        assert entry["missing_required"] == [] and entry["invalid_options"] == []
        assert entry["icon"] == "sparkles" and entry["cell_id"] == str(cell.id)
        assert entry["generator"] == rid
        editor = client.get(f"/api/runners/{rid}")
        assert editor.status_code == 200, editor.text
        assert editor.json()["generator"]["id"] == rid
        resolved = runners.resolve_runner_run(channel, rid)
        assert resolved.generator_id == rid and resolved.main_config == "settings.set_yaml"
        draft = next(d["runner"] for d in SEED["runners"] if d["runner"]["id"] == rid)
        assert resolved.settings_overlay == draft["settings"]
        assert resolved.prompt == draft["prompt"]

    generators = client.get("/api/runner_generators")
    assert generators.status_code == 200, generators.text
    assert {g["id"] for g in generators.json()} == EXPECTED
    for g in generators.json():
        assert g["cell_id"] == str(cell.id)
        assert g["prompt_intent"] if g["id"] in ("chat-assistant", "modify-files") else not g.get("prompt_intent")
