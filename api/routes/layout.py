"""Responses for canvas layout mutations and content-creating operations."""
def channel_layout(channel) -> dict:
    """Send ordered cell metadata without walking or serializing file bodies."""
    from state import state_lock
    exclude = {key: {"__all__": {"cells": {"__all__": {"files"}}}}
               for key in ("main_stacks", "nav_stacks")}
    with state_lock:
        return channel.model_dump(mode="json", exclude=exclude)


def stripped_channel(channel) -> dict:
    """New content follows the same lazy-file rule as state broadcasts."""
    from state import state_lock, file_metadata_state
    with state_lock:
        exclude = {key: {"__all__": {"cells": {"__all__": {"files"}}}}
                   for key in ("main_stacks", "nav_stacks")}
        result = channel.model_dump(mode="json", exclude=exclude)
    return file_metadata_state({"channels": [result]})["channels"][0]
