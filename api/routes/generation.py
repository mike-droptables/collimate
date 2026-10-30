"""The generators of the active channel's Capabilities cells."""
import logging
import yaml
from fastapi import APIRouter
from cell_placement import capabilities_stack_cells
from state import find_active_channel

_logger = logging.getLogger("generation")

router = APIRouter()


@router.get("/api/generators")
def list_generators():
    # Plain ``def``: FastAPI runs it in the threadpool, so parsing every
    # generator definition on Capabilities stays off the event loop.
    # Serves the manage view's generator rows; the runner editor and
    # the run gate read the richer /api/runner_generators instead.
    ch = find_active_channel()
    if ch:
        source_cells = capabilities_stack_cells(ch)
        generators = []
        seen_ids: set = set()
        for source_cell in source_cells:
            # The cell name is the group name (e.g. "Basic", "Problem Space")
            group_name = source_cell.name or "Generators"
            from runners import definition_files
            manifests = [f for _, f in definition_files(source_cell, "generator")]
            for mf in manifests:
                try:
                    manifest = yaml.safe_load(mf.content)
                    gen_id = manifest.get("id")
                    if not isinstance(gen_id, str) or not gen_id:
                        continue
                    # Dedup by id (like /api/renderers + the SDK catalog):
                    # the manage view keys its rows by id, so two same-id
                    # manifests must not both appear.
                    if gen_id in seen_ids:
                        _logger.warning(
                            "duplicate generator id %r (cell %r) — keeping the first",
                            gen_id, source_cell.name,
                        )
                        continue
                    seen_ids.add(gen_id)

                    _daemon_on = bool(manifest.get("daemon", False))
                    generators.append({
                        "name": gen_id,
                        "display_name": manifest.get("name", manifest.get("id", "?")),
                        "summary": manifest.get("summary", ""),
                        "cell_id": str(source_cell.id),
                        # Group = the holding cell's name; group icon =
                        # that cell's picked icon.
                        "group": group_name,
                        "group_icon": source_cell.icon or None,
                        # Behavior flags — the manage view's badges.
                        "daemon": _daemon_on,
                        # Chat-capable run (assistant renderer / serve_chat).
                        "chat": bool(manifest.get("chat", False)),
                        # The only config folders a runner may provide to this
                        # generator (skills/mcp/scripts…) — shown read-only
                        # in the manage view as "accepts: …".
                        "config_folders": [
                            e for e in (manifest.get("config_folders") or [])
                            if isinstance(e, dict)
                        ],
                        # Generic mid-run file-editing capability; defaults
                        # to "none" (files locked during the run).
                        "live_files_mode": manifest.get("live_files_mode") or "none",
                        # Read-only facts the Manage view lists per generator.
                        "version": str(manifest["version"]) if manifest.get("version") is not None else None,
                        "entrypoint": manifest.get("entrypoint") or None,
                        "ui_files": [
                            s for s in (manifest.get("ui_files") or [])
                            if isinstance(s, str)
                        ],
                        "requires_docker": bool(manifest.get("requires_docker", False)),
                        # The conditional forms over the main config: a run
                        # needs docker: full, or takes the selected file,
                        # only for the settings the condition names.
                        "requires_docker_if": manifest.get("requires_docker_if") or None,
                        "takes_selected_file": manifest.get("takes_selected_file") is True,
                        "takes_selected_file_if": manifest.get("takes_selected_file_if") or None,
                    })
                except Exception as exc:
                    _logger.warning(
                        "skipping generator manifest %s in cell %r: %s",
                        mf.name, source_cell.name, exc,
                    )
                    continue

        return generators

    return []
