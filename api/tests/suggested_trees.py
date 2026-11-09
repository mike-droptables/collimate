"""A synthetic content tree to read suggestions from, and the helpers that make
it the app's own.

The tree has the layout ``collimate._host.packaging.suggested`` reads: asset-tree
cells under ``generators/`` and ``renderers/`` and one suggestion,
``suggested/basic.json``. Every name and body is hand-made: two generator
cells (``First`` holds the ``alpha`` generator, a binary file and two runner
packages, ``ready`` complete and ``needs`` missing its prompt; ``Second``
holds ``beta``), one renderer cell (``Views``) and a one-file tutorial cell
(``Tour``). No cell names a library; ``library`` writes one into a tree
and ``install_libraries`` makes a set of them the app's own.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

PNG = b"\x89PNG\r\n\x1a\n\x00\xff"
MAIN = "settings.set_yaml"


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def runner(identity: str, *, generator: str = "alpha", prompt: str = "Write the notes.") -> dict:
    """A runner definition of the synthetic ``alpha`` generator."""
    return {"id": identity, "generator": generator, "name": identity.title(), "description": "Synthetic runner",
            "tags": [], "prompt": prompt, "settings": {MAIN: {"message": "hello"}}, "folders": [],
            "accounts": {}, "permissions": {}, "packages": {}}


def draft(identity: str, **kwargs) -> dict:
    return {"runner": runner(identity, **kwargs), "files": []}


def _generator(cell: Path, gid: str, *, extra: dict | None = None) -> None:
    write(cell / gid / "choice.generator", yaml.safe_dump({
        "id": gid, "name": gid.title(), "summary": "Synthetic generator", "entrypoint": f"{gid}.py",
        "main_config": MAIN, **(extra or {})}))
    write(cell / gid / f"{gid}.py", f"print('{gid}')\n")
    write(cell / gid / MAIN, "fields:\n  - key: message\n    type: text\n    default: hello\n")


def build_tree(root: Path, *, suggestion: dict | None = None) -> Path:
    """Write the synthetic tree under ``root`` and return ``root``."""
    generators, renderers = root / "generators", root / "renderers"
    first = generators / "first"
    _generator(first, "alpha", extra={"prompt_required": True, "prompt_intent": "What to write"})
    write(first / "alpha" / "image.png", PNG)
    write(first / "ready" / "ready.runner", yaml.safe_dump(runner("ready"), sort_keys=False))
    write(first / "needs" / "needs.runner", yaml.safe_dump(runner("needs", prompt=""), sort_keys=False))
    write(first / "cell.yaml", yaml.safe_dump({"name": "First", "icon": "scale", "order": 1}))
    second = generators / "second"
    _generator(second, "beta")
    write(second / "cell.yaml", yaml.safe_dump({"name": "Second", "order": 2}))
    write(renderers / "views" / "widget" / "view.renderer",
          "id: widget\nname: Widget\nsummary: Synthetic renderer\nentrypoint: main.tsx\nembed: native\n"
          "extensions: [.widget]\n")
    write(renderers / "views" / "widget" / "main.tsx",
          "import { defineRenderer } from '/api/sdk/react.js';\nconst Component = () => null;\n"
          "export default defineRenderer({ component: Component });\n")
    write(renderers / "views" / "cell.yaml", yaml.safe_dump({"name": "Views", "icon": "eye"}))
    write(root / "suggested" / "tutorial" / "tour" / "tour.slides", "# Tour\n\nSynthetic slides.\n")
    write(root / "suggested" / "basic.json", json.dumps(suggestion or {
        "name": "Basic", "description": "Synthetic suggestion", "order": 10,
        "generators": [["First", "generators/first"], ["Second", "generators/second"]],
        "renderers": [["Views", "renderers/views"]],
        "libraries": [],
        "tutorial": [["Tour", "suggested/tutorial/tour"]],
        "quick_fields": {},
    }))
    return root


def library(folder: Path, name: str, files: dict[str, str | bytes] | None = None, *,
            description: str = "Synthetic library") -> Path:
    """Write the library ``name`` (``<folder>/<name>/<name>.library`` and
    ``files``, relative to the library) and return its folder."""
    root = folder / name
    write(root / f"{name}.library", yaml.safe_dump({"id": name, "name": name.strip("_").title(),
                                                     "description": description}))
    for rel, body in (files or {"__init__.py": f"NAME = {name!r}\n"}).items():
        write(root / rel, body)
    return root


def install_libraries(monkeypatch, libraries: dict[str, dict[str, str | bytes]], root: Path) -> Path:
    """Make ``libraries`` (``{name: {relative path: body}}``, written as
    library packages under ``root/generators``) the libraries the app ships:
    the bundled content's generators tree, the rest of the bundled content
    left as it is. Returns ``root``."""
    import collimate_server.genesis as genesis
    for name, files in libraries.items():
        library(root / "generators", name, files)
    real = genesis.bundled_dir
    monkeypatch.setattr(genesis, "bundled_dir", lambda tree: str(root) if tree == "content" else real(tree))
    return root


def use_content(monkeypatch, client, path) -> dict:
    """Make ``path`` the content tree the app reads its suggestions from (the
    tree installed with the server, as a checkout's own ``content/`` is);
    returns the catalog."""
    import collimate_server.genesis as genesis
    real = genesis.bundled_dir
    monkeypatch.setattr(genesis, "bundled_dir", lambda tree: str(path) if tree == "content" else real(tree))
    response = client.get("/api/suggested")
    assert response.status_code == 200, response.text
    return response.json()


def create_suggested(client, name: str, suggestion: str = "basic", *, tutorial: bool = True,
                     runners: list | None = None):
    return client.post("/api/channels", params={"name": name}, json={
        "suggested": {"id": suggestion, "tutorial": tutorial, "runners": runners or []}})


def provenance(channel_id: str, cell_id) -> list[dict]:
    """The provenance records admission wrote for one cell of a channel."""
    from collimate_server.database import get_db_connection
    from collimate_server.provenance import read
    conn = get_db_connection(str(channel_id))
    try:
        return read(conn, [str(cell_id)]).get(str(cell_id), [])
    finally:
        conn.close()


def folder_hash(folder: Path) -> str:
    """``content_hash`` of every file under ``folder``, relative to it: a
    package folder's identity, computed from the tree on disk."""
    from collimate._host.package_hash import content_hash
    return content_hash({path.relative_to(folder).as_posix(): path.read_bytes()
                         for path in sorted(folder.rglob("*")) if path.is_file()})
