"""Keep edits made at the materialization boundary visible on the canvas."""
from test_pytest_options_identity import CHANNEL, client as client, make_stack, accept_file


def test_merge_reprobes_accepted_files_after_commit__P0F16(client, monkeypatch):
    from routes import options
    _, stack, ids = make_stack([('First', {'a.txt': 'a'}), ('Second', {'b.txt': 'b'})])
    original = options.get_db_connection

    class CommitBoundary:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            accept_file(ids[0], 'a.txt', 'edit at commit boundary')

    monkeypatch.setattr(options, 'get_db_connection', lambda *args, **kwargs: CommitBoundary(original(*args, **kwargs)))
    result = client.post(f'/api/channels/{CHANNEL}/merge', json={'stack_id': str(stack.id)})
    assert result.status_code == 409, result.text
    assert [str(cell.id) for cell in stack.cells] == ids
    assert __import__('persistence').read_file(CHANNEL, ids[0], 'a.txt')[1] == b'edit at commit boundary'
