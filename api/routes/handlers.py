"""Authenticated user handler metadata and verification surfaces."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter()


@router.get("/api/handlers")
def list_handlers():
    from handlers import catalog
    from state import find_active_channel

    channel = find_active_channel()
    return catalog(channel).public() if channel else []


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    verifier: str | None = None


@router.post("/api/channels/{channel}/cells/{cell_id}/validate")
def validate(channel: str, cell_id: str, request: ValidationRequest):
    from cell_placement import find_cell
    from fastapi import HTTPException
    from state import require_channel, state_lock
    from verification import validate_file

    current = require_channel(channel)
    with state_lock:
        if find_cell(current, cell_id)[1] is None:
            raise HTTPException(404, "Cell not found")
    return validate_file(current, cell_id, request.path, verifier=request.verifier)


class PackagePreviewRequest(BaseModel):
    """One package of a cell, for the Capabilities stack: into the capability
    cell ``target_cell_id``, or a new cell when it is None."""
    model_config = ConfigDict(extra="forbid")
    source_cell_id: str
    definition_path: str
    target_cell_id: str | None = None


class PackageAddRequest(PackagePreviewRequest):
    expected_content_hash: str
    expected_target_revision: str
    replace: bool = False


def _user_operation(fn, *args):
    from fastapi import HTTPException
    from runners import RunnerError
    from yaml import YAMLError

    try:
        return fn(*args)
    except (
        ValueError,
        KeyError,
        UnicodeError,
        PermissionError,
        RunnerError,
        YAMLError,
    ) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/channels/{channel}/packages/preview")
def preview_package(channel: str, request: PackagePreviewRequest):
    from state import require_channel
    from package_approval import preview

    return _user_operation(preview, require_channel(channel), request)[0]


@router.post("/api/channels/{channel}/packages/add")
def add_package(channel: str, request: PackageAddRequest):
    from state import require_channel
    from package_approval import add_to

    return _user_operation(add_to, require_channel(channel), request)


class HandlerBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str


class HandlerAcceptRequest(HandlerBuildRequest):
    expected_content_hash: str


@router.post("/api/channels/{channel}/cells/{cell_id}/handler-build/inspect")
def inspect_handler_build(channel: str, cell_id: str, request: HandlerBuildRequest):
    from state import require_channel
    from handler_build import inspect_build

    return _user_operation(
        inspect_build, require_channel(channel), cell_id, request.folder
    )[0]


@router.post("/api/channels/{channel}/cells/{cell_id}/handler-build/accept")
def accept_handler_build(channel: str, cell_id: str, request: HandlerAcceptRequest):
    from state import require_channel
    from handler_build import accept_build

    return _user_operation(
        accept_build,
        require_channel(channel),
        cell_id,
        request.folder,
        request.expected_content_hash,
    )


@router.get("/api/channels/{channel}/cells/{cell_id}/validation")
def current_validation(channel: str, cell_id: str):
    from fastapi import HTTPException
    from user_validation import read_user_validation
    try:
        return read_user_validation(channel, cell_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
