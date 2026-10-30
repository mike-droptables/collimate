"""Retained Pi native rejection shape; no model, Docker or account lookup."""
from copy import deepcopy

import pytest

from tests_live.test_live_pi_commands import terminal_turn_finished

COMMAND = 'sha256sum commands-probe.txt | tee /workspace/commands-probe.ran'
DIGEST = 'f' * 64
# Exact original native call/result/final-stop shape, excluding thinking/usage.
OBSERVED_MESSAGES = [{'role': 'assistant', 'stopReason': 'toolUse', 'content': [{'type': 'text', 'text': 'Let me run the command:\n\n'}, {'type': 'toolCall', 'id': 'call_5460bcc7bf03413ebadef085', 'name': 'bash', 'arguments': {'command': 'sha256sum commands-probe.txt | tee /workspace/commands-probe.ran', 'timeout': 10}}]}, {'role': 'toolResult', 'toolCallId': 'call_5460bcc7bf03413ebadef085', 'isError': True, 'content': [{'type': 'text', 'text': 'collimate: the user did not approve this command'}]}, {'role': 'assistant', 'stopReason': 'stop', 'content': [{'type': 'text', 'text': 'The sandbox blocked execution of `sha256sum commands-probe.txt | tee /workspace/commands-probe.ran`. \n\nMay I have your permission to run this command outside the sandbox? If you approve, please grant the execution permission so I can proceed and return the output.'}]}]

def test_native_completed_rejection_does_not_need_literal_model_prose():
    assert terminal_turn_finished(OBSERVED_MESSAGES, DIGEST, rejected=True, command=COMMAND)


@pytest.mark.parametrize('damage', [
    'missing_result', 'mismatched_id', 'not_error', 'missing_completed_turn',
    'wrong_command', 'wrong_refusal', 'turn_still_running', 'result_before_call',
])
def test_rejection_completion_requires_typed_native_evidence(damage):
    messages = deepcopy(OBSERVED_MESSAGES)
    if damage == 'missing_result':
        messages.pop(1)
    elif damage == 'mismatched_id':
        messages[1]['toolCallId'] = 'unrelated-native-call'
    elif damage == 'not_error':
        messages[1]['isError'] = False
    elif damage == 'missing_completed_turn':
        messages.pop()
    elif damage == 'wrong_command':
        messages[0]['content'][1]['arguments']['command'] = 'echo unrelated'
    elif damage == 'wrong_refusal':
        messages[1]['content'][0]['text'] = 'Executable missing'
    elif damage == 'turn_still_running':
        messages[2]['stopReason'] = 'toolUse'
    elif damage == 'result_before_call':
        messages[:2] = messages[1::-1]
    assert not terminal_turn_finished(messages, DIGEST, rejected=True, command=COMMAND)


def test_model_refusal_prose_alone_cannot_prove_native_rejection():
    messages = [{'role': 'assistant', 'stopReason': 'stop',
                 'content': [{'type': 'text', 'text': 'NO-SHELL'}]}]
    assert not terminal_turn_finished(messages, DIGEST, rejected=True, command=COMMAND)


@pytest.mark.parametrize('text', [DIGEST, 'NO-SHELL'])
def test_other_grant_completion_retains_original_literal_condition(text):
    assert terminal_turn_finished([{'role': 'assistant',
        'content': [{'type': 'text', 'text': text}]}], DIGEST)
