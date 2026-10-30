"""Matched isolated-service comparison for the P5 durability gate.

Run only after the joined source is frozen and the measurement window is quiet.
The four child servers use fresh throwaway data directories. Weak settings
exist only in persistence_service_metrics.install inside those child servers.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import httpx

from persistence_browser import Suite
from persistence_durability_measurement_support import (
    environment, instrumentation_hashes, percentiles, source_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = {'small': 1024, '49KiB': 49 * 1024, '51KiB': 51 * 1024,
             '210k-characters': 210_000, 'unicode': 51 * 1024}
CONFIGURATIONS = [('off', 'NORMAL'), ('on', 'NORMAL'),
                  ('off', 'FULL'), ('on', 'FULL')]
METRICS = ('ackMs', 'scheduledStartLagMs', 'serviceMs', 'stageMs', 'hashMs', 'verifyHashMs', 'verifyMs',
           'fileSyncMs', 'fileSyncSkippedCallMs', 'directorySyncMs', 'directorySyncSkippedCallMs',
           'dbDirectorySyncMs', 'renameMs', 'publicationLockWaitMs',
           'publicationLockHoldMs', 'cellLockWaitMs', 'cellLockHoldMs', 'sqlCommitCallMs',
           'dbConfirmationMs')


def body_for(seed, scenario, index, size, *, cell=0):
    prefix = uuid.uuid5(uuid.NAMESPACE_URL, f'P5:{seed}:{scenario}:{index}:{cell}').hex
    if scenario == 'unicode':
        body = prefix + '🧪' * ((size - len(prefix)) // 4)
    else:
        body = prefix + 'x' * (size - len(prefix))
    assert len(body.encode('utf-8')) == size
    return body


def new_shard_bodies(directory, count, seed):
    objects = directory / '.collimate/objects'
    occupied = {item.name for item in objects.iterdir() if item.is_dir()} if objects.exists() else set()
    if len(occupied) + count > 256:
        raise RuntimeError(f'Only {256 - len(occupied)} fresh CAS shards remain after fixture setup')
    chosen = set()
    bodies = []
    candidate = 0
    while len(bodies) < count:
        body = body_for(seed, 'new-shard-candidate', candidate, 1024)
        prefix = hashlib.sha256(body.encode()).hexdigest()[:2]
        if prefix not in occupied and prefix not in chosen:
            chosen.add(prefix)
            bodies.append(body)
        candidate += 1
    return bodies


def existing_shard_bodies(directory, count, seed, shard_body):
    prefix = hashlib.sha256(shard_body.encode()).hexdigest()[:2]
    shard = directory / '.collimate/objects' / prefix
    assert shard.is_dir(), f'Target existing CAS shard is missing: {shard}'
    bodies = []
    candidate = 0
    while len(bodies) < count:
        body = body_for(seed, 'existing-shard-candidate', candidate, 1024)
        digest = hashlib.sha256(body.encode()).hexdigest()
        if digest[:2] == prefix and not (shard / digest[2:]).exists():
            bodies.append(body)
        candidate += 1
    return bodies


def sync_level(directory, path):
    target = Path(path)
    if not target.is_absolute():
        target = directory / target
    relative = target.resolve().relative_to(directory.resolve())
    parts = relative.parts
    if not parts:
        return 'data-directory'
    if parts == ('.collimate',):
        return 'collimate-directory'
    if parts == ('.collimate', 'objects'):
        return 'objects-directory'
    if len(parts) == 3 and parts[:2] == ('.collimate', 'objects'):
        return 'shard-directory'
    return '/'.join(parts)


def merge_instrumentation(row, measured, directory):
    assert row['operationId'] == measured['operationId']
    assert row['receipt'] == measured['receipt'], 'Client and service saw different immutable receipts'
    assert measured['outcome'] == 'accepted'
    row.update({key: value for key, value in measured.items() if key != 'receipt'})
    row['fileSyncMs'] = sum(call['ms'] for call in row['fileSyncCalls'] if not call['skipped'])
    row['fileSyncSkippedCallMs'] = sum(call['ms'] for call in row['fileSyncCalls'] if call['skipped'])
    directory_levels = {}
    skipped_directory_ms = 0.0
    for call in row['directorySyncCalls']:
        level = sync_level(directory, call['path'])
        call['level'] = level
        if call['skipped']:
            skipped_directory_ms += call['ms']
        else:
            directory_levels[level] = directory_levels.get(level, 0.0) + call['ms']
    row['directorySyncByLevelMs'] = directory_levels
    row['directorySyncMs'] = sum(directory_levels.values())
    row['directorySyncSkippedCallMs'] = skipped_directory_ms
    database_levels = {}
    for call in row['dbDirectorySyncCalls']:
        level = sync_level(directory, call['path'])
        call['level'] = level
        database_levels[level] = database_levels.get(level, 0.0) + call['ms']
    row['dbDirectorySyncByLevelMs'] = database_levels
    row['dbDirectorySyncMs'] = sum(database_levels.values())
    assert not any(call['failed'] for call in row['fileSyncCalls'] + row['directorySyncCalls']
                   + row['dbDirectorySyncCalls'])
    return row


def scenario_summary(rows):
    scenarios = sorted({row['scenario'] for row in rows if row['scenario'] not in {'warmup', 'slow-stage', 'alongside-slow-stage'}})
    result = {}
    for scenario in scenarios:
        subset = [row for row in rows if row['scenario'] == scenario]
        timing = {metric: percentiles([row[metric] for row in subset]) for metric in METRICS}
        levels = sorted({level for row in subset for level in row['directorySyncByLevelMs']})
        timing['directorySyncByLevelMs'] = {
            level: percentiles([row['directorySyncByLevelMs'].get(level, 0.0) for row in subset])
            for level in levels}
        database_levels = sorted({level for row in subset for level in row['dbDirectorySyncByLevelMs']})
        timing['dbDirectorySyncByLevelMs'] = {
            level: percentiles([row['dbDirectorySyncByLevelMs'].get(level, 0.0) for row in subset])
            for level in database_levels}
        result[scenario] = {'count': len(subset), 'timingMs': timing,
                            'newObjectCount': sum(row['newObject'] for row in subset),
                            'newShardCount': sum(row['newShard'] for row in subset),
                            'dedupeCount': sum(row['dedupe'] for row in subset),
                            'maxClientQueueDepth': max(row['clientQueueDepthAtIssue'] for row in subset),
                            'maxServiceQueueDepth': max(row['serviceQueueDepthAtEntry'] for row in subset),
                            'maxScheduleDebtMs': max(row['scheduledStartLagMs'] for row in subset),
                            'cadenceDeadlineMisses': sum(row.get('cadenceDeadlineMiss', False) for row in subset)}
    return result


def validate_sqlite_observations(observed, requested):
    expected = 1 if requested == 'NORMAL' else 2
    for store in ('channel', 'global'):
        assert observed[store], f'No effective {store} SQLite connection was observed'
        for encoded in observed[store]:
            setting = json.loads(encoded)
            assert setting['synchronous'] == expected, (store, setting)
            assert setting['journalMode'].lower() == 'wal', (store, setting)


def matched_overhead(configurations):
    by_key = {(result['casSync'], result['sqliteSync']): result for result in configurations}
    comparisons = {}
    for dimension, pairs in {
        'cas-at-NORMAL': [(('off', 'NORMAL'), ('on', 'NORMAL'))],
        'cas-at-FULL': [(('off', 'FULL'), ('on', 'FULL'))],
        'sqlite-at-cas-off': [(('off', 'NORMAL'), ('off', 'FULL'))],
        'sqlite-at-cas-on': [(('on', 'NORMAL'), ('on', 'FULL'))],
    }.items():
        first, second = pairs[0]
        a, b = by_key[first], by_key[second]
        per_scenario = {}
        for scenario in sorted(a['summary']):
            def order(row):
                return row['index'], row['bodyHash']
            left = sorted((row for row in a['samples'] if row['scenario'] == scenario), key=order)
            right = sorted((row for row in b['samples'] if row['scenario'] == scenario), key=order)
            assert len(left) == len(right)
            assert [(row['index'], row['bodyHash']) for row in left] == [
                (row['index'], row['bodyHash']) for row in right]
            per_scenario[scenario] = {
                metric: {'absoluteFirstMs': a['summary'][scenario]['timingMs'][metric],
                         'absoluteSecondMs': b['summary'][scenario]['timingMs'][metric],
                         'pairedDeltaMs': percentiles([r[metric] - l[metric] for l, r in zip(left, right)])}
                for metric in ('ackMs', 'serviceMs', 'fileSyncMs', 'directorySyncMs',
                               'sqlCommitCallMs', 'dbConfirmationMs')}
        comparisons[dimension] = {'first': first, 'second': second, 'scenarios': per_scenario}
    return comparisons


async def run_configuration(args, cas_sync, sqlite_sync, shard_bodies, existing_bodies):
    output = args.output.parent / f'config-{cas_sync}-{sqlite_sync}.json'
    suite = Suite(output)
    suite.fault()
    settings = {'PERSISTENCE_MEASURE_SERVICE': '1',
                'PERSISTENCE_MEASURE_CAS_SYNC': cas_sync,
                'PERSISTENCE_MEASURE_SQLITE': sqlite_sync}
    previous = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    startup_at = time.perf_counter()
    try:
        await suite.start()
    except BaseException:
        await suite.stop()
        suite.log.close()
        raise
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    startup_ms = (time.perf_counter() - startup_at) * 1000
    rows = []
    try:
        created_at = time.perf_counter()
        channel = suite.api('POST', '/api/channels?name=P5%20service%20measurement', timeout=180)['id']
        channel_create_ms = (time.perf_counter() - created_at) * 1000
        cells_at = time.perf_counter()
        cells = []
        for index in range(2):
            result = suite.import_cell(channel, f'P5 service measurement {index}',
                                       [{'name': 'timing.txt', 'content': 'initial'}])
            cells.append(result['cell_ids'][0])
        cell_setup_ms = (time.perf_counter() - cells_at) * 1000
        if shard_bodies is None:
            shard_bodies = new_shard_bodies(suite.directory, args.samples, args.seed)
        else:
            occupied = {item.name for item in (suite.directory / '.collimate/objects').iterdir()
                        if item.is_dir()}
            assert all(hashlib.sha256(body.encode()).hexdigest()[:2] not in occupied
                       for body in shard_bodies), 'Matched new-shard body already existed in this arm'
        revisions = {(cell, 'timing.txt'): 1 for cell in cells}
        sequences = {}
        last_body = {}
        writers = {cell: f'p5-service-{index}-{args.seed}' for index, cell in enumerate(cells)}
        outstanding = 0
        max_outstanding = 0
        client_lock = asyncio.Lock()
        async with httpx.AsyncClient(base_url=suite.base, timeout=30) as client:
            async def write(cell, body, scenario, index, path='timing.txt'):
                nonlocal outstanding, max_outstanding
                key = cell, path
                prior = revisions.get(key, 0)
                seq = sequences.get(key, 0) + 1
                sequences[key] = seq
                operation_id = str(uuid.uuid4())
                raw = body.encode('utf-8')
                digest = hashlib.sha256(raw).hexdigest()
                operation = {'operation_id': operation_id, 'resolves': [], 'mutations': [{
                    'path': path, 'writer_id': writers[cell], 'edit_seq': seq,
                    'base_revision': prior, 'kind': 'put', 'content_text': body,
                    'encoding': 'utf-8'}]}
                async with client_lock:
                    depth = outstanding
                    outstanding += 1
                    max_outstanding = max(max_outstanding, outstanding)
                started = time.perf_counter_ns()
                try:
                    response = await client.post(
                        f'/api/channels/{channel}/cells/{cell}/files/mutate', json=operation)
                    ack_ms = (time.perf_counter_ns() - started) / 1_000_000
                    response.raise_for_status()
                    receipt = response.json()
                    assert receipt['operation_id'] == operation_id and receipt['outcome'] == 'accepted'
                    assert len(receipt['files']) == 1 and not receipt['conflicts']
                    file = receipt['files'][0]
                    assert file['path'] == path and file['body_hash'] == digest
                    assert file['writer_id'] == writers[cell] and file['edit_seq'] == seq
                    assert file['file_revision'] > prior and not file['deleted']
                    revisions[key] = file['file_revision']
                    last_body[key] = body
                    row = {'scenario': scenario, 'index': index, 'operationId': operation_id,
                           'cellId': cell, 'path': path, 'bodyHash': digest,
                           'bodyBytes': len(raw), 'bodyCharacters': len(body),
                           'ackMs': ack_ms, 'clientQueueDepthAtIssue': depth,
                           'receipt': receipt}
                    rows.append(row)
                    return row
                finally:
                    async with client_lock:
                        outstanding -= 1

            async def paced(cell, body, scenario, index, epoch, path='timing.txt'):
                scheduled = epoch + index * args.cadence_ms / 1000
                await asyncio.sleep(max(0, scheduled - time.perf_counter()))
                started = time.perf_counter()
                row = await write(cell, body, scenario, index, path)
                row['scheduledStartLagMs'] = max(0, (started - scheduled) * 1000)
                row['cadenceDeadlineMiss'] = time.perf_counter() > scheduled + args.cadence_ms / 1000

            epoch = time.perf_counter()
            for index, body in enumerate(shard_bodies):
                await paced(cells[0], body, 'new-shard', index, epoch, 'shards.txt')
            if existing_bodies is None:
                existing_bodies = existing_shard_bodies(
                    suite.directory, args.samples, args.seed, shard_bodies[0])
            epoch = time.perf_counter()
            for index, body in enumerate(existing_bodies):
                await paced(cells[0], body, 'new-object-existing-shard', index, epoch, 'existing-shard.txt')
            dedupe_body = body_for(args.seed, 'dedupe', 0, 1024)
            await write(cells[0], dedupe_body, 'warmup', 0, 'dedupe.txt')
            epoch = time.perf_counter()
            for index in range(args.samples):
                await paced(cells[0], dedupe_body, 'dedupe', index, epoch, 'dedupe.txt')
            for scenario, size in WORKLOADS.items():
                epoch = time.perf_counter()
                for index in range(args.samples):
                    await paced(cells[0], body_for(args.seed, scenario, index, size),
                                scenario, index, epoch)
            epoch = time.perf_counter()
            for index in range(args.samples):
                scheduled = epoch + index * args.cadence_ms / 1000
                await asyncio.sleep(max(0, scheduled - time.perf_counter()))
                started = time.perf_counter()
                pair = await asyncio.gather(*(
                    write(cell, body_for(args.seed, 'concurrent-cells', index, 1024, cell=which),
                          'concurrent-cells', index, 'timing.txt')
                    for which, cell in enumerate(cells)))
                for row in pair:
                    row['scheduledStartLagMs'] = max(0, (started - scheduled) * 1000)
                    row['cadenceDeadlineMiss'] = time.perf_counter() > scheduled + args.cadence_ms / 1000
            suite.fault(stage_delay_path='slow.txt', stage_delay_ms=500)
            slow = asyncio.create_task(write(cells[0], body_for(args.seed, 'slow-stage', 0, 1024),
                                             'slow-stage', 0, 'slow.txt'))
            await asyncio.sleep(.1)
            fast = await write(cells[1], body_for(args.seed, 'alongside-slow-stage', 0, 1024, cell=1),
                               'alongside-slow-stage', 0)
            state_at = time.perf_counter_ns()
            state_response = await client.get('/api/state')
            state_ms = (time.perf_counter_ns() - state_at) / 1_000_000
            state_response.raise_for_status()
            slow_row = await slow
            suite.fault()
            for (cell, path), body in last_body.items():
                check = await client.get(f'/api/channels/{channel}/cells/{cell}/files/{path}')
                check.raise_for_status()
                assert check.content == body.encode('utf-8'), (cell, path)
        measurements = suite.api('GET', '/__persistence/measurements')
        server = next(row for row in measurements if row.get('kind') == 'server_configuration')
        measured = {row['operationId']: row for row in measurements if 'operationId' in row}
        client_ids = {row['operationId'] for row in rows}
        assert len(client_ids) == len(rows), 'Every client operation needs a unique ID'
        assert client_ids <= measured.keys(), 'Every client operation needs one service measurement'
        setup_ids = sorted(measured.keys() - client_ids)
        for row in rows:
            merge_instrumentation(row, measured[row['operationId']], suite.directory)
        validate_sqlite_observations(server['sqliteObserved'], sqlite_sync)
        assert all(row['newObject'] and row['newShard'] for row in rows if row['scenario'] == 'new-shard')
        assert all(row['newObject'] and not row['newShard'] and not row['dedupe']
                   for row in rows if row['scenario'] == 'new-object-existing-shard')
        assert all(row['dedupe'] and not row['newObject'] for row in rows if row['scenario'] == 'dedupe')
        summary = scenario_summary(rows)
        expected_counts = {**{name: args.samples for name in WORKLOADS},
                           'new-shard': args.samples, 'new-object-existing-shard': args.samples,
                           'dedupe': args.samples,
                           'concurrent-cells': args.samples * 2}
        assert {name: item['count'] for name, item in summary.items()} == expected_counts
        budgets = {name: item['timingMs']['ackMs']['p95'] <= 100
                   and item['timingMs']['ackMs']['p99'] <= 250
                   for name, item in summary.items()}
        isolation = {'fastAckMs': fast['ackMs'], 'stateReadMs': state_ms,
                     'slowAckMs': slow_row['ackMs'],
                     'passed': fast['ackMs'] < 250 and state_ms < 250 and slow_row['ackMs'] >= 500}
        return {'casSync': cas_sync, 'sqliteSync': sqlite_sync,
                'setup': {'freshProcessMs': startup_ms, 'freshChannelMs': channel_create_ms,
                          'twoCellSetupMs': cell_setup_ms,
                          'coldDiskCacheClaimed': False, 'seedSelection': 'empty synthetic'},
                'environment': environment(suite.directory),
                'serverObserved': server, 'maxClientInFlight': max_outstanding,
                'setupServiceOperationIds': setup_ids,
                'summary': summary, 'workloadBudgetsPassed': budgets,
                'slowIsolation': isolation, 'samples': rows,
                'serverLog': str(suite.directory / 'server.log'),
                'dataDirectory': str(suite.directory),
                'measurementValid': True,
                'latencyGatePassed': all(budgets.values()) and isolation['passed']}, shard_bodies, existing_bodies
    finally:
        await suite.stop()
        suite.log.close()


async def run(args):
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.samples < 100 and not args.smoke:
        raise ValueError('Acceptance measurement requires at least 100 samples per workload/config')
    if not args.smoke and not args.quiet_window_confirmed:
        raise ValueError('Timed P5 matrix requires the coordinated quiet measurement window')
    tracked = [ROOT / 'api/tests/persistence_service_metrics.py',
               ROOT / 'api/tests/persistence_service_performance.py',
               ROOT / 'api/tests/persistence_durability_measurement_support.py',
               ROOT / 'api/tests/persistence_browser.py',
               ROOT / 'api/cas.py', ROOT / 'api/database.py',
               ROOT / 'api/cell_metadata.py', ROOT / 'api/persistence.py',
               ROOT / 'api/tests/persistence_performance.py',
               ROOT / 'api/tests/persistence_editor_metrics.py']
    start_hashes = instrumentation_hashes(tracked)
    start_manifest = source_manifest(args.source_manifest)
    receipt = {'schema_version': 1, 'phase': 'P5',
               'suite': 'matched_service_durability_2x2',
               'source_manifest': start_manifest,
               'instrumentation_hashes': start_hashes,
               'environment': environment(args.output.parent),
               'configuration': {'matrix': CONFIGURATIONS,
                                 'samplesPerWorkloadPerConfiguration': args.samples,
                                 'cadenceMs': args.cadence_ms, 'seed': args.seed,
                                 'comparison': 'reconstructed sync-only, not historical baseline',
                                 'quietWindowConfirmed': args.quiet_window_confirmed,
                                 'smoke': args.smoke,
                                 'insufficientSamples': args.samples < 100,
                                 'timingBudgetsEnforced': not args.smoke},
               'results': [], 'matched_overhead': {},
               'limitations': ['Process/service timings are not a physical power-loss experiment.',
                               'SQL commit call includes native SQLite work and separate directory confirmation; it is not labeled native fsync.',
                               'CAS verification time includes its own hash time; component timers overlap.',
                               'Skipped CAS sync wrapper time is separate from actual file/directory sync time.',
                               'The service driver serializes each 250 ms workload; scheduled-start debt is recorded rather than claiming an independent 10 Hz queue.',
                               'Fresh process and directory do not imply a cold operating-system disk cache.',
                               'The four arms validate the comparison; latency acceptance applies to on/FULL only.',
                               'Server-only measurement; browser journal and editor budgets are owned by root.']}
    shard_bodies = None
    existing_bodies = None
    try:
        if not start_manifest['verified']:
            raise RuntimeError('Frozen source manifest does not match the joined source candidate')
        for cas_sync, sqlite_sync in receipt['configuration']['matrix']:
            result, shard_bodies, existing_bodies = await run_configuration(
                args, cas_sync, sqlite_sync, shard_bodies, existing_bodies)
            receipt['results'].append(result)
            args.output.write_text(json.dumps(receipt, indent=2))
            print(json.dumps({'configuration': [cas_sync, sqlite_sync],
                              'measurementValid': result['measurementValid'],
                              'latencyGatePassed': result['latencyGatePassed'],
                              'setup': result['setup'],
                              'workloadBudgetsPassed': result['workloadBudgetsPassed'],
                              'slowIsolation': result['slowIsolation']}), flush=True)
        receipt['matched_overhead'] = matched_overhead(receipt['results'])
        end_hashes = instrumentation_hashes(tracked)
        receipt['sourceUnchangedDuringMeasurement'] = end_hashes == start_hashes
        end_manifest = source_manifest(args.source_manifest)
        receipt['sourceManifestAfter'] = end_manifest
        receipt['sourceManifestUnchanged'] = (
            end_manifest['sha256'] == start_manifest['sha256']
            and end_manifest['verified']
            and end_manifest['calculatedManifestSha256'] == start_manifest['calculatedManifestSha256'])
        receipt['comparisonValid'] = (
            all(result['measurementValid'] for result in receipt['results'])
            and receipt['sourceUnchangedDuringMeasurement']
            and receipt['sourceManifestUnchanged'])
        selected = next(result for result in receipt['results']
                        if (result['casSync'], result['sqliteSync']) == ('on', 'FULL'))
        receipt['selectedPolicyPerformancePassed'] = selected['latencyGatePassed']
        receipt['acceptanceEligible'] = not args.smoke and args.samples >= 100
        receipt['passed'] = (receipt['comparisonValid']
                             and (args.smoke or receipt['selectedPolicyPerformancePassed']))
        return int(not receipt['passed'])
    except BaseException as exc:
        receipt['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        args.output.write_text(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-manifest', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--cadence-ms', type=int, default=250)
    parser.add_argument('--seed', default='p5-durability-2026-10-09')
    parser.add_argument('--quiet-window-confirmed', action='store_true')
    parser.add_argument('--smoke', action='store_true',
                        help='Run four configurations with fewer samples; never an acceptance receipt')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
