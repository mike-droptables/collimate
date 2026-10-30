"""A built-in handler's regenerated skills, offered from a work cell
(``POST /api/channels/{channel}/builtin/handlers/{id}/skills``).

A regenerate work stack (``capability_spawn``) writes the skills; the offer
takes only the non-pinned files under ``<folder>/derived/skills/`` of a
standard cell into the Definitions card's overlay, where they wait for
review. Inspect runs every example through the build's verifier; Accept
records the channel-owned (non-pinned) skills hash in ``additions.yaml``, so
an upgrade that changes a pinned file keeps it accepted; ``DELETE …/skills``
returns to the build's skills. ``verify.ts``, asset folders, references and
the ``handler`` built-in's own briefs are pinned.
"""
from __future__ import annotations

import uuid

import pytest

SKILL = "generator/derived/skills/generator-authoring/SKILL.md"
REFERENCE = "generator/derived/skills/generator-authoring/references/interface.md"
BETTER = "---\nname: generator-authoring\ndescription: Regenerated.\n---\n# Better\n"


def _channel(client):
    from state import require_channel
    return require_channel(client.channel_id)


def _card(client):
    from builtin_packages import handler_card
    return handler_card(_channel(client))


def _record(client, handler="generator"):
    return next(h for h in client.get("/api/handlers").json() if h["builtin"] and h["id"] == handler)


def _overlay(client) -> dict[str, bytes]:
    from package_approval import snapshot_cell
    return snapshot_cell(_channel(client), _card(client)).bytes()


def _work_cell(client, files: dict[str, str]) -> str:
    """A new standard cell holding ``files``; answers its id."""
    response = client.post(f"/api/stacks/{client.channel_id}/create")
    assert response.status_code == 200, response.text
    cell_id = response.json()["main_stacks"][-1]["cells"][0]["id"]
    mutations = [dict(path=path, writer_id="tab:1", edit_seq=1, base_revision=0, kind="put", content_text=text)
                 for path, text in files.items()]
    put = client.post(f"/api/channels/{client.channel_id}/cells/{cell_id}/files/mutate",
                      json={"operation_id": str(uuid.uuid4()), "mutations": mutations})
    assert put.status_code == 200, put.text
    return cell_id


def _offer(client, cell_id, *, handler="generator", folder="generator"):
    return client.post(f"/api/channels/{client.channel_id}/builtin/handlers/{handler}/skills",
                       json={"source_cell_id": str(cell_id), "folder": folder})


def _inspect(client, handler="generator"):
    response = client.post(f"/api/channels/{client.channel_id}/cells/{_card(client)}/handler-build/inspect",
                           json={"folder": handler})
    assert response.status_code == 200, response.text
    return response.json()


def _accept(client, handler="generator"):
    inspection = _inspect(client, handler)
    response = client.post(f"/api/channels/{client.channel_id}/cells/{_card(client)}/handler-build/accept",
                           json={"folder": handler, "expected_content_hash": inspection["content_hash"]})
    assert response.status_code == 200, response.text
    return inspection


@pytest.mark.parametrize("handler", ["generator", "handler", "options", "renderer", "runner", "git-checkout"])
def test_inspect_passes_every_shipped_example_of_a_builtin(client, handler):
    """Each built-in's shipped examples meet their expectation through its
    own verifier (a ``.handler`` example's definition sees its nested
    examples, as a real package's would)."""
    from builtin_packages import builtin_handlers
    inspection = _inspect(client, handler)
    assert inspection["compile"] == {"valid": True, "error": None}
    shipped = {e["folder"] for e in builtin_handlers()[handler].manifest["examples"]}
    assert shipped and {e["folder"] for e in inspection["examples"]} == shipped
    for example in inspection["examples"]:
        assert example["error"] is None and example["verdicts"], example
        for verdict in example["verdicts"]:
            assert verdict["valid"] is (example["expect"] == "valid"), (example["folder"], verdict)


def test_an_offer_writes_only_the_owned_skills_and_waits_for_review(client):
    from builtin_packages import builtin_handlers
    vendored = builtin_handlers()["generator"].files
    cell = _work_cell(client, {SKILL: BETTER, REFERENCE: "# Rewritten reference\n",
                               "generator/derived/verify.ts": "export default () => ({valid:false});",
                               "generator/generator.handler": "id: generator\n", "notes.md": "Notes\n"})
    response = _offer(client, cell)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"handler"}
    overlay = _overlay(client)
    assert {path for path in overlay if path.startswith("generator/")} == {SKILL}
    assert overlay[SKILL] == BETTER.encode()
    record = _record(client)
    assert (record["has_derived"], record["accepted"], record["uses_app_skills"]) == (True, False, True)
    assert response.json()["handler"] == record
    inspection = _inspect(client)
    assert inspection["builtin"] is True and inspection["compile"]["valid"] is True
    assert inspection["derived_hash"] != builtin_handlers()["generator"].skills_hash
    assert all(v["valid"] for e in inspection["examples"] for v in e["verdicts"])
    _accept(client)
    record = _record(client)
    assert (record["accepted"], record["accepted_skills_hash"], record["uses_app_skills"]) == (
        True, inspection["derived_hash"], False)
    from handlers import catalog
    skills = catalog(_channel(client)).skills("generator")
    assert skills["generator--generator-authoring/SKILL.md"].endswith("# Better\n")
    assert skills["generator--generator-authoring/references/interface.md"] == \
        vendored["derived/skills/generator-authoring/references/interface.md"].decode()
    verify = next(r for r in catalog(_channel(client)).records if r.builtin
                  and r.prefix == "generator").accepted_files["verify.ts"]
    assert verify == vendored["derived/verify.ts"]


def test_a_new_offer_replaces_the_waiting_skills_whole(client):
    first = _work_cell(client, {SKILL: BETTER, "generator/derived/skills/extra/SKILL.md":
                                "---\nname: extra\ndescription: Extra.\n---\n# Extra\n"})
    assert _offer(client, first).status_code == 200
    second = _work_cell(client, {SKILL: BETTER})
    assert _offer(client, second).status_code == 200
    assert {path for path in _overlay(client) if path.startswith("generator/")} == {SKILL}


def test_an_offer_reads_the_named_folder(client):
    cell = _work_cell(client, {"work/derived/skills/generator-authoring/SKILL.md": BETTER})
    response = _offer(client, cell, folder="work/")
    assert response.status_code == 200, response.text
    assert _overlay(client)[SKILL] == BETTER.encode()


def test_offering_the_effective_skills_unchanged_is_not_pending(client):
    from builtin_packages import builtin_handlers, pinned
    vendored = builtin_handlers()["generator"].files
    owned = {"generator/" + path: body.decode() for path, body in vendored.items()
             if path.startswith("derived/skills/") and not pinned("generator", path)}
    assert _offer(client, _work_cell(client, owned)).status_code == 200
    record = _record(client)
    assert record["has_derived"] is True and record["accepted"] is True
    assert record["built_hash"] == record["accepted_skills_hash"]


def test_using_the_apps_skills_again_removes_the_regenerated_ones(client):
    from builtin_packages import builtin_handlers
    build_hash = builtin_handlers()["generator"].skills_hash
    assert _offer(client, _work_cell(client, {SKILL: BETTER})).status_code == 200
    _accept(client)
    revert = client.delete(f"/api/channels/{client.channel_id}/builtin/handlers/generator/skills")
    assert revert.status_code == 200, revert.text
    handler = revert.json()["handler"]
    assert (handler["has_derived"], handler["accepted_skills_hash"], handler["uses_app_skills"]) == (
        False, build_hash, True)
    assert not any(path.startswith("generator/derived/skills/") for path in _overlay(client))


def test_an_offer_comes_from_a_standard_cell_with_owned_skills(client):
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    from state import flush_persist_state, persist_state, state_lock
    capability = Cell(name="Held", files=[VirtualFile(name=SKILL, content=BETTER)])
    with state_lock:
        add_capabilities_cell(_channel(client), capability)
    persist_state()
    flush_persist_state()
    where = "Offer skills from the cell where they were written"
    for source in (capability.id, _card(client)):
        response = _offer(client, source)
        assert (response.status_code, response.json()["detail"]) == (400, where)
    pinned_only = _work_cell(client, {REFERENCE: "# Mine\n", "generator/derived/verify.ts": "x"})
    response = _offer(client, pinned_only)
    assert (response.status_code, response.json()["detail"]) == (
        400, "No regenerated skills under generator/derived/skills")
    assert _offer(client, uuid.uuid4()).status_code == 404
    assert _offer(client, pinned_only, handler="nope").status_code == 404
    machinery = _work_cell(client, {"handler/derived/skills/build/SKILL.md": "# Mine\n",
                                    "handler/derived/skills/create/SKILL.md": "# Mine\n"})
    response = _offer(client, machinery, handler="handler", folder="handler")
    assert response.status_code == 400
    assert not any(path.startswith("generator/") or path.startswith("handler/") for path in _overlay(client))


def test_an_offer_request_is_bounded(client):
    url = f"/api/channels/{client.channel_id}/builtin/handlers/generator/skills"
    for body in ({"source_cell_id": "x", "folder": "generator"}, {"source_cell_id": str(uuid.uuid4())},
                 {"source_cell_id": str(uuid.uuid4()), "folder": "generator", "extra": 1}):
        assert client.post(url, json=body).status_code == 422, body


def test_an_upgrade_changing_a_pinned_file_keeps_the_regeneration_accepted(client, monkeypatch):
    """The accepted hash covers only the channel-owned skills: a later build
    whose reference mirror changed still reports the regeneration accepted
    and serves the new reference, and the self-heal re-stores the accepted
    skills from the overlay when app.db lost them."""
    from dataclasses import replace
    from types import MappingProxyType
    import builtin_packages
    from database import get_global_db_connection
    from handlers import catalog, clear_catalog_cache
    assert _offer(client, _work_cell(client, {SKILL: BETTER})).status_code == 200
    accepted = _accept(client)["derived_hash"]
    reference = "derived/skills/generator-authoring/references/interface.md"
    real = builtin_packages.build()
    generator = real.handlers["generator"]
    files = dict(generator.files, **{reference: generator.files[reference] + b"\nRe-synced.\n"})
    handlers = dict(real.handlers, generator=replace(generator, files=MappingProxyType(files)))
    upgraded = replace(real, handlers=MappingProxyType(handlers))
    monkeypatch.setattr(builtin_packages, "build", lambda: upgraded)
    conn = get_global_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM handler_derived_files WHERE derived_hash=?", (accepted,))
    finally:
        conn.close()
    clear_catalog_cache()
    record = _record(client)
    assert (record["accepted"], record["built_hash"], record["accepted_skills_hash"]) == (True, accepted, accepted)
    skills = catalog(_channel(client)).skills("generator")
    assert skills["generator--generator-authoring/SKILL.md"].endswith("# Better\n")
    assert skills["generator--generator-authoring/references/interface.md"].endswith("Re-synced.\n")


def test_a_skill_folder_holding_assets_is_pinned_whole():
    """A built-in skill folder with an ``assets/`` folder is the build's
    bytes, its SKILL.md included."""
    from builtin_packages import _pinned
    assets = frozenset({"authoring"})
    for path in ("derived/skills/authoring/SKILL.md", "derived/skills/authoring/assets/template.json"):
        assert _pinned("x", assets, path), path
    assert not _pinned("x", assets, "derived/skills/other/SKILL.md")
