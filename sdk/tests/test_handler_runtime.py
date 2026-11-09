"""Public handler operations work without a permission grant or host process."""
import pytest
import gen
from collimate._host.bridge import HostBridge

HANDLERS = [{"id": "short", "name": "Short", "description": "", "types": [{"ext": ".txt"}]},
            {"id": "long", "name": "Long", "description": "", "types": [{"ext": ".special.txt"}]}]


def test_public_handler_lookup_skills_and_current_workspace_verdict__D09(native_bridge):
    native_bridge.stub_handlers(HANDLERS, skills={"long": {"long--edit/SKILL.md": "edit text"}})
    native_bridge.stub_verifier(".special.txt", lambda content: ({"valid": True} if content == b"good" else {"valid": False, "error": "bad"}))
    gen.update_file("doc.special.txt", "bad")
    assert gen.handler_for("DOC.SPECIAL.TXT")["id"] == "long"
    assert gen.handler_for("unknown") is None
    assert gen.handler_skills(".SPECIAL.TXT") == {"long--edit/SKILL.md": "edit text"}
    assert gen.validate("doc.special.txt") == {"valid": False, "error": "bad"}
    gen.update_file("doc.special.txt", "good")
    assert gen.validate("doc.special.txt") == {"valid": True}
    gen.update_file("plain.txt", "anything")
    assert gen.validate("plain.txt") == {"valid": True}
    records = gen.handlers()
    records[0]["types"].clear()
    assert gen.handler_for("doc.txt")["id"] == "short"


@pytest.mark.parametrize("method,args,op", [("handlers", (), "list_handlers"), ("handler_skills", (".txt",), "handler_skills"), ("validate", ("a.txt",), "validate_file")])
def test_handler_requests_use_existing_frame_precheck__D09_F1194(monkeypatch, method, args, op):
    from collimate._host import bridge as _host
    monkeypatch.setattr(_host.ipc, "IPC_MAX_LINE_BYTES", 8)
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._socket = bridge._sock_rfile = bridge._sock_wfile = None
    with pytest.raises(ValueError, match=op):
        getattr(bridge, method)(*args)


def test_recording_plain_verdict_matches_host_shape__D09(native_bridge):
    gen.update_file('plain.txt', 'plain')
    assert gen.validate('plain.txt') == {'valid':True}


def test_handler_nullable_types_and_testing_exports_match_runtime__D09():
    import typing
    from collimate import testing
    from collimate._results import HandlerType, HandlerInfo, ValidationResult
    assert testing.HandlerType is gen.HandlerType is HandlerType
    assert testing.HandlerInfo is gen.HandlerInfo is HandlerInfo
    assert testing.ValidationResult is gen.ValidationResult is ValidationResult
    fields = typing.get_type_hints(HandlerType, include_extras=True)
    assert set(fields) == {'ext', 'render_as'}
    assert type(None) in typing.get_args(typing.get_args(fields['render_as'])[0])
    error = typing.get_type_hints(ValidationResult, include_extras=True)['error']
    assert typing.get_args(error) == (str,)
