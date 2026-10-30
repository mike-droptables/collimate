"""P4F09: JSPI callback interleaving retains one active advisory worker."""
import asyncio
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace


def test_P4F09_validation_busy_guard_and_inflight_shutdown_drain(monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'shim/collimate_playground/threads.py'
    spec = importlib.util.spec_from_file_location('guarded_scheduler', path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    import handlers
    import state
    import user_validation as validation
    import verification
    monkeypatch.setattr(validation, 'ValidationService', validation.ValidationService)
    monkeypatch.setattr(validation, '_service', None)
    monkeypatch.setattr(validation, 'DEBOUNCE_SECONDS', 0)
    monkeypatch.setattr(state, 'channel_db_name', lambda channel: '00000000-0000-4000-8000-0000000000e1')
    channel = SimpleNamespace()
    monkeypatch.setattr(validation, '_snapshot', lambda *args: validation.Snapshot(channel, 'cell', 'one', None, ('sample.checked',), ()))
    record = SimpleNamespace(accepted_files={'verify.ts': b'accepted bytes'})
    monkeypatch.setattr(handlers, 'catalog', lambda channel: SimpleNamespace(for_path=lambda path: record))
    entered, finish = threading.Event(), threading.Event()
    calls, order = [], []
    holder = {}

    def verify(*args, **kwargs):
        calls.append(args)
        service = holder['service']
        assert service.busy
        # Simulate callbacks dispatched while a supervised Promise is pending.
        service.wake('00000000-0000-4000-8000-0000000000e1', 'cell')
        service.read('00000000-0000-4000-8000-0000000000e1', 'cell')
        service._step()
        assert len(calls) == 1 and service.handle is None
        assert ('00000000-0000-4000-8000-0000000000e1', 'cell') in service.pending
        entered.set()
        assert finish.wait(3)
        order.append('verification-returned')
        return {'valid': True}

    monkeypatch.setattr(verification, 'validate_file', verify)

    async def exercise():
        worker = None

        def drain(pending):
            assert service.stopping and service.busy and not pending.done()
            assert service.results == {'retained': 'until drain'}
            order.append('drain-started')
            finish.set()
            worker.join(3)
            assert not worker.is_alive() and pending.done()
            order.append('drain-finished')

        adapter.install_validation(drain=drain)
        service = validation.ValidationService()
        holder['service'] = service
        service.pending[('00000000-0000-4000-8000-0000000000e1', 'cell')] = 0
        service.results['retained'] = 'until drain'
        # A native test thread models the suspended stack while the lifecycle
        # callback runs; production uses JSPI run_sync, not a Python thread.
        worker = threading.Thread(target=service._step)
        worker.start()
        assert entered.wait(3)
        service.close()
        assert not service.busy and service.idle.done()
        assert service.stopping and service.handle is None
        assert not service.pending and not service.results and not service.seen
        assert order == ['drain-started', 'verification-returned', 'drain-finished']
        assert len(calls) == 1

    asyncio.run(exercise())
