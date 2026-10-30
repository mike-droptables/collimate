"""Bounded Python transport for the exact pinned esbuild service protocol.

All module contents arrive through virtual plugin callbacks. No filesystem
resolver, CLI fallback, environment override or runtime download is available.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import time

MAX_FRAME = 64 * 1024**2
MAX_BYTES = 32 * 1024**2
MAX_ITEMS = 100_000
MAX_DEPTH = 64


class ServiceError(ValueError):
    pass


def encode(value, depth=0):
    if depth > MAX_DEPTH:
        raise ServiceError('Compiler protocol nesting exceeds limit')
    if value is None:
        return b'\0'
    if isinstance(value, bool):
        return b'\1' + bytes([value])
    if isinstance(value, int) and -(2**31) <= value < 2**31:
        return b'\2' + struct.pack('<i', value)
    if isinstance(value, (str, bytes)):
        raw = value.encode('utf-8') if isinstance(value, str) else value
        if len(raw) > MAX_FRAME:
            raise ServiceError('Compiler protocol value exceeds limit')
        return (b'\3' if isinstance(value, str) else b'\4') + struct.pack('<I', len(raw)) + raw
    if isinstance(value, (list, dict)):
        if len(value) > MAX_ITEMS:
            raise ServiceError('Compiler protocol collection exceeds limit')
        if isinstance(value, list):
            return b'\5' + struct.pack('<I', len(value)) + b''.join(encode(v, depth + 1) for v in value)
        parts = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ServiceError('Compiler protocol map key is not text')
            key = key.encode('utf-8')
            parts.append(struct.pack('<I', len(key)) + key + encode(item, depth + 1))
        return b'\6' + struct.pack('<I', len(value)) + b''.join(parts)
    raise ServiceError('Unsupported compiler protocol value')


def decode(raw: bytes):
    if len(raw) > MAX_FRAME:
        raise ServiceError('Compiler protocol frame exceeds limit')
    pos = 0
    items = 0

    def take(n):
        nonlocal pos
        if n > len(raw) - pos:
            raise ServiceError('Truncated compiler protocol value')
        value = raw[pos:pos+n]
        pos += n
        return value

    def integer():
        return struct.unpack('<I', take(4))[0]

    def string():
        return take(integer()).decode('utf-8')

    def value(depth):
        nonlocal items
        items += 1
        if depth > MAX_DEPTH or items > MAX_ITEMS:
            raise ServiceError('Compiler protocol complexity exceeds limit')
        tag = take(1)[0]
        if tag == 0:
            return None
        if tag == 1:
            flag = take(1)[0]
            if flag > 1:
                raise ServiceError('Invalid compiler protocol boolean')
            return bool(flag)
        if tag == 2:
            return struct.unpack('<i', take(4))[0]
        if tag == 3:
            return string()
        if tag == 4:
            return take(integer())
        if tag in (5, 6):
            size = integer()
            if size > MAX_ITEMS:
                raise ServiceError('Compiler protocol collection exceeds limit')
            if tag == 5:
                return [value(depth + 1) for _ in range(size)]
            result = {}
            for _ in range(size):
                key = string()
                if key in result:
                    raise ServiceError('Duplicate compiler protocol key')
                result[key] = value(depth + 1)
            return result
        raise ServiceError('Unknown compiler protocol tag')

    header = integer()
    result = value(0)
    if pos != len(raw):
        raise ServiceError('Trailing compiler protocol bytes')
    return header >> 1, bool(header & 1), result


class Service:
    def __init__(self, executable, version, deadline, own_process_group):
        self.deadline = deadline
        self.own_process_group = own_process_group
        self.temp = tempfile.TemporaryDirectory(prefix='collimate-compiler-')
        self.selector = selectors.DefaultSelector()
        self.received = bytearray()
        self.outgoing = bytearray()
        self.stderr = bytearray()
        self.total_output = 0
        self.next_request = 0
        self.process = None
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-I', str(Path(__file__).with_name('_compiler_child.py')), str(executable), version],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.temp.name, env={'PATH': '', 'LANG': 'C.UTF-8'},
                start_new_session=own_process_group, close_fds=True,
            )
            for stream, label in ((self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, label)
            os.set_blocking(self.process.stdin.fileno(), False)
            if self.frame() != version.encode('ascii'):
                raise ServiceError('Native compiler handshake version mismatch')
        except BaseException:
            self.close()
            raise

    def close(self):
        p = self.process
        if p is not None:
            if p.poll() is None:
                try:
                    if self.own_process_group:
                        os.killpg(p.pid, signal.SIGKILL)
                    else:
                        p.kill()
                except ProcessLookupError:
                    pass
            p.wait()
            for stream in (p.stdin, p.stdout, p.stderr):
                if stream:
                    stream.close()
        self.selector.close()
        self.temp.cleanup()

    def frame(self):
        while True:
            if time.monotonic() >= self.deadline:
                raise ServiceError('Native compiler deadline exceeded')
            if len(self.received) >= 4:
                size = struct.unpack_from('<I', self.received)[0]
                if size > MAX_FRAME:
                    raise ServiceError('Compiler protocol frame exceeds limit')
                if len(self.received) >= size + 4:
                    frame = bytes(self.received[4:4+size])
                    del self.received[:4+size]
                    return frame
            ready = self.selector.select(max(0, self.deadline - time.monotonic()))
            if not ready:
                raise ServiceError('Native compiler deadline exceeded')
            for key, mask in ready:
                if key.data == 'stdin':
                    try:
                        count = os.write(key.fd, self.outgoing[:65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError as exc:
                        raise ServiceError('Native compiler closed input') from exc
                    del self.outgoing[:count]
                    if not self.outgoing:
                        self.selector.unregister(key.fileobj)
                    continue
                try:
                    block = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not block:
                    self.selector.unregister(key.fileobj)
                    if key.data == 'stdout':
                        raise ServiceError('Native compiler stopped before completing request')
                    continue
                self.total_output += len(block)
                if self.total_output > MAX_FRAME * 2:
                    raise ServiceError('Compiler protocol output exceeds limit')
                if key.data == 'stdout':
                    self.received.extend(block)
                else:
                    self.stderr.extend(block)
                    del self.stderr[:-8192]

    def send(self, identifier, response, value):
        packet = struct.pack('<I', (identifier << 1) | response) + encode(value)
        if len(packet) > MAX_FRAME or len(self.outgoing) + len(packet) > MAX_FRAME:
            raise ServiceError('Compiler protocol input exceeds limit')
        self.outgoing.extend(struct.pack('<I', len(packet)) + packet)
        try:
            self.selector.get_key(self.process.stdin)
        except KeyError:
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, 'stdin')

    def request(self, value, callback=None):
        request_id = self.next_request
        self.next_request += 1
        self.send(request_id, False, value)
        callbacks = 0
        while True:
            identifier, response, result = decode(self.frame())
            if not isinstance(result, dict):
                raise ServiceError('Compiler response is not an object')
            if response:
                if identifier != request_id:
                    raise ServiceError('Compiler response identifier mismatch')
                if result.get('error'):
                    raise ServiceError(str(result['error'])[:4096])
                errors = result.get('errors', [])
                if not isinstance(errors, list) or any(not isinstance(e, dict) or not isinstance(e.get('text'), str) for e in errors):
                    raise ServiceError('Malformed compiler diagnostics')
                if errors:
                    raise ServiceError('; '.join(str(e.get('text', 'Compiler error')) for e in errors)[:4096])
                return result
            callbacks += 1
            if callbacks > MAX_ITEMS or callback is None:
                raise ServiceError('Unexpected compiler callback')
            self.send(identifier, True, callback(result))


def compile_map(executable, version, files, entrypoint, policy, deadline, own_process_group, *, verifier=False):
    """Compile exactly this virtual graph and return module plus transform text."""
    from ._host.renderer_bundle import validate_path
    namespace = 'collimate-verifier' if verifier else 'collimate-package'

    def reply_error(identifier, message):
        return {'id': identifier, 'errors': [{'id': '', 'pluginName': namespace, 'text': message[:4096], 'location': None, 'notes': [], 'detail': -1}]}

    def callback(message):
        command = message.get('command')
        if message.get('key') != 0:
            raise ServiceError('Unknown compiler callback build key')
        if command == 'on-start':
            return {'errors': [], 'warnings': []}
        if command == 'on-resolve':
            if message.get('ids') != [0]:
                raise ServiceError('Unexpected compiler resolver callback identity')
            path = message.get('path')
            try:
                if message.get('kind') == 'entry-point':
                    if path != entrypoint or message.get('namespace') != '':
                        raise ValueError('Unexpected compiler entrypoint')
                    return {'id': 0, 'path': entrypoint, 'namespace': namespace}
                if message.get('namespace') != namespace or message.get('importer') not in files:
                    raise ValueError('Import is outside the virtual compiler package')
                if verifier:
                    if path != 'collimate/verify' or message['importer'] != 'verify.ts':
                        raise ValueError('A verifier may import only collimate/verify')
                    resolved = 'prelude.js'
                else:
                    if not isinstance(path, str):
                        raise ValueError('Invalid renderer import')
                    if any(path.startswith(prefix) for prefix in policy['external_prefixes']):
                        return {'id': 0, 'path': path, 'external': True}
                    if not path.startswith(('./', '../')) or '\\' in path or '\0' in path or '_runs' in path.split('/'):
                        raise ValueError(f'Import {path!r} is outside the renderer package')
                    parts = message['importer'].split('/')[:-1]
                    for part in path.split('/'):
                        if not part or part == '.':
                            continue
                        if part == '..' and parts and parts[-1] != '..':
                            parts.pop()
                        else:
                            parts.append(part)
                    joined = validate_path('/'.join(parts))
                    candidates = [joined] + [joined + ext for ext in policy['resolve_extensions']] + [joined + '/index' + ext for ext in policy['resolve_extensions']]
                    resolved = next((name for name in candidates if name in files), None)
                    if resolved is None:
                        raise ValueError(f'Cannot resolve {path!r} inside the renderer package')
                return {'id': 0, 'path': resolved, 'namespace': namespace}
            except ValueError as exc:
                return reply_error(0, str(exc))
        if command == 'on-load':
            if message.get('ids') != [1] or message.get('namespace') != namespace:
                raise ServiceError('Unexpected compiler loader callback identity')
            path = message.get('path')
            if path not in files:
                return reply_error(1, 'Missing virtual compiler file')
            loader = policy['options']['loader'].get(Path(path).suffix)
            if not loader:
                return reply_error(1, f'Unsupported renderer module type: {path}')
            return {'id': 1, 'contents': files[path], 'loader': loader}
        raise ServiceError('Unexpected compiler callback command')

    options = policy['options']
    expected = {'bundle', 'format', 'platform', 'target', 'jsx', 'jsxFactory', 'jsxFragment', 'legalComments', 'minify', 'sourcemap', 'tsconfigRaw', 'loader'}
    if set(options) != expected or options['bundle'] is not True or options['minify'] is not False or options['sourcemap'] is not False or options['tsconfigRaw'] != {}:
        raise ServiceError('Unsupported native compiler contract options')
    flags = ['--bundle', '--format=' + options['format'], '--platform=' + options['platform'], '--target=' + options['target'],
             '--jsx=' + options['jsx'], '--jsx-factory=' + options['jsxFactory'], '--jsx-fragment=' + options['jsxFragment'],
             '--legal-comments=' + options['legalComments'], '--tsconfig-raw={}', '--log-level=silent']
    flags.extend('--loader:' + extension + '=' + loader for extension, loader in options['loader'].items())
    if verifier:
        flags.append('--global-name=__collimate_verifier')
    service = Service(executable, version, deadline, own_process_group)
    try:
        result = service.request({
            'command': 'build', 'key': 0, 'entries': [['', entrypoint]], 'flags': flags,
            'write': False, 'stdinContents': None, 'stdinResolveDir': None,
            'absWorkingDir': service.temp.name, 'nodePaths': [], 'context': False,
            'plugins': [{'name': namespace, 'onStart': False, 'onEnd': False,
                         'onResolve': [{'id': 0, 'filter': '.*', 'namespace': ''}],
                         'onLoad': [{'id': 1, 'filter': '.*', 'namespace': namespace}]}],
        }, callback)
        outputs = result.get('outputFiles')
        if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], dict) or not isinstance(outputs[0].get('contents'), bytes):
            raise ServiceError('Compiler must return exactly one in-memory module')
        raw = outputs[0]['contents']
        if len(raw) > MAX_BYTES:
            raise ServiceError('Compiled module exceeds output limit')
        code = raw.decode('utf-8')
        transformed = ''
        if not verifier:
            loader = options['loader'].get(Path(entrypoint).suffix)
            if loader is None:
                raise ServiceError('Unsupported renderer entrypoint type')
            result = service.request({'command': 'transform', 'inputFS': False, 'input': files[entrypoint],
                'flags': ['--loader=' + loader, '--target=' + options['target'], '--jsx=' + options['jsx'],
                          '--jsx-factory=' + options['jsxFactory'], '--jsx-fragment=' + options['jsxFragment'],
                          '--legal-comments=' + options['legalComments'], '--sourcefile=' + entrypoint, '--log-level=silent']})
            if result.get('codeFS') or result.get('mapFS') or not isinstance(result.get('code'), str):
                raise ServiceError('Compiler transform must return in-memory text')
            transformed = result['code']
            if len(transformed.encode()) > MAX_BYTES:
                raise ServiceError('Compiler transform exceeds output limit')
        return code, transformed
    finally:
        service.close()
