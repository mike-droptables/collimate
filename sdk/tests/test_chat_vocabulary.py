"""Chat pointers and the run's inbox name their chat registry identity."""

from collimate._host.bridge import HostBridge
from collimate.pointers import CHAT_KEYS, chat_session_pointer
from collimate.testing import run_generator_callable


def test_surface_bridge_returns_chat_id_for_the_inbox__F0287_D22(monkeypatch):
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge.folder = "_runs/runner-12345678"
    monkeypatch.setattr(bridge, "_host_available", lambda: True)
    monkeypatch.setattr(bridge, "emit_and_wait", lambda *args, **kwargs: {
        "ok": True, "chat_id": "chat_1", "ws_path": "/api/chats/chat_1", "token": "t",
    })
    session = bridge.surface_register("_runs/runner-12345678/conversation.chat",
                                      label="Chat", transport_only=False, broadcast=False)
    assert session == {"chat_id": "chat_1", "ws_path": "/api/chats/chat_1", "token": "t"}
    chat = chat_session_pointer(session, threads_dir="_runs/runner-12345678/chat")
    assert set(chat) == set(CHAT_KEYS) == {
        "chat_id", "ws_path", "token", "label", "threads_dir", "active_thread", "threads",
    }
    assert chat["chat_id"] == "chat_1"


def test_recorded_surface_registration_names_chat_id__F0287():
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, files=False, broadcast=True,
                             pointer_file=gen.run_path("chat/conversation.chat"),
                             threads_dir=gen.run_path("chat"))

    result = run_generator_callable(run, messages=["hi"])
    assert result.ok, result.error
    [registration] = [call for call in result.tier_b_calls if call["op"] == "surface_register"]
    assert registration["chat_id"] == "rec-surface-1"
    assert set(registration) == {"op", "chat_id", "path", "label", "transport_only", "broadcast"}
    assert registration["broadcast"] is True and registration["transport_only"] is True
