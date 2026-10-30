"""P4F09 scheduler parity: retain core verdict/revision/error/cancel behavior."""
import asyncio
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("outcome", ["valid", "invalid", "revision", "cancel", "error"])
def test_P4F09_single_loop_validation_retains_core_contract(monkeypatch, outcome):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("threadless_adapter", root / "playground/shim/collimate_playground/threads.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    import user_validation as validation
    import state
    import handlers
    import verification
    monkeypatch.setattr(validation, "ValidationService", validation.ValidationService)
    monkeypatch.setattr(validation, "_service", None)
    monkeypatch.setattr(validation, "DEBOUNCE_SECONDS", 0.001)
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")))
    monkeypatch.setattr(state, "channel_db_name", lambda channel: "00000000-0000-4000-8000-0000000000e1")
    revision = ["one"]
    channel = SimpleNamespace()
    monkeypatch.setattr(validation, "_snapshot", lambda *args: validation.Snapshot(channel, "cell", revision[0], None, ("plan.options_stack",), ()))
    record = SimpleNamespace(accepted_files={"verify.ts": b"actual accepted source"})
    monkeypatch.setattr(handlers, "catalog", lambda channel: SimpleNamespace(for_path=lambda path: record))
    calls = []

    def verify(*args, **kwargs):
        calls.append(args)
        if outcome == "revision":
            revision[0] = "two"
        if outcome == "error":
            raise RuntimeError("backend unavailable")
        return {"valid": True} if outcome == "valid" else {"valid": False, "error": "actual verifier rejection"}

    monkeypatch.setattr(verification, "validate_file", verify)

    async def exercise():
        # The unchanged before implementation reaches its actual blocked Thread.
        if hasattr(adapter, "install_validation"):
            adapter.install_validation()
        validation.start_user_validation()
        service = validation._service
        original_validate = service._validate.__func__
        assert original_validate is validation.ValidationService.__mro__[1]._validate
        initial = validation.read_user_validation("00000000-0000-4000-8000-0000000000e1", "cell")
        assert initial["pending"] and initial["verdicts"] == {}
        if outcome == "cancel":
            validation.close_user_validation()
            await asyncio.sleep(0.01)
            assert calls == [] and service.stopping
            assert not service.pending and not service.results and not service.seen
            assert service.handle is None
            return
        if outcome == "revision":
            service._validate(("00000000-0000-4000-8000-0000000000e1", "cell"))
            assert revision == ["two"] and not service.results
            assert ("00000000-0000-4000-8000-0000000000e1", "cell") in service.pending
        await asyncio.sleep(0.01)
        if outcome in ("valid", "invalid"):
            result = validation.read_user_validation("00000000-0000-4000-8000-0000000000e1", "cell")
            assert not result["pending"] and result["source_revision"] == "one"
            expected = {"valid": True} if outcome == "valid" else {"valid": False, "error": "actual verifier rejection"}
            assert result["verdicts"] == {"plan.options_stack": expected}
            # The exposed result remains a copy of the retained verdict.
            result["verdicts"]["plan.options_stack"]["valid"] = "changed"
            assert service.results[("00000000-0000-4000-8000-0000000000e1", "cell")]["verdicts"]["plan.options_stack"] == expected
        elif outcome == "revision":
            # The core sees a changed snapshot, discards the old result and queues it.
            assert revision == ["two"]
            assert not service.results or all(result["source_revision"] == "two" for result in service.results.values())
            assert calls
        else:
            assert calls and not service.results
            assert validation.read_user_validation("00000000-0000-4000-8000-0000000000e1", "cell")["pending"]
        validation.close_user_validation()
        assert validation._service is None and service.stopping
        assert not service.pending and not service.results and not service.seen
        assert service.handle is None

    asyncio.run(exercise())


def test_P4R02_scheduler_skipped_deadline_is_busy_and_logged_once(monkeypatch, capsys):
    import json
    import user_validation as validation
    import handlers
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / 'playground/shim'))
    from collimate_playground import threads, verification as shim
    from conftest import refusal_overlay_module
    validation = refusal_overlay_module('user_validation', shim, monkeypatch)
    monkeypatch.setattr(validation, 'ValidationService', validation.ValidationService)
    snapshot = validation.Snapshot(SimpleNamespace(), 'cell', 'revision', None, ('sample.check',), ())
    monkeypatch.setattr(validation, '_snapshot', lambda *args: snapshot)
    record = SimpleNamespace(accepted_files={'verify.ts': b'accepted source'})
    monkeypatch.setattr(handlers, 'catalog', lambda channel: SimpleNamespace(for_path=lambda path: record))
    canonical = validation.ValidationService
    clock = iter([100., 131.])
    async def exercise():
        threads.install_validation()
        service = validation.ValidationService()
        assert service._validate.__func__ is canonical._validate
        monkeypatch.setattr(validation.time, 'monotonic', lambda: next(clock, 131.))
        service._validate(('demo', 'cell'))
        result = service.results[('demo', 'cell')]
        assert result['verdicts']['sample.check'] == {'valid': False, 'error': 'Verifier busy, try again (deadline exceeded)'}
        validation.ValidationService._response(snapshot, result['verdicts'])
    asyncio.run(exercise())
    prefix = '[collimate-verifier-refusal] '
    records = [json.loads(line[len(prefix):]) for line in capsys.readouterr().out.splitlines()
               if line.startswith(prefix)]
    assert len(records) == 1
    assert records[0]['reason'] == 'deadline exceeded; verifier busy, try again'
    assert records[0]['elapsed_ms'] >= 0


def test_P4R02_scheduler_preserves_authored_deadline_text(monkeypatch, capsys):
    import json
    import handlers
    import verification as host
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / 'playground/shim'))
    from collimate_playground import threads, verification as shim
    from conftest import refusal_overlay_module
    validation = refusal_overlay_module('user_validation', shim, monkeypatch)
    monkeypatch.setattr(validation, 'ValidationService', validation.ValidationService)
    snapshot = validation.Snapshot(SimpleNamespace(), 'cell', 'revision', None, ('sample.check',), ())
    monkeypatch.setattr(validation, '_snapshot', lambda *args: snapshot)
    record = SimpleNamespace(accepted_files={'verify.ts': b'accepted source'})
    monkeypatch.setattr(handlers, 'catalog', lambda channel: SimpleNamespace(for_path=lambda path: record))
    authored = {'valid': False, 'error': 'verifier budget exceeded'}
    monkeypatch.setattr(host, 'validate_file', shim._observe('verify')(lambda *args, **kwargs: dict(authored)))
    async def exercise():
        threads.install_validation()
        service = validation.ValidationService()
        service._validate(('demo', 'cell'))
        assert service.results[('demo', 'cell')]['verdicts']['sample.check'] == authored
    asyncio.run(exercise())
    prefix = '[collimate-verifier-refusal] '
    records = [json.loads(line[len(prefix):]) for line in capsys.readouterr().out.splitlines()
               if line.startswith(prefix)]
    assert len(records) == 1 and records[0]['reason'] == 'content refused'
