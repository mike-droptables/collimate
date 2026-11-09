"""The current settings format has one forward shape."""

import json
import sqlite3


def test_settings_have_no_history_pin_section__F0272():
    import collimate_server.channel_settings as channel_settings

    assert 'pins' not in channel_settings.default_settings()
    assert 'pins' not in channel_settings.SECTIONS
    assert 'pins' not in channel_settings.validate({'kind': 'channel-settings'})


def test_stack_model_does_not_persist_unused_widths__F0650():
    from collimate_server.models import Stack

    saved = Stack().model_dump()
    assert 'width' not in saved
    assert 'prev_width' not in saved


def test_migrations_remove_retired_fields_preserving_work__F0272_F0650(fresh_cwd):
    from collimate_server.migrations import CHANNEL_MIGRATIONS, GLOBAL_MIGRATIONS, apply

    for migrations, old_version in [(CHANNEL_MIGRATIONS, 10), (GLOBAL_MIGRATIONS, 1)]:
        conn = sqlite3.connect(':memory:')
        apply(conn, [m for m in migrations if m[0] <= old_version])
        cell = {'name': 'Keep', 'pinned': 'notes.md', 'files': [{'name': 'notes.md', 'content': 'work'}]}
        stack = {'name': 'S', 'width': 57, 'prev_width': 41, 'cells': [cell]}
        app = {'channels': [{'main_stacks': [stack], 'nav_stacks': [stack]}]}
        settings = {'pins': [{'id': 'old'}], 'layout': {'defaultStackWidth': 500}, 'editing': {'spellcheck': False}}
        conn.executemany('INSERT INTO ui_state (key,value) VALUES (?,?)', [
            ('app_state', json.dumps(app)), ('channel_settings', json.dumps(settings)),
        ])
        conn.commit()
        apply(conn, migrations)
        saved = {k: json.loads(v) for k, v in conn.execute('SELECT key,value FROM ui_state')}
        kept = {'layout': {'defaultStackWidth': 500}, 'editing': {'spellcheck': False}}
        # A channel database also takes the channel-settings kind (migration 25).
        assert saved['channel_settings'] == (kept if migrations is GLOBAL_MIGRATIONS else {**kept, 'kind': 'channel-settings'})
        layout = saved['app_state']
        if migrations is GLOBAL_MIGRATIONS:
            # The app database no longer holds any layout (migration 14): channels
            # live in their own folders, and an old layout is not carried over.
            assert layout == {'storage_format': 'sql-layout-v3', 'active_channel_id': None}
            assert apply(conn, migrations) == []
            conn.close()
            continue
        for field in ['main_stacks', 'nav_stacks']:
            migrated_stack = layout['channels'][0][field][0]
            assert migrated_stack['name'] == 'S'
            assert 'width' not in migrated_stack and 'prev_width' not in migrated_stack
            migrated_cell = migrated_stack['cells'][0]
            assert migrated_cell['name'] == cell['name']
            assert migrated_cell['pinned'] == cell['pinned']
            assert [(file['name'], file['content']) for file in migrated_cell['files']] == [('notes.md', 'work')]
        assert apply(conn, migrations) == []
        conn.close()
