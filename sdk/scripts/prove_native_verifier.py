#!/usr/bin/env python3
"""Execute installed private engine, bundler limits and external watchdog contracts."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from collimate._host.verifier_engine import context_type, native_target, payload_directory, verify_payload
from collimate._host.verification import run_verifier


def main():
    distribution = importlib.metadata.distribution('collimate-sdk')
    requirements = distribution.requires or []
    assert not any('quickjs' in requirement.lower() for requirement in requirements)
    directory = payload_directory(native_target())
    assert directory.parent.name == 'native_verifier', 'proof must use an installed wheel, not source provisioning'
    record = verify_payload(directory, native_target())
    assert context_type()().eval('2+2') == 4
    assert Path(sys.modules['collimate._quickjs'].__file__).parent.resolve() == directory.resolve()
    def run(body, data=None):
        return run_verifier('var __collimate_verifier={default:function(input){'+body+'}};',
                            data or {}, deadline=time.monotonic()+5)
    assert run('return {valid:true}') == {'valid': True}
    assert run('return {valid:false,error:"invalid"}') == {'valid': False, 'error': 'invalid'}
    assert run('return {valid:typeof process==="undefined" && typeof fetch==="undefined"}') == {'valid': True}
    assert run('new ArrayBuffer(70*1024*1024);return {valid:true}')['valid'] is False
    assert run('function f(){f()};f();return {valid:true}')['valid'] is False
    source = 'export default {__collimate_renderer__:true,component:()=>null}'
    assert run('const r=__collimate_bundle(input.files,input.entrypoint);return {valid:typeof r.entrypoint_js==="string"}',
               {'files': {'main.tsx': source}, 'entrypoint': 'main.tsx'}) == {'valid': True}
    child = subprocess.Popen([sys.executable, '-c',
        'import time;from collimate._host.verification import run_verifier;'
        'run_verifier("var __collimate_verifier={default:()=>{while(true){}}};",{},deadline=time.monotonic()+0.1)'],
        env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        try:
            child.communicate(timeout=0.4)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError('Hostile actual verifier did not run until the external watchdog')
    finally:
        child.kill()
        child.communicate(timeout=3)
    assert child.returncode is not None
    print(json.dumps({'python': sys.version, 'target': native_target(), 'binding': record['binding_version'],
                      'binary_sha256': record['files']['_quickjs.abi3.so'], 'wheel_version': distribution.version,
                      'verifier_limits_bundle_watchdog': 'passed'}, sort_keys=True))


if __name__ == '__main__':
    main()
