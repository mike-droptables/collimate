"""Built-in holds apply when the renderer catalog is read: moves and direct
writes reach Capabilities without admission, so a user renderer claiming a
built-in renderer's id or a surface extension is skipped, never served, and
reported as a Capabilities problem.
"""
from __future__ import annotations

import pytest

from conftest import install_test_renderer


def _surface() -> str:
    from builtin_packages import SURFACE_EXTENSIONS
    return sorted(SURFACE_EXTENSIONS)[0]


def _builtin_id() -> str:
    from builtin_packages import builtin_renderers
    return sorted(builtin_renderers())[0]


@pytest.mark.parametrize("claim", ["surface", "suffix", "builtin-id"])
def test_a_renderer_claiming_a_built_in_is_skipped_and_reported(client, claim):
    from builtin_packages import builtin_renderers
    from routes.blobs import renderer_inventory_problems
    from state import find_active_channel
    if claim == "builtin-id":
        rid, extensions = _builtin_id(), [".memo"]
    else:
        rid = "synth-view"
        extensions = [".memo", _surface() if claim == "surface" else ".x" + _surface()]
    install_test_renderer(renderer_id=rid, extensions=extensions)
    kept = install_test_renderer(renderer_id="synth-kept", extensions=[".kept"])

    listing = client.get("/api/renderers?include_disabled=1").json()
    users = [row for row in listing if not row["builtin"]]
    assert [row["id"] for row in users] == [kept]
    if claim == "builtin-id":
        (served,) = [row for row in listing if row["id"] == rid]
        assert served["builtin"] is True and served["content_hash"] == builtin_renderers()[rid].source_hash
    else:
        assert client.get(f"/api/renderers/{rid}/source").status_code == 404
        assert client.get(f"/api/renderers/{rid}/package", params={"v": "x"}).status_code == 404

    (problem,) = renderer_inventory_problems(find_active_channel())
    assert problem["path"] == f"{rid}/view.renderer"
    assert ("built-in renderer" in problem["error"]) if claim == "builtin-id" else ("surface" in problem["error"])


def test_a_second_renderer_with_one_id_is_reported_and_the_first_serves(client):
    from routes.blobs import renderer_inventory_problems
    from state import find_active_channel
    install_test_renderer(renderer_id="synth-view", name="First", extensions=[".memo"])
    install_test_renderer(renderer_id="synth-view", name="Second", extensions=[".memo"])
    rows = [row for row in client.get("/api/renderers").json() if row["id"] == "synth-view"]
    assert [row["name"] for row in rows] == ["First"]
    (problem,) = renderer_inventory_problems(find_active_channel())
    assert problem["error"] == "Duplicate renderer id 'synth-view'"
