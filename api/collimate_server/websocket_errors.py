"""WebSocket rejection responses shared by session routes."""

from fastapi import WebSocket


async def reject_websocket(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept the upgrade, then close with an app close code. Closing
    pre-accept sends a plain HTTP 403 handshake rejection, which strips the
    code — the renderer then can't tell "session gone, stop" (4404/4401)
    from "someone else holds it" (4409) or a transient failure, and retries
    a dead session forever."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)
