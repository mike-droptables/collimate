"""Chat identifiers include every declared live chat surface."""

from sessions import chat as chat_sessions

import inspect

import pytest

from routes import chat_live


@pytest.mark.parametrize("broadcast", [False, True])
def test_chat_registration_replacement_and_teardown_use_chat_id__F0287(broadcast):
    def register():
        return chat_sessions.register_surface_session(
            "vocabulary-run", "vocabulary-cell", "_runs/vocabulary-aaaaaaaa/view.data",
            broadcast=broadcast, answer_route="inbox", inbox=True,
        )

    sibling = chat_sessions.register_surface_session(
        "sibling-run", "vocabulary-cell", "_runs/sibling-bbbbbbbb/view.data",
        broadcast=True, answer_route="",
    )
    try:
        pointer = register()
        assert set(pointer) == {"chat_id", "ws_path", "token"}
        chat_id = pointer["chat_id"]
        assert chat_id.startswith("chat_")
        assert pointer["ws_path"] == f"/api/chats/{chat_id}"
        session = chat_sessions.get_inbox("vocabulary-run")
        assert session.chat_id == chat_id
        replacement = register()
        assert set(replacement) == {"chat_id", "ws_path", "token"}
        if broadcast:
            assert replacement == pointer
            assert chat_sessions.get_inbox("vocabulary-run") is session
        else:
            assert replacement["chat_id"] != chat_id
            assert chat_id not in chat_sessions._sessions
            assert session.closed
        chat_sessions.unregister_chat_session(chat_id=replacement["chat_id"])
        assert chat_sessions.get_inbox("vocabulary-run") is None
        assert sibling["chat_id"] in chat_sessions._sessions
        route = next(route for route in chat_live.router.routes if route.endpoint is chat_live.chat_ws)
        assert route.path == "/api/chats/{chat_id}"
        assert "chat_id" in inspect.signature(chat_live.chat_ws).parameters
    finally:
        chat_sessions.unregister_sessions_for_run("vocabulary-run")
        chat_sessions.unregister_sessions_for_run("sibling-run")
