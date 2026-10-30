"""The module mirror's route: ``GET /api/mirror/{host}/{path}``.

Serves ``https://<host>/<path>[?<query>]`` for the allowlisted module CDNs
(``module_mirror``). The key comes from the request's raw path and query,
never the decoded ``path``, so an escaped and an unescaped spelling of one
URL share a key and the query is kept as sent. The body goes out with the
exact upstream Content-Type, ``ETag: "<sha256>"`` (a matching
``If-None-Match`` answers 304), ``Cache-Control: public, max-age=31536000,
immutable`` for an exact key and ``no-cache`` for a floating one, and
``nosniff``. A refusal is ``{error, detail}`` with ``no-store``.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import module_mirror
from collimate._host.module_mirror import InvalidKey, is_floating, mirror_key
from module_mirror import MirrorError

router = APIRouter()

_PREFIX = "/api/mirror/"
_IMMUTABLE = "public, max-age=31536000, immutable"


def _raw_key(request: Request) -> str:
    raw = request.scope.get("raw_path")
    path = raw.decode("ascii") if isinstance(raw, bytes) else request.url.path
    if not path.startswith(_PREFIX):
        raise InvalidKey("not a mirror path")
    return mirror_key(path[len(_PREFIX):], request.scope.get("query_string", b"").decode("ascii"))


def _headers(key: str, digest: str) -> dict[str, str]:
    return {
        "ETag": f'"{digest}"',
        "Cache-Control": "no-cache" if is_floating(key) else _IMMUTABLE,
        "X-Content-Type-Options": "nosniff",
    }


def _matches(if_none_match: str | None, digest: str) -> bool:
    if not if_none_match:
        return False
    tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return "*" in tags or f'"{digest}"' in tags


def _refusal(error: MirrorError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content=error.body(),
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get(
    "/api/mirror/{host}/{path:path}",
    response_class=Response,
    responses={
        200: {"description": "The module's bytes, with the upstream Content-Type"},
        304: {"description": "Not modified (If-None-Match)"},
        400: {"description": "`{error: 'invalid', detail}`: not a mirrorable URL"},
        404: {"description": "`{error: 'upstream', status, detail}`: upstream has no such file"},
        502: {"description": "`{error: 'upstream' | 'snapshot', detail}`: upstream failed or a vendored module changed"},
    },
)
async def get_mirror_module(host: str, path: str, request: Request) -> Response:
    """One module of an allowlisted CDN (``collimate._host.module_mirror.HOSTS``),
    from the shipped snapshot, the store, or upstream on a miss."""
    try:
        key = _raw_key(request)
    except (InvalidKey, UnicodeDecodeError) as error:
        return _refusal(MirrorError(400, "invalid", str(error)))
    if_none_match = request.headers.get("if-none-match")
    known = module_mirror.known_digest(key)
    if known is not None and _matches(if_none_match, known):
        return Response(status_code=304, headers=_headers(key, known))
    try:
        module = await module_mirror.get_module(key)
    except MirrorError as error:
        return _refusal(error)
    headers = _headers(key, module.digest)
    if _matches(if_none_match, module.digest):
        return Response(status_code=304, headers=headers)
    return Response(content=module.body, headers={"Content-Type": module.media_type, **headers})
