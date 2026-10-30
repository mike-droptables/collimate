"""P4F09: lifespan persistence works when native threads cannot start."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from uuid import UUID
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("write_fails", [False, True])
def test_P4F09_boot_preserves_inline_persistence_barriers__P6F01(tmp_path, monkeypatch, write_fails):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "playground/shim"))
    for name in list(sys.modules):
        if name == "collimate_playground" or name.startswith("collimate_playground."):
            monkeypatch.delitem(sys.modules, name)
    boot = importlib.import_module("collimate_playground.boot")
    import state
    import threading
    import user_validation
    import genesis
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    monkeypatch.setattr(state, "_persistence_writer", None)
    monkeypatch.setattr(state, "start_persistence_writer", state.start_persistence_writer)
    monkeypatch.setattr(state, "stop_persistence_writer", state.stop_persistence_writer)
    calls = []
    notifications = []

    real_save = genesis.save_global_ui_state

    def write(snapshot):
        calls.append(snapshot)
        if write_fails:
            raise OSError("save failed")
        real_save(snapshot)

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(genesis, "save_global_ui_state", write)
    monkeypatch.setattr(state, "broadcast_state_update", lambda: None)
    monkeypatch.setattr(user_validation, "notify_user_validation", lambda: notifications.append(True))
    monkeypatch.setattr(threading.Thread, "start", no_thread)
    monkeypatch.setattr(boot.threads, "install", lambda: None)
    monkeypatch.setattr(boot.modules, "install", lambda: [])
    monkeypatch.setattr(boot.demo_runner, "install", lambda: None)
    monkeypatch.setattr(boot.terminal, "register_route", lambda app: None)

    @asynccontextmanager
    async def lifespan(app):
        state.start_persistence_writer()
        try:
            yield
        finally:
            state.stop_persistence_writer()

    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(app=app))

    async def seed(*args):
        return {"id": "test"}

    monkeypatch.setattr(boot, "seed_channel", seed)
    monkeypatch.chdir(tmp_path)

    async def exercise():
        await boot.boot(app_root=str(tmp_path), data_dir=str(tmp_path), log=lambda text: None)
        assert state._persistence_writer is None
        for index, operation in enumerate((state.persist_state, state.flush_persist_state), 1):
            state.state.active_channel_id = UUID(int=index)
            if write_fails:
                with pytest.raises(OSError, match="save failed"):
                    operation()
            else:
                operation()
                assert json.loads(genesis.load_global_ui_state())["active_channel_id"] == str(UUID(int=index))
        state.state.active_channel_id = UUID(int=3)
        if write_fails:
            with pytest.raises(OSError, match="save failed"):
                await boot._lifespan.__aexit__(None, None, None)
        else:
            await boot._lifespan.__aexit__(None, None, None)
        assert [snapshot["active_channel_id"] for snapshot in calls] == [str(UUID(int=index)) for index in (1, 2, 3)]
        if not write_fails:
            restored = genesis.parse_saved_app_state(genesis.load_global_ui_state())
            assert restored.active_channel_id == UUID(int=3)
        else:
            assert genesis.load_global_ui_state() is None
        assert len(notifications) == 3
        assert state._persistence_writer is None

    asyncio.run(exercise())
