# SPDX-License-Identifier: MIT
"""Tests for the open transcript format (``collimate.transcript``) — the
OpenAI-compatible messages JSON that replaced the proprietary ``.chatlog``."""
import json

from collimate.transcript import Message
from collimate.transcript import parse_transcript, serialize_transcript


def _pairs(msgs):
    return [(m.role, m.content) for m in msgs]


def test_round_trip():
    msgs = [
        Message(role="system", content="be terse"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    assert _pairs(parse_transcript(serialize_transcript(msgs))) == [
        ("system", "be terse"), ("user", "hi"), ("assistant", "hello"),
    ]


def test_serialize_is_openai_shape():
    text = serialize_transcript([Message(role="user", content="x")])
    data = json.loads(text)
    assert data == {"messages": [{"role": "user", "content": "x"}]}


def test_parse_accepts_bare_array_and_object():
    bare = json.dumps([{"role": "user", "content": "a"}])
    wrapped = json.dumps({"messages": [{"role": "assistant", "content": "b"}]})
    assert _pairs(parse_transcript(bare)) == [("user", "a")]
    assert _pairs(parse_transcript(wrapped)) == [("assistant", "b")]


def test_tolerant_and_role_filtering():
    assert parse_transcript("") == []
    assert parse_transcript("not json") == []
    assert parse_transcript('{"messages": "nope"}') == []
    # Unknown roles dropped — including 'tool', which the renderer and the
    # prompt composer also ignore (one agreed set of visible roles).
    text = json.dumps({"messages": [
        {"role": "bogus", "content": "x"},
        {"role": "tool", "content": "result"},
        {"role": "user", "content": "y"},
    ]})
    assert _pairs(parse_transcript(text)) == [("user", "y")]


def test_null_content_parses_as_empty_not_the_text_none():
    # OpenAI-shaped assistant turns carry "content": null beside tool_calls.
    msgs = parse_transcript('{"messages":[{"role":"assistant","content":null}]}')
    assert msgs[0].content == ""


def test_content_with_newlines_and_unicode():
    msgs = [Message(role="user", content="line1\nline2     end")]
    assert _pairs(parse_transcript(serialize_transcript(msgs))) == _pairs(msgs)


def test_thinking_round_trips_only_when_present():
    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", content="a", thinking="let me reason"),
        Message(role="assistant", content="b"),
    ]
    text = serialize_transcript(msgs)
    data = json.loads(text)
    # Written only when non-empty — plain messages stay the two-key shape.
    assert data["messages"][0] == {"role": "user", "content": "q"}
    assert data["messages"][1] == {
        "role": "assistant", "content": "a", "thinking": "let me reason",
    }
    assert data["messages"][2] == {"role": "assistant", "content": "b"}
    back = parse_transcript(text)
    assert [m.thinking for m in back] == ["", "let me reason", ""]


def test_attachments_round_trip_only_when_present():
    msgs = [
        Message(role="user", content="make this happier",
                attachments=[{"path": "attachments/photo.png",
                              "name": "photo.png", "media_type": "image/png"}]),
        Message(role="assistant", content="done",
                attachments=[{"path": "attachments/out.png",
                              "name": "out.png", "media_type": "image/png"}]),
        Message(role="user", content="plain"),
    ]
    text = serialize_transcript(msgs)
    data = json.loads(text)
    # Written only when present — plain messages keep the two-key shape.
    assert data["messages"][0]["attachments"] == [
        {"path": "attachments/photo.png", "name": "photo.png", "media_type": "image/png"}]
    assert "attachments" not in data["messages"][2]
    back = parse_transcript(text)
    assert back[0].attachments[0]["path"] == "attachments/photo.png"
    assert back[1].attachments[0]["name"] == "out.png"
    assert back[2].attachments == []


def test_attachments_tolerate_malformed_and_absent_key():
    # Non-dict entries and refs without a path are dropped; a text-only message
    # (no attachments key — what both writers emit) parses to empty.
    text = json.dumps({"messages": [
        {"role": "user", "content": "x",
         "attachments": ["nope", {"no": "path"},
                         {"path": "a/b.pdf", "name": "b.pdf", "media_type": "application/pdf"}]},
        {"role": "user", "content": "legacy"},
    ]})
    back = parse_transcript(text)
    assert back[0].attachments == [
        {"path": "a/b.pdf", "name": "b.pdf", "media_type": "application/pdf"}]
    assert back[1].attachments == []


def test_tools_round_trip_only_when_present():
    # A plain assistant message serializes to the exact two-key shape …
    plain = serialize_transcript([Message(role="assistant", content="hi")])
    assert json.loads(plain)["messages"][0] == {"role": "assistant", "content": "hi"}

    # … and a tool record rides as the optional bounded display shape.
    msgs = [Message(role="assistant", content="done", tools=[
        {"name": "Read", "status": "done", "input": "path=a.py",
         "result": "42 lines"},
        {"name": "society", "status": "round 3"},
    ])]
    text = serialize_transcript(msgs)
    entry = json.loads(text)["messages"][0]
    assert entry["tools"] == [
        {"name": "Read", "status": "done", "input": "path=a.py",
         "result": "42 lines"},
        {"name": "society", "status": "round 3"},
    ]
    back = parse_transcript(text)
    assert back[0].tools == entry["tools"]


def test_tool_typed_extras_round_trip():
    """The ACP-typed extras (kind / locations / content) persist through the
    transcript in the writer's key order (name, status, input?, result?,
    kind?, locations?, content?), bounded: malformed locations/blocks drop,
    diff text is capped, and an unknown block type never survives."""
    msgs = [Message(role="assistant", content="done", tools=[
        {"name": "Edit", "status": "done", "input": "path=a.py",
         "kind": "edit",
         "locations": [{"path": "a.py", "line": 3}, {"path": ""},
                       {"path": "b.py", "line": "seven"}],
         "content": [
             {"type": "diff", "path": "a.py", "oldText": "x = 1",
              "newText": "x = 2"},
             {"type": "terminal", "terminalId": "term_1"},
             {"type": "mystery", "data": "?"},
         ]},
    ])]
    entry = json.loads(serialize_transcript(msgs))["messages"][0]
    assert list(entry["tools"][0]) == [
        "name", "status", "input", "kind", "locations", "content"]
    assert entry["tools"][0]["kind"] == "edit"
    assert entry["tools"][0]["locations"] == [
        {"path": "a.py", "line": 3}, {"path": "b.py"}]
    assert entry["tools"][0]["content"] == [
        {"type": "diff", "path": "a.py", "oldText": "x = 1", "newText": "x = 2"},
        {"type": "terminal", "terminalId": "term_1"},
    ]
    back = parse_transcript(serialize_transcript(msgs))
    assert back[0].tools == entry["tools"]


def test_tools_assistant_only_and_malformed_dropped():
    # tools on a user message never serialize; malformed entries (no name,
    # non-dict, non-list) are dropped on read rather than smuggled through.
    text = serialize_transcript([
        Message(role="user", content="q", tools=[{"name": "x", "status": "done"}]),
    ])
    assert "tools" not in json.loads(text)["messages"][0]

    doc = {"messages": [{"role": "assistant", "content": "r", "tools": [
        {"status": "done"},              # no name → dropped
        "not-a-dict",                    # → dropped
        {"name": "ok", "input": 42},     # input coerced to str
    ]}]}
    msgs = parse_transcript(json.dumps(doc))
    assert msgs[0].tools == [{"name": "ok", "status": "done", "input": "42"}]
    assert parse_transcript(json.dumps(
        {"messages": [{"role": "assistant", "content": "r", "tools": "nope"}]}
    ))[0].tools == []


def test_tools_bounded_on_read():
    from collimate.transcript import _MAX_TOOLS, _TOOL_TEXT_CAP
    doc = {"messages": [{"role": "assistant", "content": "r", "tools": [
        {"name": f"t{i}", "input": "x" * (_TOOL_TEXT_CAP + 100)}
        for i in range(_MAX_TOOLS + 10)
    ]}]}
    msgs = parse_transcript(json.dumps(doc))
    assert len(msgs[0].tools) == _MAX_TOOLS
    assert len(msgs[0].tools[0]["input"]) == _TOOL_TEXT_CAP


def test_stopped_round_trips_only_when_true():
    text = serialize_transcript([
        Message(role="assistant", content="part", stopped=True),
        Message(role="assistant", content="full"),
        Message(role="user", content="q", stopped=True),   # non-assistant: never
    ])
    entries = json.loads(text)["messages"]
    assert entries[0]["stopped"] is True
    assert "stopped" not in entries[1] and "stopped" not in entries[2]
    back = parse_transcript(text)
    assert [m.stopped for m in back] == [True, False, False]


def test_optional_transcript_fields_use_empty_defaults():
    plain = json.dumps({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "thinking": "hm"},
    ]})
    msgs = parse_transcript(plain)
    assert msgs[1].tools == [] and msgs[1].stopped is False
    assert msgs[1].thinking == "hm"
