"""P4F09: lifespan persistence works when native threads cannot start.

The app persists a state snapshot through ``genesis.save_app_state``: each
channel's layout into its own channel.db, the active channel id into app.db
(``genesis.load_active_channel_id`` reads it back). Every persist and flush,
and the lifespan's shutdown flush, must reach it inline, in order, with a
failed write propagating to the caller."""
import asyncio
from contextlib import asynccontextmanager
import importlib
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
    import collimate_server.state as state
    import threading
    import collimate_server.user_validation as user_validation
    import collimate_server.genesis as genesis
    import collimate_server.channel_paths as channel_paths
    from collimate_server.models import AppState
    monkeypatch.setattr(state, "state", AppState())
    monkeypatch.setattr(state, "_persistence_writer", None)
    monkeypatch.setattr(state, "start_persistence_writer", state.start_persistence_writer)
    monkeypatch.setattr(state, "stop_persistence_writer", state.stop_persistence_writer)
    # boot() names the demo's channel location as --channels-dir would.
    monkeypatch.setattr(channel_paths, "_startup", {"channels_dir": None, "claim": False})
    monkeypatch.setattr(genesis, "_saved_active", {})
    calls = []
    notifications = []

    real_save = genesis.save_app_state

    def write(snapshot):
        calls.append(snapshot)
        if write_fails:
            raise OSError("save failed")
        real_save(snapshot)

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(genesis, "save_app_state", write)
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
    import collimate_server
    fake_main = SimpleNamespace(app=app)
    monkeypatch.setitem(sys.modules, "collimate_server.main", fake_main)
    # `import collimate_server.main as main` reads the package attribute first.
    monkeypatch.setattr(collimate_server, "main", fake_main, raising=False)

    async def seed(*args):
        return {"id": "test"}

    monkeypatch.setattr(boot, "seed_channel", seed)
    monkeypatch.chdir(tmp_path)

    async def exercise():
        await boot.boot(app_root=str(tmp_path), data_dir=str(tmp_path), log=lambda text: None)
        assert state._persistence_writer is None
        assert channel_paths._startup == {"channels_dir": str(tmp_path / "Channels"), "claim": False}
        for index, operation in enumerate((state.persist_state, state.flush_persist_state), 1):
            state.state.active_channel_id = UUID(int=index)
            if write_fails:
                with pytest.raises(OSError, match="save failed"):
                    operation()
            else:
                operation()
                assert genesis.load_active_channel_id() == str(UUID(int=index))
        state.state.active_channel_id = UUID(int=3)
        if write_fails:
            with pytest.raises(OSError, match="save failed"):
                await boot._lifespan.__aexit__(None, None, None)
        else:
            await boot._lifespan.__aexit__(None, None, None)
        assert [snapshot["active_channel_id"] for snapshot in calls] == [str(UUID(int=index)) for index in (1, 2, 3)]
        if not write_fails:
            assert genesis.load_active_channel_id() == str(UUID(int=3))
        else:
            assert genesis.load_active_channel_id() is None
        assert len(notifications) == 3
        assert state._persistence_writer is None

    asyncio.run(exercise())
