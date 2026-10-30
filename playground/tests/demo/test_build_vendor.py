"""The vendored api tree: the bundled trees where the app's bundled default
finds them, the SDK's portable compiler bytes beside the staged frontend, and
no prebuilt renderer bundle — the app stores a server bundle for every
renderer of the demo channel itself."""
from pathlib import Path
import hashlib
import importlib.util
import json

import collimate
import yaml

from conftest import create_demo_channel

ROOT = Path(__file__).resolve().parents[3]
DEMO_GENERATORS = ROOT / "playground/demo/generators"


def _vendor():
    spec = importlib.util.spec_from_file_location("build_vendor", ROOT / "playground/build/vendor.py")
    vendor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vendor)
    return vendor


def _tree_bytes(tree: Path) -> dict[str, bytes]:
    return {path.relative_to(tree).as_posix(): path.read_bytes()
            for path in tree.rglob("*") if path.is_file()
            and "__pycache__" not in path.parts and "node_modules" not in path.parts and path.suffix != ".pyc"}


def test_P4F09_vendor_stages_the_bundled_trees_and_the_canonical_browser_compiler(tmp_path, monkeypatch):
    sdk = Path(collimate.__file__).resolve().parents[2]
    _vendor().run(tmp_path, ROOT / "playground/demo", ROOT, sdk, shim_dir=ROOT / "playground/shim")
    api = tmp_path / "api"
    assert {path.name for path in api.iterdir()} == {
        "collimate_content", "collimate_builtins", "collimate_playground", "_demo"}

    content = api / "collimate_content"
    assert {path.name for path in content.iterdir()} == {"generators", "renderers", "suggested"}
    assert _tree_bytes(content / "generators") == _tree_bytes(DEMO_GENERATORS)
    assert _tree_bytes(content / "renderers") == _tree_bytes(ROOT / "content/renderers")
    assert _tree_bytes(api / "collimate_builtins") == _tree_bytes(ROOT / "builtins")
    assert [path.name for path in (content / "suggested").iterdir()] == ["demo.json"]
    suggestion = json.loads((content / "suggested/demo.json").read_text())
    assert suggestion["generators"] == [["Demo generators", "generators/demo"]]
    assert suggestion["renderers"] == json.loads((ROOT / "content/suggested/standard.json").read_text())["renderers"]
    assert suggestion["tutorial"] == []
    from collimate._host.packaging import read_suggestions
    assert [s.id for s in read_suggestions(content)] == ["demo"]

    # pyodide has the staged api on sys.path: the app's bundled default is it.
    monkeypatch.syspath_prepend(str(api))
    import genesis
    assert Path(genesis.bundled_dir("content")) == content
    assert Path(genesis.bundled_dir("builtins")) == api / "collimate_builtins"

    # .stage/sdk/ holds only the SDK files the staged frontend imports by
    # relative path, at the paths its imports name.
    staged_sdk = tmp_path / "sdk"
    assert sorted(path.relative_to(staged_sdk).as_posix() for path in staged_sdk.rglob("*") if path.is_file()) == [
        "scripts/renderer_bundle_core.d.mts", "scripts/renderer_bundle_core.mjs",
        "src/collimate/renderer_bundle_contract.json"]
    for name in ("scripts/renderer_bundle_core.mjs", "scripts/renderer_bundle_core.d.mts",
                 "src/collimate/renderer_bundle_contract.json"):
        assert (staged_sdk / name).read_bytes() == (sdk / name).read_bytes()
    worker = (ROOT / "frontend/src/lib/rendererBundle.worker.ts").read_text()
    for specifier in ("../../../sdk/scripts/renderer_bundle_core.mjs",
                      "../../../sdk/src/collimate/renderer_bundle_contract.json"):
        assert f"'{specifier}'" in worker
        assert (tmp_path / "frontend/src/lib" / specifier).resolve().is_file()
    assert (api / "collimate_playground/boot.py").is_file()
    assert (api / "_demo/channel.yaml").is_file() and (api / "_demo/channel").is_dir()


def test_P4F09_demo_channel_serves_every_renderer_from_a_server_bundle(app_client):
    import builtin_packages

    client = app_client(DEMO_GENERATORS)
    content_ids = {yaml.safe_load(path.read_bytes())["id"]
                   for path in (client.content / "renderers").rglob("*.renderer")}
    builtin_ids = set(builtin_packages.build().renderers)
    assert content_ids and builtin_ids and not content_ids & builtin_ids

    response = create_demo_channel(client, "Demo", [])
    assert response.status_code == 200, response.text
    catalog = client.get("/api/renderers").json()
    assert {row["id"] for row in catalog} == content_ids | builtin_ids

    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        stored = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT source_hash, blob_hash, origin FROM renderer_bundles")}
    finally:
        conn.close()
    # Startup and admission compiled everything: no row came from a client.
    assert {origin for _blob, origin in stored.values()} == {"server"}
    for row in catalog:
        result = client.get(f"/api/renderers/{row['id']}/source", params={"v": row["content_hash"]})
        assert result.status_code == 200, (row["id"], result.text)
        assert result.headers["X-Entrypoint"] == "module.js"
        assert result.content and b"export" in result.content
        assert stored[row["content_hash"]] == (hashlib.sha256(result.content).hexdigest(), "server"), row["id"]
        stale = client.get(f"/api/renderers/{row['id']}/source", params={"v": "0" * 64})
        assert stale.status_code == 404
