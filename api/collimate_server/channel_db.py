"""HTTP reads of an existing channel never create its database."""
from fastapi import HTTPException


def open_channel_db(channel: str):
    from collimate_server.database import get_db_connection
    from collimate_server.state import channel_db_name, require_channel

    channel_uuid = channel_db_name(require_channel(channel))
    try:
        return get_db_connection(channel_uuid)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"No such channel: {channel}") from exc
