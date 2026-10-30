"""The split owners expose usable tools without a package compatibility facade."""
import asyncio
import importlib

import pytest



def test_public_tool_owners_preserve_registry_and_containment_F1341_F1351(native_bridge):
    import gen
    from _agent_shared import workspace_tools as workspace, web_tools as web
    gen.update_file('work.md', 'Persisted task bytes\n')
    rows = workspace.tool_entries(enable_file_tools=True, websearch=False)
    read = next(row[4] for row in rows if row[0] == 'read_file')
    assert 'Persisted task bytes' in asyncio.run(read('work.md'))
    with pytest.raises(workspace.PathEscape):
        workspace.contain_ws_path('../outside.md')
    assert web.vet_url('http://127.0.0.1/private')[0]
    assert [row[0] for row in rows if row[1] == 'file'] == [
        'list_files', 'read_file', 'grep', 'view_image', 'write_file', 'edit_file']
    assert 'test_generator' not in [row[0] for row in rows]


def test_retired_package_tool_facade_is_absent_F1341_F1351():
    core = importlib.import_module('_agent_shared')
    for name in ('__all__', 'tool_entries', 'run_turn', 'read_file', 'fetch', '_workspace_tool_entries', '_run_pydantic_ai',
                 '_ws_read_file', '_web_fetch_impl'):
        assert name not in vars(core)
    from _agent_shared.harnesses import HARNESS
    assert HARNESS['pydantic-ai'].prepare == 'harnesses.pydantic_ai.backend:prepare'
