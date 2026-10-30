"""Coverage for the Capabilities manage view's backend.

Exercises the channel-scoped renderer availability config (a single on/off
switch per renderer) and the catalog enrichment the manage view reads (the
holding cell's id and the manifest facts). Generator-driven cell access is
governed only by the run's permission grant. Uses the self-contained
``install_test_generator`` / ``install_test_renderer`` helpers so nothing
depends on the SDK-seeded catalog.
"""
from __future__ import annotations

from conftest import install_test_generator


# ─── Catalog enrichment ──────────────────────────────────────────────────────

def test_generator_catalog_dedups_by_id(client):
    # Two Capabilities cells holding the same generator id → one catalog
    # entry, so the manage view never lists duplicate same-id generators.
    install_test_generator(name="dup_gen")
    install_test_generator(name="dup_gen")
    names = [g["name"] for g in client.get("/api/generators").json()]
    assert names.count("dup_gen") == 1


def test_generator_catalog_carries_cell_id_and_drops_availability(client, test_generator):
    body = client.get("/api/generators").json()
    entry = next(g for g in body if g["name"] == test_generator)
    assert entry.get("cell_id"), "generator entry missing cell_id"
    # Per-generator picker availability is gone (relays own picking): the catalog
    # entry no longer carries an available_ui (or a callability) flag.
    assert "available_ui" not in entry
    assert "available_generators" not in entry


def test_generator_catalog_carries_manifest_facts(client):
    # The Manage view lists each generator's version, entrypoint, UI files
    # and docker need read-only (polish).
    install_test_generator(
        name="facts_gen",
        manifest_extra='version: 1.2\nui_files: [".chat", ".json"]\nrequires_docker: true',
    )
    install_test_generator(name="bare_gen")
    body = {g["name"]: g for g in client.get("/api/generators").json()}
    facts = body["facts_gen"]
    assert facts["version"] == "1.2"
    assert facts["entrypoint"] == "facts_gen.py"
    assert facts["ui_files"] == [".chat", ".json"]
    assert facts["requires_docker"] is True
    bare = body["bare_gen"]
    assert bare["version"] is None
    assert bare["ui_files"] == []
    assert bare["requires_docker"] is False


def test_renderer_catalog_carries_cell_id_and_available(client, test_renderer):
    body = client.get("/api/renderers").json()
    entry = next(r for r in body if r["id"] == test_renderer)
    assert entry.get("cell_id"), "renderer entry missing cell_id"
    assert entry["available"] is True


# ─── Renderer availability (on / off) ────────────────────────────────────────

def test_renderer_disable_hides_from_catalog_but_not_include_disabled(client, test_renderer):
    client.put(f"/api/renderer_availability/{test_renderer}", json={"enabled": False})

    default_ids = {r["id"] for r in client.get("/api/renderers").json()}
    assert test_renderer not in default_ids, "disabled renderer leaked into default list"

    incl = client.get("/api/renderers?include_disabled=1").json()
    entry = next(r for r in incl if r["id"] == test_renderer)
    assert entry["available"] is False


def test_renderer_disable_404s_source(client, test_renderer):
    """A disabled renderer drops out of the source-serving path — resolution
    must not bind it."""
    from test_pytest_renderer_bundles import _publish
    source = next(r['content_hash'] for r in client.get('/api/renderers').json() if r['id'] == test_renderer)
    _publish(source, b'export default () => null;')
    assert client.get(f"/api/renderers/{test_renderer}/source").status_code == 200
    client.put(f"/api/renderer_availability/{test_renderer}", json={"enabled": False})
    assert client.get(f"/api/renderers/{test_renderer}/source").status_code == 404
    # Re-enabling restores it.
    client.put(f"/api/renderer_availability/{test_renderer}", json={"enabled": True})
    assert client.get(f"/api/renderers/{test_renderer}/source").status_code == 200


def test_retired_renderer_display_route_cannot_mutate_selected_files__D13(client, test_renderer):
    before = next(r for r in client.get("/api/renderers").json() if r["id"] == test_renderer)
    result = client.put(f"/api/renderers/{test_renderer}/display", json={"name": "Unapproved edit"})
    assert result.status_code in (404, 405)
    assert "/api/renderers/{renderer_id}/display" not in client.get("/openapi.json").json()["paths"]
    after = next(r for r in client.get("/api/renderers").json() if r["id"] == test_renderer)
    assert after["name"] == before["name"]
