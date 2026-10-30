"""Shipped dependency URLs survive the production renderer compiler exactly."""
from pathlib import Path

from collimate._host.renderer_bundle import bundle_renderer
from collimate.shared import toolchain
from tests._workspace import BUILTINS, RENDERERS


def compiled_renderer(package: Path) -> str:
    assert package.is_dir(), package
    files = {p.relative_to(package).as_posix(): p.read_bytes()
             for p in package.rglob('*') if p.is_file()}
    return bundle_renderer(files, entrypoint='main.tsx').code


def test_excalidraw_compiled_dependency_and_production_stylesheet__F1871():
    code = compiled_renderer(RENDERERS / 'visual/excalidraw')
    react = toolchain()
    assert ('https://esm.sh/@excalidraw/excalidraw@0.18.1'
            f"?deps=react@{react['react']},react-dom@{react['react_dom']}&target=es2022") in code
    assert 'https://cdn.jsdelivr.net/npm/@excalidraw/excalidraw@0.18.1/dist/prod/index.css' in code
    # The supported package export is explicit, without an older module-shape fallback.
    assert 'ExcalidrawMod' not in code


def test_monaco_compiled_amd_asset_base_uses_selected_release__F1872():
    code = compiled_renderer(RENDERERS / 'documents/monaco')
    assert '0.57.0' in code
    assert 'https://cdn.jsdelivr.net/npm/monaco-editor@' in code
    assert '/min/vs' in code
    assert '0.52.2' not in code


def test_terminal_compiled_scoped_family_and_styles_use_the_dom_renderer__F1710():
    code = compiled_renderer(BUILTINS / 'live/terminal')
    assert 'https://esm.sh/@xterm/xterm@6.0.0?target=es2022' in code
    assert 'https://esm.sh/@xterm/xterm@6.0.0/css/xterm.css' in code
    for addon, version in [('fit', '0.11.0'), ('web-links', '0.12.0')]:
        assert f'https://esm.sh/@xterm/addon-{addon}@{version}?deps=@xterm/xterm@6.0.0&target=es2022' in code
    assert 'https://esm.sh/xterm@' not in code
    assert 'https://esm.sh/xterm-addon-' not in code
    # xterm 6 has no canvas renderer, and WebGL can lose its context here.
    assert 'addon-canvas' not in code
    assert 'addon-webgl' not in code
