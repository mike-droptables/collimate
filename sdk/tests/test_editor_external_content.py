# SPDX-License-Identifier: MIT
"""Host-selected external content lands in place without resetting the view.

The editors load their engines from a CDN (monaco) or need a DOM (text), and
these source checks cover each document editor's in-place body application.
The runtime's document ownership is exercised in test_renderer_document_owner.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._workspace import BUILTINS, RENDERERS


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "packages" / "renderer-hooks" / "src"
DOCUMENTS = RENDERERS / "documents"


def test_monaco_publishes_each_edit_and_applies_a_body_in_place():
    source = (DOCUMENTS / "monaco" / "main.tsx").read_text()
    change = re.search(r"editor\.onDidChangeModelContent\(\(e: any\) => \{(.*?)\n                \}\);",
                       source, re.S).group(1)
    # Host replacements do not emit edits; user changes serialize immediately.
    assert "if (e.isFlush || applyingHost) { clearPending(); return; }" in change
    assert "pending = true;" in change
    assert "flush();" in change
    assert "setTimeout" not in change
    # No setValue: one edit over the model, the view state restored.
    assert ".setValue(" not in source
    listener = source[source.index("const unsubReplace = api.onSetContent("):]
    listener = listener[:listener.index("let cancelled = false;")]
    assert "const view = editor.saveViewState();" in listener
    assert ("model.pushEditOperations([], [{ range: model.getFullModelRange(), text: value }], () => null);"
            in listener)
    assert "editor.restoreViewState(view);" in listener
    # Local React content commits cannot replay an older body over newer input.
    assert "}, [content]);" not in source


def test_text_editor_keeps_the_caret_when_a_body_arrives():
    source = (BUILTINS / "text" / "text" / "main.tsx").read_text()
    # Uncontrolled: React never sets the value, so typing never round-trips
    # through it and no re-render resets the text or the caret.
    textarea = source[source.index("<textarea"):]
    textarea = textarea[:textarea.index("/>")]
    assert "ref={areaRef}" in textarea
    assert "value=" not in textarea and "defaultValue" not in textarea
    effect = source[source.index("useLayoutEffect(() => {"):]
    effect = effect[:effect.index("}, [content]);")]
    assert "if (!area || area.value === content) return;" in effect
    assert "const { selectionStart, selectionEnd } = area;" in effect
    assert ("area.setSelectionRange(Math.min(selectionStart, content.length), "
            "Math.min(selectionEnd, content.length));") in effect
    # The area is as tall as its text (RS-04): the scroller around it keeps
    # its own offset, and the area has none to restore.
    assert "scrollTop" not in effect
    assert "fieldSizing: 'content'" in source
