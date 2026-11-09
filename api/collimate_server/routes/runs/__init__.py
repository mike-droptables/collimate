"""Run lifecycle routes — create, stop, list runs; inspect cells.

Composed from topical submodules:

  * create.py      — POST /runs
  * control.py     — stop, stop a channel's runs, emit_renderer_action,
                     change_run_option
  * sites.py       — open_site (a local site a live run of the cell serves)
  * queries.py     — list / get / active (one channel, every channel) /
                     events_since / artifacts
  * inspection.py  — list_cell_files
  * teardown.py    — stop_all_runs (Stop all and server shutdown)
  * helpers.py / models.py — shared helpers and request models

Each submodule owns its own ``APIRouter``; this __init__ builds a single
combined router that preserves the existing ``from routes.runs import router``
import path. Only ``router`` and ``stop_all_runs`` are re-exported; import everything else from its
submodule.
"""

from fastapi import APIRouter, Depends

from collimate_server.state import require_channel

from collimate_server.routes.runs import control, create, inspection, queries, sites
from collimate_server.routes.runs.teardown import stop_all_runs


router = APIRouter()
router.include_router(create.router)
router.include_router(control.router)
router.include_router(sites.router)
# Read routes admit a registered UUID before opening its existing database.
router.include_router(queries.router, dependencies=[Depends(require_channel)])
router.include_router(queries.everywhere_router)
router.include_router(inspection.router, dependencies=[Depends(require_channel)])


__all__ = [
    "router",
    "stop_all_runs",
]
