"""Actual editor capture, local durability and accepted-save timing under load.

Run against the built frontend and vendored SDK. Background activity uses real
checkpoints and accepted host writes on a sibling file, not an external runner.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import time
import traceback
import uuid

from playwright.async_api import async_playwright

from persistence_browser import ROOT, Suite, record_contains
from persistence_editor_metrics import durability, instrumentation, percentiles

FIXTURES = {
    '49-kib': ('paragraph text.\n\n' * 4000)[:49 * 1024],
    '51-kib': ('paragraph text.\n\n' * 4000)[:51 * 1024],
    '210k-characters': ('paragraph text.\n\n' * 14000)[:210000],
    'unicode': '日本語🌻 café\n\n' * 2500,
    'soft-lines': 'line\n' * 15000,
}
DEFAULT_FIXTURES = ['49-kib', '51-kib', '210k-characters', 'unicode']
FILES = {'milkdown': 'notes.md', 'text': 'notes.txt', 'monaco': 'notes.py'}


async def observe(page, frame):
    return {
        'host': await page.evaluate('({data:window.__p5,metrics:window.__persistenceMetrics})'),
        'renderer': await frame.evaluate('({metrics:window.__persistenceMetrics,plugins:window.__persistencePluginTimes})'),
    }


async def accepted_activity(suite, channel, cell, filename, marker, stop, rows):
    writer, revision, sequence = 'host:performance:' + uuid.uuid4().hex, 1, 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), 1)
            return
        except TimeoutError:
            pass
        sequence += 1
        began = time.monotonic()
        operation = {'operation_id': uuid.uuid4().hex, 'resolves': [], 'mutations': [{
            'path': 'activity.txt', 'base_revision': revision, 'writer_id': writer,
            'edit_seq': sequence, 'kind': 'put', 'content_text': f'activity {sequence}'}]}
        receipt = await asyncio.to_thread(suite.api, 'POST', '/__persistence/host-mutate',
                                         {'channel': channel, 'cell_id': cell, 'operation': operation})
        assert receipt['outcome'] == 'accepted', receipt
        revision = receipt['files'][0]['file_revision']
        checkpoint = await asyncio.to_thread(suite.api, 'POST',
                                            f'/api/channels/{channel}/cells/{cell}/checkpoint')
        body = await asyncio.to_thread(suite.file, channel, cell, filename)
        saved = body.rsplit(marker, 1)[-1].rstrip('\n') if marker in body else ''
        rows.append({'at': time.time(), 'durationMs': (time.monotonic() - began) * 1000,
                     'operation': operation, 'receipt': receipt, 'checkpoint': checkpoint,
                     'savedTypedCharacters': len(saved)})


def summarize(raw, kind, typed, elapsed, key_times, activity, drain_ms, name):
    data = raw['host']['data']
    local = durability(data, raw['host']['metrics']['snapshotInputs'], kind=kind)
    plugins = raw['renderer'].get('plugins') or []
    capture = [item['duration'] for item in plugins if item['kind'] == 'toJSON']
    handoff = [item['duration'] for item in plugins if item['kind'] == 'saveSnapshot']
    combined = [a + b for a, b in zip(capture, handoff)]
    accepted = [row for row in data['requests'] if row.get('receipt', {}).get('outcome') == 'accepted']
    local_stats, capture_stats = local['keyToSubsumingCommitMs'], percentiles(combined)
    capture_budget = 16 if name == '210k-characters' else 8
    checks = {
        'allControlledInputsObserved': len(data['inputs']) == len(typed),
        'allInputsDurable': local['inputs'] == len(typed) and local['committed'] == len(typed),
        'localP99AtMost250ms': local_stats.get('p99', float('inf')) <= 250,
        'everyInputDurableWithin400ms': local_stats.get('max', float('inf')) <= 400,
        'strictJournalTransactions': bool(data['journals']) and all(
            row['durability'] == 'strict' and 'aborted' not in row for row in data['journals']),
        'actualAcceptedRequestsObserved': bool(accepted) and len(accepted) == len(data['requests']),
        'oneRemoteRequestInFlight': data['maxPendingRequests'] <= 1,
        'oneWorkerRequestInFlight': max([row['inFlight'] for row in data['workers']] or [0]) <= 1,
        'continuousAcceptedProgress': sum(row['savedTypedCharacters'] > 0 for row in activity) >= 2,
        'drainedWithin5s': drain_ms <= 5000,
        'requestedCadenceMaintained': elapsed <= len(typed) * .1 * 1.1,
    }
    if kind == 'milkdown':
        checks['everySnapshotCaptureMeasured'] = len(capture) == len(handoff) == len(typed)
        checks['captureAndHandoffP95Budget'] = capture_stats.get('p95', float('inf')) <= capture_budget
        checks['actualWorkerCompletionsObserved'] = bool(data['workers']) and all(
            'completed' in row for row in data['workers'])
    return {
        'checks': checks, 'passed': all(checks.values()), 'typed': len(typed),
        'typingSeconds': elapsed, 'keyWallMs': percentiles(key_times),
        'snapshotDurability': local, 'captureAndHandoffMs': capture_stats,
        'coldCaptureAndHandoffMs': combined[0] if combined else None,
        'warmCaptureAndHandoffMs': percentiles(combined[1:]),
        'captureBudgetMs': capture_budget if kind == 'milkdown' else None,
        'requestAckMs': percentiles([row['completed'] - row['at'] for row in accepted]),
        'remoteDrainMs': drain_ms, 'maxRemoteInFlight': data['maxPendingRequests'],
        'maxJournalTransactionsInFlight': data['maxPendingJournals'],
        'maxWorkerInFlight': max([row['inFlight'] for row in data['workers']] or [0]),
        'maxObservedUnmaterializedEditLag': max([
            row['sequence'] - row['materializedSequence'] for row in data['journals']] or [0]),
        'maxObservedUnacceptedEditLag': max([
            row['sequence'] - row['savedSequence'] for row in data['journals']] or [0]),
        'workerRoundTripMs': percentiles([row['completed'] - row['at']
                                        for row in data['workers'] if 'completed' in row]),
        'instrumentedPutOrSendMs': percentiles(data['wrapperMs']),
        'rendererLongTasks': raw['renderer']['metrics']['longTasks'],
        'hostLongTasks': raw['host']['metrics']['longTasks'], 'activity': activity,
    }


async def measure_case(suite, browser, name, kind, samples, burst_reload):
    marker = 'P5' + uuid.uuid4().hex
    body, filename = FIXTURES[name] + '\n\n' + marker, FILES[kind]
    context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
    await context.add_init_script(instrumentation(marker))
    stop, activity_rows, activity_task = asyncio.Event(), [], None
    page = frame = None
    try:
        _, page, channel, cell, errors = await suite.fixture(
            browser, filename, body, context=context,
            extra_files=[{'name': 'activity.txt', 'content': 'initial'}])
        frame, editor = await suite.editor(page, kind)
        await editor.focus()
        await editor.press('Control+End')
        await suite.record_frames(editor)
        await page.evaluate('window.__p5Begin()')
        if frame != page.main_frame:
            await frame.evaluate('window.__p5Begin()')
        activity_task = asyncio.create_task(accepted_activity(
            suite, channel, cell, filename, marker, stop, activity_rows))
        typed, key_times = '', []
        began = time.monotonic()
        for index in range(samples):
            await asyncio.sleep(max(0, began + index * .1 - time.monotonic()))
            key = chr(97 + index % 26)
            key_start = time.monotonic()
            await editor.press(key, timeout=45000)
            key_times.append((time.monotonic() - key_start) * 1000)
            typed += key
        await asyncio.sleep(max(0, began + samples * .1 - time.monotonic()))
        elapsed = time.monotonic() - began
        typing_finished = time.monotonic()
        stop.set()
        # Observe400ms from the last key, before awaiting background completion.
        await page.wait_for_timeout(400)
        boundary = await observe(page, frame)
        (suite.directory / f'{kind}-{name}-boundary.json').write_text(json.dumps(boundary, indent=2))
        records = await suite.journal(page, filename)
        accepted = await asyncio.to_thread(suite.file, channel, cell, filename)
        assert any(record_contains(record, typed) for record in records) or typed in accepted, 'Latest input absent at400ms'
        await activity_task
        drain_started = time.monotonic()
        while not accepted.rstrip('\n').endswith(marker + typed):
            assert time.monotonic() - drain_started < 10, 'Remote owner did not drain latest input'
            await page.wait_for_timeout(50)
            accepted = await asyncio.to_thread(suite.file, channel, cell, filename)
        drain_ms = (time.monotonic() - typing_finished) * 1000
        await page.wait_for_timeout(100)
        raw = await observe(page, frame)
        frames = await frame.evaluate('window.__persistenceFrames')
        result = summarize(raw, kind, typed, elapsed, key_times, activity_rows, drain_ms, name)
        result.update(name=name, renderer=kind, bytes=len(body.encode()), characters=len(body),
                      latestPresentAt400ms=True,
                      localDurabilityAt400ms=durability(boundary['host']['data'],
                          boundary['host']['metrics']['snapshotInputs'], kind=kind),
                      errors=errors, frameSamples=frames, raw=raw)
        result['checks']['samePhysicalEditorDuringTyping'] = all(row['connected'] for row in frames)
        result['checks']['noPageErrors'] = not errors
        if burst_reload:
            suite.fault(refuse=True)
            suffix = 'recovery' + uuid.uuid4().hex[:8]
            await editor.press_sequentially(suffix, delay=35)
            await page.wait_for_timeout(400)
            records = await suite.journal(page, filename)
            assert any(record_contains(record, typed + suffix) for record in records), 'Refused burst not durable at400ms'
            result['refusedBurstRaw'] = await observe(page, frame)
            await page.reload(wait_until='domcontentloaded')
            await suite.open_file(page, filename)
            recovered_frame, recovered = await suite.editor(page, kind)
            if kind == 'milkdown':
                assert typed + suffix in await recovered.text_content()
            elif kind == 'text':
                assert typed + suffix in await recovered.input_value()
            else:
                # A narrow horizontally scrolled viewport need not render the
                # entire line. Check its visible suffix, then a real model edit.
                deadline = time.monotonic() + 5
                while True:
                    await recovered.press('Control+End')
                    visible = await recovered_frame.locator('.view-lines').inner_text()
                    if suffix in visible:
                        break
                    assert time.monotonic() < deadline, 'Recovered Monaco suffix did not render'
                    await page.wait_for_timeout(50)
                await recovered.press('Z')
                await page.wait_for_timeout(400)
                assert any(record_contains(record, typed + suffix + 'Z')
                           for record in await suite.journal(page, filename)), 'Recovered Monaco model lost prior input'
            result['checks']['refused400msReload'] = True
            suite.fault()
        result['passed'] = all(result['checks'].values())
        return result
    except Exception:
        if page is not None and frame is not None and not page.is_closed():
            try:
                evidence = await observe(page, frame)
                evidence['activity'] = activity_rows
                (suite.directory / f'{kind}-{name}-failure.json').write_text(json.dumps(evidence, indent=2))
            except Exception:
                pass
        raise
    finally:
        stop.set()
        if activity_task and not activity_task.done():
            await activity_task
        await context.close()


def environment(suite):
    files = [Path(__file__), Path(__file__).with_name('persistence_editor_metrics.py'),
             Path(__file__).with_name('persistence_browser.py'),
             Path(__file__).with_name('persistence_service_metrics.py'), ROOT / 'frontend/dist/index.html']
    return {
        'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'os': platform.platform(), 'cpuCount': os.cpu_count(), 'loadAverage': list(os.getloadavg()),
        'cpu': subprocess.run(['lscpu', '--json'], capture_output=True, text=True, check=True).stdout,
        'filesystem': subprocess.run(['findmnt', '--json', '--target', str(suite.directory),
                                      '--output', 'FSTYPE,OPTIONS,SOURCE'],
                                     capture_output=True, text=True, check=True).stdout,
        'instrumentationAndBuildHashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                         for path in files},
    }


def verify_sources(path):
    manifest = json.loads(path.read_text())
    changed = [name for name, digest in manifest['files'].items()
               if not (ROOT.parent / name).is_file()
               or hashlib.sha256((ROOT.parent / name).read_bytes()).hexdigest() != digest]
    assert not changed, f'Measured source differs from freeze: {changed}'
    return {'path': str(path), 'manifest_sha256': manifest['manifest_sha256'],
            'files': len(manifest['files']), 'changed': changed}


async def run(args):
    os.environ['PERSISTENCE_MEASURE_SERVICE'] = '1'
    os.environ['PERSISTENCE_MEASURE_CAS_SYNC'] = 'on'
    os.environ['PERSISTENCE_MEASURE_SQLITE'] = 'FULL'
    suite, rows = Suite(args.output), []
    receipt = {'schema_version': 1, 'phase': 'P5', 'measurement': 'actual-editor',
               'samples': rows, 'requestedSamplesPerWorkload': args.samples,
               'requestedCadenceMs': 100, 'fullScale': args.samples >= 100,
               'limitations': [
                   'Activity uses real accepted host mutations/checkpoints; no live external generator is measured.',
                   'Worker/HTTP counts are observed in-flight work; sequence lag is separate from internal queue length.',
                   'Long tasks describe the whole editor/host; overlap alone is not causal attribution to persistence.',
                   'Source proxy and browser API wrappers are test-only; their work is included in these timings.',
                   'instrumentedPutOrSendMs includes native browser API work, not just observation overhead.',
                   'The test server records service stages with CAS sync on and SQLite FULL; these timings include that observer.',
               ]}

    def save():
        args.output.write_text(json.dumps(receipt, indent=2) + '\n')

    try:
        if args.source_manifest:
            receipt['sourceAtStart'] = verify_sources(args.source_manifest)
        await suite.start()
        receipt['environment'] = environment(suite)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            receipt['environment']['browser'] = browser.version
            try:
                for kind in args.renderer or list(FILES):
                    for name in args.fixture or DEFAULT_FIXTURES:
                        print(json.dumps({'renderer': kind, 'fixture': name, 'status': 'running'}), flush=True)
                        try:
                            row = await measure_case(suite, browser, name, kind, args.samples, args.burst_reload)
                        except Exception as exc:
                            row = {'renderer': kind, 'name': name, 'passed': False,
                                   'error': repr(exc), 'traceback': traceback.format_exc()}
                        rows.append(row)
                        save()
                        omitted = {'raw', 'refusedBurstRaw', 'activity', 'frameSamples',
                                   'snapshotDurability', 'localDurabilityAt400ms'}
                        print(json.dumps({key: value for key, value in row.items() if key not in omitted}), flush=True)
            finally:
                await browser.close()
        receipt['passed'] = bool(rows) and all(row['passed'] for row in rows)
        receipt['serverMeasurements'] = suite.api('GET', '/__persistence/measurements')
        config = next(row for row in receipt['serverMeasurements'] if row.get('kind') == 'server_configuration')
        assert config['casSync'] == 'on' and config['sqliteRequested'] == 'FULL'
        for store in ('channel', 'global'):
            assert config['sqliteObserved'][store], f'No actual {store} SQLite settings observed'
            for observed in config['sqliteObserved'][store]:
                actual = json.loads(observed)
                assert actual['synchronous'] == 2 and actual['journalMode'] == 'wal', actual
        receipt['gatePassed'] = receipt['passed'] and receipt['fullScale']
        receipt['environment']['loadAverageAtFinish'] = list(os.getloadavg())
        if args.source_manifest:
            receipt['sourceAtFinish'] = verify_sources(args.source_manifest)
        save()
        return int(not receipt['passed'])
    finally:
        await suite.stop()
        suite.log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('tmp/persistence-1008/editor-performance.json'))
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--burst-reload', action='store_true')
    parser.add_argument('--fixture', action='append', choices=list(FIXTURES))
    parser.add_argument('--renderer', action='append', choices=list(FILES))
    parser.add_argument('--source-manifest', type=Path)
    options = parser.parse_args()
    if options.samples < 1:
        parser.error('--samples must be positive')
    raise SystemExit(asyncio.run(run(options)))
