"""The retained generator behavior after the P1 removals."""
import asyncio



def test_capture_chat_writes_only_session_exports_in_its_own_cell__D01(monkeypatch):
    from _agent_shared.harnesses.claude_code import backend as terminal

    monkeypatch.setattr(terminal, 'pull_claude_transcript', lambda *a, **kw: (
        [{'role': 'assistant', 'content': 'Captured reply'}], '{"type":"result"}\n'))
    monkeypatch.setattr(terminal.gen, 'phase', lambda *a: None)
    monkeypatch.setattr(terminal.gen, 'log', lambda *a: None)
    writes = {}
    monkeypatch.setattr(terminal.gen, 'update_file', lambda path, text: writes.__setitem__(path, text))
    flushes = []

    async def update():
        flushes.append(True)

    monkeypatch.setattr(terminal.gen, 'commit', update)
    asyncio.run(terminal._capture_chat('conversation', store_dir='/unused', project='/unused'))
    assert flushes == [True]
    assert len(writes) == 3
    assert all(path.startswith('captures/') for path in writes)
    assert len({path.rsplit('/', 1)[0] for path in writes}) == 1
    assert {path.rsplit('/', 1)[1] for path in writes} == {
        'transcript.json', 'claude_session.jsonl', 'trace.json'}
    assert any('Captured reply' in text for text in writes.values())
