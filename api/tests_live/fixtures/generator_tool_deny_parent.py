"""Test-only real app parent; imports byte-identical accepted agent tool code."""
import hashlib
import importlib
import json
from pathlib import Path

import gen

generator_test = importlib.import_module('generator_test')


async def run():
    source_path = generator_test.__file__
    assert source_path is not None
    assert gen.grant().commands == 'deny'
    rows = generator_test.entries(commands=gen.grant().commands,
                                  context=generator_test.CommandContext('host'))
    assert rows == []
    result = await generator_test.run_test(generator_test.CommandContext('host'),
        commands=gen.grant().commands, path='draft/probe.generator', settings={}, prompt='denied')
    assert result == {'error': 'commands_denied'}
    assert not list(Path(gen.workspace_path(), '.collrun').glob('forward-*'))
    assert not gen.has_file('child-was-run.txt')
    gen.update_file('denied-proof.json', json.dumps({
        'run_id': gen.run_info().run_id, 'commands': gen.grant().commands,
        'tool_names': [row[0] for row in rows], 'direct_result': result,
        'source_hash': hashlib.sha256(Path(source_path).read_bytes()).hexdigest(),
        'private_children': [],
    }))
    await gen.commit()


gen.main(run)
