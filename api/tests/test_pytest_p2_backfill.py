"""Bounded reload of an accelerated day-long run's durable log."""
import uuid


def test_backfill_tail_preserves_outcomes_and_cursor__F0343(client):
    from database import get_db_connection
    rid = str(uuid.uuid4())
    with get_db_connection(client.channel_id) as conn:
        conn.execute("INSERT INTO runs(id,status,phase,failure_reason) VALUES(?,'failed','failed','exit code 1')", (rid,))
        conn.executemany('INSERT INTO run_events(run_id,event_type,payload) VALUES(?,?,?)', [
            (rid, 'stop', '{"stop_type":"user"}'),
            (rid, 'no_output', '{}'),
            (rid, 'finish', '{"status":"failed","error_message":"failed to start"}'),
            (rid, 'timeout', '{"failure_reason":"timeout"}'),
        ])
        conn.executemany('INSERT INTO run_events(run_id,event_type,payload) VALUES(?,?,?)',
                         ((rid, 'stdout', 'x' * 1024) for _ in range(86400)))
        last = conn.execute('SELECT max(id) FROM run_events WHERE run_id=?', (rid,)).fetchone()[0]
        plan = conn.execute('EXPLAIN QUERY PLAN SELECT id FROM run_events WHERE run_id=? AND id>? ORDER BY id DESC LIMIT 5000', (rid, 0)).fetchall()
        assert any('idx_run_events_run' in row['detail'] for row in plan)
    route = f'/api/channels/{client.channel_id}/runs/{rid}/events_since'
    result = client.get(route).json()
    assert len(result['events']) == 5004
    assert result['truncated'] is True
    assert result['dropped_logs'] == 81400
    assert result['last_event_id'] == last
    assert result['failure_reason'] == 'exit code 1'
    assert result['is_terminal'] is True
    assert [e['type'] for e in result['events'][:4]] == ['stop', 'no_output', 'finish', 'timeout']
    assert [e['id'] for e in result['events']] == sorted(e['id'] for e in result['events'])
    fresh = client.get(route, params={'since_id': last - 2}).json()
    assert len(fresh['events']) == 2
    assert fresh['truncated'] is False
    assert fresh['dropped_logs'] == 0
    assert fresh['last_event_id'] == last
    assert client.get(route, params={'since_id': last}).json()['events'] == []
