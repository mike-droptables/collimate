"""Real Chromium checks for the transaction-based ordinary-text input path."""


async def check_input_semantics(suite, context, page):
    frame, editor = await suite.editor(page, 'milkdown')
    original = await editor.text_content()
    await editor.focus()
    await editor.press('Control+a')
    await editor.press_sequentially('replacement')
    assert await editor.text_content() == 'replacement'
    await editor.press('Control+z')
    assert await editor.text_content() == original, 'undo must restore the replaced selection'
    await editor.press('Control+Shift+z')
    assert await editor.text_content() == 'replacement'
    await editor.press('Control+a')
    await editor.press('Backspace')
    await editor.press_sequentially('# Heading')
    assert await editor.locator('h1').text_content() == 'Heading', 'heading input rule must still run'
    await editor.press('Control+a')
    await editor.press('Backspace')
    # Input.imeSetComposition produces actual browser composition events.
    # Composing edits stay with ProseMirror's native composition handler.
    cdp = await context.new_cdp_session(page)
    await cdp.send('Input.imeSetComposition', {'text': 'に', 'selectionStart': 1, 'selectionEnd': 1})
    await cdp.send('Input.imeSetComposition', {'text': '日本', 'selectionStart': 2, 'selectionEnd': 2})
    await cdp.send('Input.insertText', {'text': '日本'})
    await page.wait_for_timeout(100)
    assert await editor.text_content() == '日本', 'composition commit must neither duplicate nor lose text'
    await editor.press('x')
    await editor.press('Shift+ArrowLeft')
    await editor.press('Y')
    assert await editor.text_content() == '日本Y', 'ordinary insertion must preserve selection replacement after IME'
    await page.wait_for_timeout(400)
    return {'renderer': 'milkdown', 'selection_undo_redo': True, 'heading_input_rule': True,
            'native_ime_commit': True, 'selection_after_ime': True}


async def milkdown_input_semantics(suite, browser):
    context, page, _, _, errors = await suite.fixture(browser, 'notes.md', 'original')
    try:
        result = await check_input_semantics(suite, context, page)
        assert not errors, errors
        return result
    finally:
        await context.close()
