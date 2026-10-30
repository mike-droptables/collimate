"""D23 live oracle: full native records, not truncated display strings."""
from copy import deepcopy
import json

import pytest

from tests_live.test_live_generator_tool import CHILD_PATH, _tool_call


def _records(native):
    settings = {'overlay': 'child-value', 'marker': 'x' * 180,
                'parent_run_id': 'actual-parent', 'cell_id': 'actual-cell'}
    prompt = 'private-prompt-after-display-boundary'
    arguments = {'path': CHILD_PATH, 'settings': settings, 'prompt': prompt}
    name = f'collimate: test generator · {CHILD_PATH}' if native else 'test_generator'
    output = {'exit_code': 0, 'output': 'dependency-log\n' * 100 + 'child-committed',
              'result': {'success': True}}
    encoded = json.dumps(output) if native else str(output)
    frames = [
        {'t': 'tool', 'id': 'native-call-1', 'name': name, 'status': 'running',
         'input': json.dumps(arguments)[:199] + '…'},
        {'t': 'tool', 'id': 'native-call-1', 'name': name, 'status': 'done',
         'result': json.dumps(output)[:499] + '…'},
    ]
    trace = [{'type': 'tool_use', 'tool': name, 'input': arguments if native else json.dumps(arguments)},
             {'type': 'tool_result', 'output': encoded}]
    if not native:
        trace[-1]['tool'] = name
    return frames, settings, prompt, trace, output


@pytest.mark.parametrize('native', [False, True])
def test_full_tool_trace_survives_bounded_input_and_result_D23(native):
    frames, settings, prompt, trace, output = _records(native)
    assert prompt not in frames[0]['input']
    assert 'success' not in frames[1]['result']
    assert _tool_call(frames, settings, prompt, trace, 1) == output


@pytest.mark.parametrize('mutation', ['hidden_input', 'result_id', 'extra_id',
                                     'wrong_name', 'wrong_target', 'wrong_tool_with_target', 'extra_suffix',
                                     'extra_trace_call', 'trace_name', 'result_name'])
def test_full_tool_oracle_rejects_misattributed_or_modified_records_D23(mutation):
    frames, settings, prompt, trace, _output = _records(True)
    if mutation == 'hidden_input':
        trace[0]['input']['prompt'] = 'different-private-prompt'
    elif mutation == 'result_id':
        frames[1]['id'] = 'another-call'
    elif mutation == 'extra_id':
        frames.append({**frames[0], 'id': 'additional-call'})
    elif mutation == 'wrong_name':
        for row in frames:
            row['name'] = 'run_bash'
    elif mutation in {'wrong_target', 'wrong_tool_with_target', 'extra_suffix'}:
        name = {
            'wrong_target': 'collimate: test generator · draft/another.generator',
            'wrong_tool_with_target': f'collimate: run bash · {CHILD_PATH}',
            'extra_suffix': f'collimate: test generator · {CHILD_PATH} · extra',
        }[mutation]
        # Mutate every attribution consistently: only the exact title check
        # can refuse these, rather than a mismatch between frames and trace.
        for row in frames:
            row['name'] = name
        trace[0]['tool'] = name
        trace[-1]['tool'] = name
    elif mutation == 'extra_trace_call':
        trace += deepcopy(trace)
    elif mutation == 'trace_name':
        trace[0]['tool'] = 'another_tool'
    elif mutation == 'result_name':
        trace[-1]['tool'] = 'run_bash'
    with pytest.raises(AssertionError):
        _tool_call(frames, settings, prompt, trace, 1)
