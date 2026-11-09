"""Pure receipt helpers for the isolated P5 service comparison."""

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import subprocess


DEVELOPMENT = Path(__file__).resolve().parents[3]


def percentiles(values):
    if not values:
        raise ValueError('A reported workload has no observations')
    ordered = sorted(values)
    return {name: ordered[math.ceil(len(ordered) * fraction) - 1]
            for name, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]} | {'count': len(values)}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        while part := source.read(1024 * 1024):
            digest.update(part)
    return digest.hexdigest()


def source_manifest(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Frozen source manifest is required: {path}')
    document = json.loads(path.read_text())
    files = document['files']
    if not isinstance(files, dict) or not files:
        raise ValueError('Frozen source manifest must have a nonempty files mapping')
    calculated = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    mismatches = {}
    for relative, expected in files.items():
        target = (DEVELOPMENT / relative).resolve()
        if not target.is_relative_to(DEVELOPMENT) or not target.is_file():
            mismatches[relative] = 'missing or outside development source tree'
        else:
            actual = sha256_file(target)
            if actual != expected:
                mismatches[relative] = {'expected': expected, 'actual': actual}
    return {'path': str(path), 'sha256': sha256_file(path),
            'declaredManifestSha256': document.get('manifest_sha256'),
            'calculatedManifestSha256': calculated, 'fileCount': len(files),
            'mismatches': mismatches,
            'verified': document.get('manifest_sha256') == calculated and not mismatches}


def instrumentation_hashes(paths):
    return {str(Path(path).resolve()): sha256_file(path) for path in paths}


def _command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    return {'exitCode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def environment(directory):
    directory = Path(directory).resolve()
    cpu_model = None
    cpuinfo = Path('/proc/cpuinfo')
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(errors='replace').splitlines():
            if line.startswith('model name'):
                cpu_model = line.partition(':')[2].strip()
                break
    mount = _command('findmnt', '--json', '--target', str(directory),
                     '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS')
    storage = _command('lsblk', '--json', '--output', 'NAME,TYPE,MODEL,ROTA,TRAN,SIZE,FSTYPE,MOUNTPOINTS')
    return {'cpu': {'model': cpu_model, 'logicalCount': os.cpu_count(),
                    'machine': platform.machine()},
            'os': {'platform': platform.platform(), 'release': platform.release(),
                   'python': platform.python_version()},
            'filesystem': mount, 'storage': storage,
            'sqlite': {'libraryVersion': sqlite3.sqlite_version,
                       'pythonModuleVersion': sqlite3.version},
            'browser': None, 'directory': str(directory)}
