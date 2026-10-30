"""Actual API + built UI with isolated synthetic renderer rollout resources.

No renderer source/cache/CDN route is replaced. Local replay sockets exercise
the shipped UI protocol without claiming generator/provider integration.
"""
from __future__ import annotations

import argparse
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import wave


def main() -> None:
    repo = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5113)
    parser.add_argument('--page-port', type=int, default=5114)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--static', type=Path, required=True)
    args = parser.parse_args()
    data, static = args.data.resolve(), args.static.resolve()
    disposable = (repo / 'tmp/p3', Path(tempfile.gettempdir()).resolve())
    if not any(data.is_relative_to(root) for root in disposable):
        parser.error('data must be disposable: under repo tmp/p3 or the system temp dir')
    if not (static / 'index.html').is_file():
        parser.error('static must contain a built frontend index.html')
    data.parent.mkdir(parents=True, exist_ok=True)
    os.environ['PYTHON_KEYRING_BACKEND'] = 'keyring.backends.null.Keyring'
    os.environ['COLLIMATE_ACCOUNTS_CONFIG'] = str(data.parent / 'synthetic-accounts.json')
    os.environ['TMPDIR'] = str(repo / 'tmp/p3/tmp')
    sys.path.insert(0, str(repo / 'api'))
    import main as api_main
    import yaml
    from collimate.definitions import discover_definitions
    from fastapi import WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, Response
    from fastapi.staticfiles import StaticFiles

    # FastAPI evaluates endpoint annotations against module globals.
    globals()['WebSocket'] = WebSocket
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\0\0' * 8000)
    video = data.parent / 'fixture.webm'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
        '-i', 'color=c=blue:s=32x32:d=1', '-c:v', 'libvpx', '-an', str(video),
    ], check=True, timeout=30)

    class FixturePage(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/page':
                self.send_error(404)
                return
            body = b'<!doctype html><h1>P3 local page ready</h1>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    page_server = ThreadingHTTPServer(('127.0.0.1', args.page_port), FixturePage)
    page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
    page_thread.start()

    @api_main.app.get('/__renderer_rollout/config')
    async def fixture_config():
        return {'page_url': f'http://127.0.0.1:{args.page_port}/page'}

    @api_main.app.get('/__renderer_rollout/page')
    async def fixture_page():
        return HTMLResponse('<!doctype html><h1>P3 local page ready</h1>')

    @api_main.app.get('/__renderer_rollout/roster')
    async def fixture_roster():
        result = []
        cells = [cell for tree in ('content/renderers', 'builtins') for cell in (repo / tree).iterdir()]
        for cell in sorted(cells):
            if not cell.is_dir():
                continue
            found = discover_definitions(
                p.relative_to(cell).as_posix() for p in cell.rglob('*') if p.is_file()
            )
            if found.problems:
                raise RuntimeError(str(found.problems))
            for package in found.packages:
                if package.kind == 'renderer':
                    definition = yaml.safe_load((cell / package.definition_path).read_text())
                    result.append({key: definition[key] for key in ('id', 'embed', 'entrypoint')})
        return result

    @api_main.app.get('/__renderer_rollout/media/{kind}')
    async def fixture_media(kind: str):
        if kind == 'audio':
            return Response(audio.getvalue(), media_type='audio/wav')
        if kind == 'video':
            return Response(video.read_bytes(), media_type='video/webm')
        return Response(status_code=404)

    @api_main.app.websocket('/__renderer_rollout/terminal')
    async def terminal_replay(ws: WebSocket):
        await ws.accept()
        await ws.send_bytes(b'P3 terminal replay ready\r\n')
        try:
            while True:
                if (await ws.receive())['type'] == 'websocket.disconnect':
                    break
        except WebSocketDisconnect:
            pass

    # Test endpoints must precede main's root static mount; no production
    # route, middleware or source response is replaced.
    probes = [r for r in api_main.app.routes
              if getattr(r, 'path', '').startswith('/__renderer_rollout/')]
    api_main.app.routes[:] = [r for r in api_main.app.routes if r not in probes]
    static_at = next((i for i, r in enumerate(api_main.app.routes)
                      if getattr(r, 'name', None) == 'static'), len(api_main.app.routes))
    api_main.app.routes[static_at:static_at] = probes

    # Explicit static root avoids any shared ignored api/static mutation.
    api_main._static_dir = str(static)
    api_main._INDEX_PATH = str(static / 'index.html')
    mounted = False
    for route in api_main.app.routes:
        if getattr(route, 'name', None) == 'static':
            route.app = StaticFiles(directory=str(static), html=True)
            mounted = True
    if not mounted:
        # A checkout without a built api/static gets no index route or root
        # mount from main; serve the given build the way main would.
        @api_main.app.get('/', include_in_schema=False)
        @api_main.app.get('/index.html', include_in_schema=False)
        async def index_with_config():
            return HTMLResponse(api_main._render_index_with_runtime_config())

        api_main.app.mount('/', StaticFiles(directory=str(static), html=True), name='static')
    sys.argv = ['renderer_rollout_server', '--port', str(args.port), '--data-dir', str(data)]
    try:
        api_main.cli_main()
    finally:
        page_server.shutdown()
        page_server.server_close()
        page_thread.join(timeout=5)


if __name__ == '__main__':
    main()
