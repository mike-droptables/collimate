# SPDX-License-Identifier: MIT
"""A run's live options: ``gen.publish_options`` sends the whole set in the
run-option shape (``collimate._host.ipc.normalize_run_options``) and
``gen.pop_option_change`` takes the user's changes to it, oldest first.
The host-free bridge records the sets and scripts the changes as the host
takes them (``RecordingBridge.change_option``)."""
from __future__ import annotations

import pytest

import gen
from collimate._host import ipc
from collimate._host.bridge import HostBridge

SET = [
    {"id": "speed", "name": "Speed", "category": "model", "type": "select",
     "currentValue": "slow", "options": [{"value": "slow", "name": "Slow"},
                                         {"value": "fast", "name": "Fast", "description": "Quick"}]},
    {"id": "loud", "name": "Loud", "description": "", "type": "boolean", "currentValue": False},
]


def test_a_set_is_published_whole_in_the_run_option_shape(native_bridge):
    gen.publish_options([{**SET[0], "group": "x"}, SET[1]])
    gen.publish_options([])
    assert native_bridge.published_options == [
        [SET[0], {"id": "loud", "name": "Loud", "type": "boolean", "currentValue": False}],
        [],
    ]


@pytest.mark.parametrize("bad, message", [
    ({"id": "x"}, "must be a list"),
    ([{"id": "x", "name": "X", "type": "radio", "currentValue": "a"}], "select or boolean"),
    ([{"id": "x", "name": "", "type": "boolean", "currentValue": True}], "name"),
    ([{"id": "x", "name": "X", "type": "select", "currentValue": 1,
       "options": [{"value": "a", "name": "A"}]}], "currentValue"),
    ([{"id": "x", "name": "X", "type": "select", "currentValue": "a",
       "options": [{"value": "", "name": "A"}]}], "value, name"),
    ([SET[1], SET[1]], "used twice"),
    ([{"id": f"o{i}", "name": "O", "type": "boolean", "currentValue": True}
      for i in range(ipc.RUN_OPTIONS_MAX + 1)], "at most"),
])
def test_a_malformed_set_raises_before_anything_is_sent(native_bridge, bad, message):
    with pytest.raises(ValueError, match=message):
        gen.publish_options(bad)
    assert native_bridge.published_options == []


def test_changes_arrive_oldest_first_and_only_ones_the_set_takes(native_bridge):
    assert gen.pop_option_change() is None
    gen.publish_options(SET)
    native_bridge.change_option("speed", "fast")
    native_bridge.change_option("loud", True)
    for refused in (("volume", 1), ("speed", "warp"), ("loud", "true")):
        with pytest.raises(ValueError):
            native_bridge.change_option(*refused)
    assert gen.pop_option_change() == {"id": "speed", "value": "fast"}
    assert gen.pop_option_change() == {"id": "loud", "value": True}
    assert gen.pop_option_change() is None


def test_a_new_set_drops_the_changes_it_no_longer_takes(native_bridge):
    gen.publish_options(SET)
    native_bridge.change_option("speed", "fast")
    native_bridge.change_option("loud", True)
    gen.publish_options([{**SET[0], "options": SET[0]["options"][:1]}, SET[1]])
    assert gen.pop_option_change() == {"id": "loud", "value": True}
    assert gen.pop_option_change() is None


def test_the_host_bridge_speaks_the_two_ops():
    bridge = HostBridge.__new__(HostBridge)
    sent: list = []
    answers = iter([{"id": "speed", "value": "fast"}, {"id": None}])
    bridge.emit = sent.append
    bridge.emit_and_wait = lambda msg, *, wait_op: (sent.append((msg, wait_op)), next(answers))[1]
    bridge.publish_options(SET)
    assert bridge.poll_option_change() == {"id": "speed", "value": "fast"}
    assert bridge.poll_option_change() is None
    assert sent == [{"op": "publish_options", "options": SET},
                    ({"op": "poll_option_change"}, "ack_poll_option_change"),
                    ({"op": "poll_option_change"}, "ack_poll_option_change")]
    assert ipc.OPS["publish_options"].kind == "notify"
    assert ipc.OPS["poll_option_change"].kind == "request"
