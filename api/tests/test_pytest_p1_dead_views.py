"""User-facing readers of the current history surfaces."""


def test_the_dag_verdict_add_cell_and_index_remove_routes_are_gone_O02(client):
    """The history DAG's routes and the index-addressed remove are deleted;
    the overview reads the lineage, the shelf restores and removes by id."""
    paths = client.get('/openapi.json').json()['paths']
    served = {(method.upper(), path) for path, ops in paths.items() for method in ops}
    gone = {
        ('GET', '/api/channels/{channel}/dag'),
        ('GET', '/api/channels/{channel}/cells/{cell_id}/dag'),
        ('POST', '/api/channels/{channel}/cells/verdict'),
        ('POST', '/api/channels/{channel}/main/add_cell'),
        ('POST', '/api/stacks/{channel}/{stack_id}/remove_cell'),
    }
    assert not served & gone
    assert {('GET', '/api/channels/{channel}/lineage'),
            ('POST', '/api/channels/{channel}/cells/{cell_id}/restore'),
            ('POST', '/api/channels/{channel}/cells/{cell_id}/remove')} <= served
    cell = '00000000-0000-0000-0000-000000000001'
    # A POST no route serves is 404, or 405 when a built UI in api/static is
    # mounted at "/" (StaticFiles answers every unserved POST with 405).
    assert client.get('/api/channels/starting/dag').status_code == 404
    assert client.get(f'/api/channels/starting/cells/{cell}/dag').status_code == 404
    assert client.post('/api/channels/starting/cells/verdict',
                       json={'cell_ids': [cell], 'verdict': 'maybe'}).status_code in (404, 405)
    assert client.post('/api/channels/starting/main/add_cell', json={'cell_id': cell}).status_code in (404, 405)
