"""Pinned private CPython binding resources; no global engine or compilation fallback."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import stat
import struct
import sys
import sysconfig

CONTRACT = Path(__file__).parent.parent / 'verifier_engine_contract.json'


def contract() -> dict:
    return json.loads(CONTRACT.read_text())


def wheel_payload_targets(host: str) -> frozenset[str]:
    """Installed host packages also supply both Linux container architectures."""
    if host not in contract()['targets']:
        raise ValueError('Unsupported verifier wheel host target')
    return frozenset((host, 'linux-x64', 'linux-arm64'))


def native_target() -> str:
    if (sys.implementation.name != 'cpython' or sys.version_info < (3, 12)
            or sys.version_info >= (4, 0) or sysconfig.get_config_var('Py_GIL_DISABLED')):
        raise RuntimeError('Verifier requires ordinary GIL-enabled CPython 3.12 or later within Python 3')
    machine = platform.machine().lower()
    architecture = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    if not architecture or sys.platform not in {'linux', 'darwin'}:
        raise RuntimeError('Verifier has no prepared binding for this platform')
    if sys.platform == 'linux' and platform.libc_ver()[0] != 'glibc':
        raise RuntimeError('Verifier Linux wheels require glibc; no native source-build fallback')
    if sys.platform == 'darwin' and tuple(map(int, platform.mac_ver()[0].split('.')[:2])) < (11, 0):
        raise RuntimeError('Verifier macOS wheels require macOS 11 or later')
    return f'{sys.platform}-{architecture}'


def verify_binary_target(raw: bytes, target: str) -> None:
    if target.startswith('linux-'):
        architecture = {'linux-x64': 62, 'linux-arm64': 183}[target]
        valid = (len(raw) >= 20 and raw[:6] == b'\x7fELF\x02\x01'
                 and struct.unpack_from('<H', raw, 18)[0] == architecture)
    else:
        architecture = {'darwin-x64': 0x01000007, 'darwin-arm64': 0x0100000C}[target]
        valid = (len(raw) >= 32 and raw[:4] == b'\xcf\xfa\xed\xfe'
                 and struct.unpack_from('<I', raw, 4)[0] == architecture)
    if not valid:
        raise ValueError('Verifier binary format or architecture differs from target')


def verify_payload(directory: Path, target: str) -> dict:
    locked = contract()
    expected = locked['targets'].get(target)
    if expected is None:
        raise ValueError('Unsupported verifier payload target')
    manifest = directory / 'manifest.json'
    if not stat.S_ISREG(manifest.lstat().st_mode):
        raise ValueError('Verifier manifest must be a regular file')
    record = json.loads(manifest.read_text())
    if (set(record) != {'schema', 'binding_version', 'abi', 'target', 'platform_tag', 'source_sha256', 'files'}
            or record['schema'] != 1 or record['binding_version'] != locked['binding_version']
            or record['abi'] != locked['abi'] or record['target'] != target
            or record['platform_tag'] != expected['platform_tag']
            or record['source_sha256'] != locked['source']['sha256']
            or set(record['files']) != {'_quickjs.abi3.so', 'LICENSE.quickjs-ng', 'LICENSE.quickjs-engine'}):
        raise ValueError('Verifier payload metadata differs from its pinned contract')
    pins = {**locked['license_sha256']}
    if 'extension_sha256' in expected:
        pins['_quickjs.abi3.so'] = expected['extension_sha256']
    if any(record['files'][name] != digest for name, digest in pins.items()):
        raise ValueError('Verifier payload hashes differ from its pinned source artifacts')
    for name, digest in record['files'].items():
        path = directory / name
        if not stat.S_ISREG(path.lstat().st_mode) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Verifier payload resource integrity mismatch')
    verify_binary_target((directory / '_quickjs.abi3.so').read_bytes(), target)
    return record


def payload_directory(target: str) -> Path:
    prepared = Path(__file__).parent.parent / 'native_verifier' / target
    if not prepared.is_dir():
        raise RuntimeError('Prepare the SDK private verifier with scripts/prepare_verifier_engine.py')
    return prepared


def context_type():
    target = native_target()
    name = 'collimate._quickjs'
    if name not in sys.modules:
        directory = payload_directory(target)
        verify_payload(directory, target)
        specification = importlib.util.spec_from_file_location(name, directory / '_quickjs.abi3.so')
        if specification is None or specification.loader is None:
            raise RuntimeError('Private verifier extension cannot be loaded')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name].Context
