"""The media renderers use the canonical renderer runtime's shared viewer."""
from pathlib import Path

from tests._workspace import RENDERERS


ROOT = Path(__file__).resolve().parents[1]


def test_shared_media_and_desktop_consumers_use_canonical_runtime__F1807_F1828_F1847():
    runtime = (ROOT / 'packages/renderer-hooks/src/runtime.tsx').read_text()
    assert 'MediaViewer' in runtime, 'shared media component must be exported by served SDK runtime'
    for kind in ('audio', 'video'):
        source = (RENDERERS / f'visual/{kind}/main.tsx').read_text()
        assert 'MediaViewer' in source and f'kind="{kind}"' in source
        assert 'useState' not in source and 'setRetryNonce' not in source

