"""Handler rendering metadata never recursively expands renderer declarations."""
import yaml
from test_pytest_handlers import client as client, write_handler_files


def test_renderer_catalog_reports_direct_alias_fallbacks_without_rewriting_claims__D11(client):
    from conftest import install_test_renderer
    from test_pytest_handlers import install_handler
    from state import find_active_channel
    from handlers import clear_catalog_cache

    html = install_test_renderer(renderer_id='alias-html', extensions=['.html'])
    markdown = install_test_renderer(renderer_id='alias-markdown', extensions=['.md'])
    cell, manifest, _ = install_handler(find_active_channel(), identity='alias-test', ext='.task')
    manifest['types'] = [{'ext': '.REPORT_HTML', 'render_as': '.HTML'},
                         {'ext': '.html', 'render_as': '.md'}]
    write_handler_files(find_active_channel(), cell, {'alias-test/choice.handler': yaml.safe_dump(manifest)})
    clear_catalog_cache()
    entries = {item['id']: item for item in client.get('/api/renderers').json()}
    assert entries[html]['extensions'] == ['.html']
    assert '.report_html' in entries[html]['fallback_extensions']
    assert '.html' in entries[markdown]['fallback_extensions']
    assert '.report_html' not in entries[markdown]['fallback_extensions']
    install_handler(find_active_channel(), identity='clash-alias', ext='.report_html')
    entries = {item['id']: item for item in client.get('/api/renderers').json()}
    assert '.report_html' not in entries[html]['fallback_extensions']


def test_client_casefold_table_tracks_api_unicode_casefold__D11():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('sync_casefold', root / 'frontend/scripts/sync_casefold.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (root / 'frontend/src/lib/unicodeCaseFold.ts').read_text() == module.generated()
