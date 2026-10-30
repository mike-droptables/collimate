# /// script
# requires-python = ">=3.13"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""D23 live fixture: executed only by the real test_generator child command."""
import hashlib
import json
import os
from pathlib import Path
import socket

import gen
import yaml


async def run():
    settings = gen.config('main.set_yaml')
    info = gen.run_info()
    assert yaml.__version__ == '6.0.3'
    assert settings['keep'] == 'base-value'
    assert settings['overlay'] == 'child-value'
    assert info.run_id == settings['parent_run_id']
    assert info.cell_id == settings['cell_id']
    assert gen.prompt == settings['expected_prompt']
    assert gen.grant().commands == settings['commands']
    host = settings.get('execution_locus', 'container') == 'host'
    assert settings.get('execution_locus', 'container') in ('host', 'container')
    if host:
        assert gen.grant().docker == 'none'
    else:
        assert gen.grant().docker == 'full'
    assert gen.workspace_path() == settings['expected_mount']
    if host:
        from collimate.testing import forward
        sdk_forward = Path(forward.__file__).resolve()
        assert sdk_forward.is_file()
        assert sdk_forward == Path(settings['expected_sdk']) / 'collimate/testing/forward.py'
        assert os.environ['PYTHONPATH'] == settings['expected_sdk']
    else:
        assert Path('/opt/collimate-sdk/collimate/testing/forward.py').is_file()
    private = Path(os.environ['COLLIMATE_RUN_DIR'])
    assert private.parent == Path(gen.workspace_path()) / '.collrun'
    assert private.name.startswith('forward-')
    assert (private / 'prompt.md').read_text() == gen.prompt
    assert not any('forward-' in name for name in gen.list_files())
    # A forwarded child has no annotation context: its relay carries none
    # of the annotation ops.
    assert gen.annotations.available is False
    refused = []
    for name, call in [
        ('account', lambda: gen.account('model')),
    ]:
        try:
            call()
        except Exception as exc:
            assert 'forward_refused' in str(exc), (name, type(exc).__name__, str(exc))
            refused.append(name)
        else:
            raise AssertionError(name + ' escaped the fixed relay')
    # This body must be rejected by the actual built-in options verifier. A
    # valid sibling in that same flush must still be committed.
    report = {
        'marker': settings['marker'], 'run_id': info.run_id, 'cell_id': info.cell_id,
        'commands': gen.grant().commands, 'docker': gen.grant().docker,
        'hostname': socket.gethostname(), 'workspace': gen.workspace_path(),
        'private_prompt_hash': hashlib.sha256(gen.prompt.encode()).hexdigest(),
        'overlay': settings['overlay'], 'keep': settings['keep'],
        'refused': refused, 'dependency': yaml.__version__, 'verifier_refused': False,
    }
    if host:
        report.update(execution_locus='host', sdk_forward=str(sdk_forward),
                      pid=os.getpid(), pgrp=os.getpgrp(),
                      host_start=Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19])
    gen.update_file('child-proof.json', json.dumps(report, sort_keys=True))
    gen.update_file('child-invalid.options_stack', 'options: [unclosed')
    try:
        await gen.commit()
    except PermissionError as exc:
        assert 'child-invalid.options_stack' in str(exc)
        report['verifier_refused'] = True
    else:
        raise AssertionError('Host accepted the invalid typed child write')
    gen.delete_file('child-invalid.options_stack')
    gen.update_file('child-proof.json', json.dumps(report, sort_keys=True))
    await gen.commit()
    print('D23-CHILD-COMMITTED ' + settings['marker'])


gen.main(run)
