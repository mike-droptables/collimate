"""Local sites — POST /cells/{cell_id}/sites.

A click on a localhost link in a cell offers "View site"; this route finds
the live run on that cell that serves the port, makes the port reachable
through the app's proxy for that run, publishes a ``.website`` pointer in
the run's folder and reveals it. The generator takes no part: the host
sees what the run serves.

A run serves the port when (a) it already does by the host's own reading
(``ProcessManager.serves_port``: an environment publishing or forwarding
it, or a process of the run listening on it on this machine), the host
port being the port itself; (b) a container of one of its open
environments accepts a connection on it at its bridge address, the host
port being the loopback forwarder ``EnvironmentService.expose_port`` gives
(an existing one for that container port is reused); or (c) a program in
such a container listens on it at an address only the container reaches
(127.0.0.1 there, most dev servers' default), the host port being a
loopback relay that runs a ``docker exec`` stdio-to-TCP relay per
connection (``EnvironmentService.find_listener`` / ``expose_listener``;
reused the same way). An image with no relay program (python3, node or
socat) answers a 404 that says so. The pointer ``_runs/<label>/site-<port>.website``
is host-published like the run card, and goes with the run folder's sweep
at run end; the port stays in the run's ``bound_ports`` until the run ends
or its environment closes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json

from fastapi import APIRouter, HTTPException

from collimate_server.cell_commits import cell_commit_lock, commit_cell_change
from collimate_server.database import canonical_channel_id
from collimate_server.run_folders import site_pointer_path
from collimate_server.run_registry import runs_on_cell
from collimate_server.routes.proxy import proxy_path

from collimate_server.routes.runs.models import SiteRequest

router = APIRouter()


def _live(manager) -> bool:
    """Whether the run is live and not ending: its process runs and its
    writes are not sealed."""
    process = manager.process
    return (
        process is not None and process.returncode is None
        and not manager.stop_requested and not manager.final_status
        and not manager.sealed
    )


class _Unrelayable(Exception):
    """A program of the run listens on the port where the host cannot reach
    it; the message says where and why."""


def _unrelayable_detail(listener) -> str:
    address = f"[{listener.address}]" if ":" in listener.address else listener.address
    where = f"{address}:{listener.port}"
    if ipaddress.ip_address(listener.address).is_loopback:
        reach = (f"A program in this cell's container listens on {where}, which only "
                 "that container reaches")
    else:
        reach = (f"A program in this cell's container listens on {where}, and the "
                 "container refuses the host's connection to it")
    return (f"{reach}. The host relays such a port through python3, node or socat "
            "inside the container, and its image has none of them. Have the program "
            "listen on 0.0.0.0 (for example --host 0.0.0.0) or add python3 to the image.")


async def _served_host_port(manager, port: int) -> int | None:
    """The loopback host port through which ``manager``'s run serves
    ``port``, registered in the run's ``bound_ports``; None when it does
    not serve it. A container port is forwarded or relayed and registered
    in one step (``expose_listener(serve=True)``); an environment closing
    meanwhile serves nothing. Raises ``_Unrelayable`` for a container
    listener the host has no way to reach."""
    if await asyncio.to_thread(manager.serves_port, port):
        manager.bound_ports.add(port)
        return port
    # Created on the run's first environment op: a run that placed none
    # has none.
    service = getattr(manager, "_environment_service", None)
    if service is None:
        return None
    listener = await asyncio.to_thread(service.find_listener, port)
    if listener is None:
        return None
    if not listener.bridge and listener.relay is None:
        raise _Unrelayable(_unrelayable_detail(listener))
    try:
        return await asyncio.to_thread(service.expose_listener, listener, serve=True)
    except RuntimeError:
        return None


def _publish_pointer(manager, cell_id: str, path: str, pointer: dict) -> bool:
    """Commit the pointer under the cell lock unless the run's writes are
    sealed (its end has begun: the sweep must not miss the pointer). An
    unchanged pointer commits nothing. Whether the pointer stands."""
    body = json.dumps(pointer, indent=2, sort_keys=True) + "\n"
    with cell_commit_lock(cell_id):
        if manager.sealed:
            return False
        commit_cell_change(
            manager.channel, cell_id, manager.run_id, writes={path: body},
            label="Site opened", source_type="generator", generator_name="host",
        )
    return True


@router.post("/api/channels/{channel}/cells/{cell_id}/sites")
async def open_site(channel: str, cell_id: str, body: SiteRequest):
    """Expose the local site on ``body.port`` that a live run of the cell
    serves: the run's id and label, the pointer's cell path and the proxy
    URL. The pointer is committed and revealed (``RevealFile``); a repeated
    call for the same port keeps it and reveals it again. 404 when no live
    run on the cell serves the port, saying where a program listens when
    one does but cannot be reached."""
    from collimate_server.state import require_channel
    ch = str(require_channel(channel).id)
    port = body.port
    unreachable = None
    for manager in runs_on_cell(cell_id):
        if canonical_channel_id(manager.channel or "") != ch or not _live(manager):
            continue
        try:
            host_port = await _served_host_port(manager, port)
        except _Unrelayable as e:
            unreachable = unreachable or str(e)
            continue
        if host_port is None:
            continue
        url = proxy_path(manager.channel, manager.run_id, host_port)
        path = site_pointer_path(manager.label, port)
        pointer = {
            "url": url,
            "directUrl": f"http://127.0.0.1:{host_port}/",
            "loadingCover": False,
            "buttons": [],
        }
        if not await asyncio.to_thread(_publish_pointer, manager, cell_id, path, pointer):
            continue
        from collimate_server.events import broadcast
        from collimate._host.channel_events import RevealFile
        broadcast(manager.channel, RevealFile(manager.run_id, cell_id, path).to_dict())
        return {"run_id": manager.run_id, "label": manager.label, "path": path, "url": url}
    raise HTTPException(404, unreachable or f"No running runner on this cell serves port {port}")
