# SPDX-License-Identifier: MIT
"""Behavioral characterization for P6 SDK ownership moves."""

from collimate import _mirror as mirror_runtime
import asyncio

import pytest

from collimate import native
from collimate._host.bridge import PartialRefusal
from collimate.transcript import parse_transcript, serialize_transcript
from tests.test_final_output_ack import Endpoint
from tests._workspace import GENERATORS


def test_workspace_staging_keeps_ui_and_log_ownership_F1217(native_bridge):
    native_bridge.stage_file("output.txt", "result\r\n", category="file")
    native_bridge.stage_file("trace.txt", "trace", category="log")
    assert native_bridge.fs_read_bytes("output.txt") == b"result\r\n"
    assert "trace.txt" not in native_bridge.fs_list()
    assert native_bridge.tracked_writes == {"output.txt", "trace.txt"}
    assert native_bridge.tracked_deletions == {"trace.txt"}
    with pytest.raises(PermissionError):
        native_bridge.fs_write_any(f"{native_bridge.folder}/foreign.txt", "x")
    native_bridge.ui_suffixes = (".json",)
    native_bridge.fs_write_ui(f"{native_bridge.folder}/view.json", "{}")
    with pytest.raises(ValueError):
        native_bridge.fs_write_ui(f"{native_bridge.folder}/view.txt", "no")
    native_bridge.fs_unlink("output.txt")
    assert not native_bridge.fs_exists("output.txt")
    assert "output.txt" in native_bridge.tracked_deletions


def test_message_optional_fields_roundtrip_and_independent_defaults_F1227():
    message = native.Message("assistant", "answer", thinking="reason", stopped=True,
                             attachments=[{"path": "output.txt", "name": "output", "media_type": "text/plain"}],
                             tools=[{"name": "write", "status": "done"}])
    parsed = parse_transcript(serialize_transcript([message]))
    assert parsed == [message]
    first, second = native.Message("user", "a"), native.Message("user", "b")
    first.attachments.append({"path": "a"})
    first.tools.append({"name": "tool"})
    assert not second.attachments and not second.tools


def test_mirror_keeps_refused_edit_and_cell_changes_F1163_F1164_F1165(native_bridge):
    async def run():
        local, remote = Endpoint({"a": "old", "b": "old"}), Endpoint({"a": "old", "b": "old"})
        mirror = mirror_runtime.Mirror(local, remote, own_folder=native_bridge.folder)
        await mirror.flush()
        local.files.update(a="rejected", b="accepted")
        remote.refuse.add("a")
        with pytest.raises(PartialRefusal):
            await mirror.flush()
        remote.files.update(a="external", b="user edit")
        remote.refuse.clear()
        await mirror.flush()
        assert local.files == {"a": "rejected", "b": "user edit"}
        local.files["a"] = "repaired"
        await mirror.flush()
        assert remote.files == {"a": "repaired", "b": "user edit"}
    asyncio.run(run())


def test_condition_lint_preserves_public_issue_identity_F1284():
    from tests.test_lint import _lint_set_yaml
    issues = _lint_set_yaml("""fields:
  - {key: mode, type: dropdown, options: [one, two]}
  - {key: count, type: number, show_if: {unknown: one}}
""")
    condition = [issue for issue in issues if issue.code == "cond-unknown-field"]
    assert len(condition) == 1
    assert condition[0].level.value == "error"
    assert condition[0].field == "cfg.set_yaml:count"
    assert "unknown" in condition[0].message


def test_host_call_keeps_dev_ack_and_body_refusal_distinct_F1215_F1218(native_bridge):
    from collimate._host.bridge import HostBridge
    # The base transport (not RecordingBridge's overrides) has the same
    # offline contract. A returned failed body is not a transport exception.
    assert HostBridge.emit_and_wait(native_bridge, {"op": "env_close", "env_id": "one"}, "ack_env_close") == {}
    from tests.test_emit_and_wait import _bridge_with_fake_socket
    import json
    with _bridge_with_fake_socket([json.dumps({"op": "ack_env_close", "ok": False, "error": "refused"})]) as bridge:
        result = bridge.env_close("one")
        assert result["ok"] is False and result["error"] == "refused"


def test_constructor_owns_distinct_single_flight_and_write_locks_F1219(tmp_path, monkeypatch):
    from collimate._host.bridge import HostBridge
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    bridge = HostBridge()
    try:
        assert bridge._write_lock() is bridge._ipc_write_lock
        assert bridge._request_lock() is bridge._ipc_request_lock
        assert bridge._write_lock() is not bridge._request_lock()
    finally:
        bridge.finish(success=True)


def test_acp_suite_docstring_names_actual_subsystems_F1929():
    import ast
    from pathlib import Path
    source = Path(__file__).with_name("test_acp_client.py").read_text()
    documentation = ast.get_docstring(ast.parse(source))
    for subsystem in ("host Node discovery", "adapter installation", "grant/approval",
                      "harness-home persistence", "transport cleanup"):
        assert subsystem in documentation


def test_acp_node_discovery_and_install_are_already_host_adapter_owned_F1217(monkeypatch):
    monkeypatch.syspath_prepend(str(GENERATORS))
    from _agent_shared import acp_client, host_adapters
    assert host_adapters.find_host_node.__module__ == "_agent_shared.host_adapters"
    assert host_adapters.ensure_adapter.__module__ == "_agent_shared.host_adapters"
    assert not hasattr(acp_client, "find_host_node")
    assert not hasattr(acp_client, "ensure_adapter")


def test_workspace_methods_have_one_extracted_owner_F1217():
    from collimate._host.bridge import HostBridge
    from collimate._workspace import WorkspaceMixin
    from collimate.testing import RecordingBridge
    assert HostBridge.fs_write_any is RecordingBridge.fs_write_any is WorkspaceMixin.fs_write_any
    assert HostBridge.snapshot_workspace is WorkspaceMixin.snapshot_workspace


def test_message_is_public_transcript_owned_F1227():
    from collimate.transcript import Message
    assert native.Message is Message
    assert Message.__module__ == "collimate.transcript"


def test_host_wrappers_use_one_call_entry_with_unchanged_budget_F1215_F1218(native_bridge, monkeypatch):
    from collimate._host.bridge import HostBridge
    calls = []

    def call(op, **values):
        calls.append((op, values))
        return {"ok": False, "error": "body refusal"}

    monkeypatch.setattr(native_bridge, "_call", call, raising=False)
    assert HostBridge.env_close(native_bridge, "owned") == {"ok": False, "error": "body refusal"}
    assert calls == [("env_close", {"env_id": "owned", "wait_op": "ack_env_close", "timeout": 60.0})]


def test_ipc_locks_require_initialized_transport_F1219():
    from collimate._host.bridge import HostBridge
    invalid_bridge = HostBridge.__new__(HostBridge)
    with pytest.raises(AttributeError):
        invalid_bridge._write_lock()
    with pytest.raises(AttributeError):
        invalid_bridge._request_lock()


def test_live_files_constructs_extracted_mirror_F1163_F1164_F1165(native_bridge):
    from collimate._mirror import Mirror
    assert type(native.live_files()) is Mirror
    assert Mirror.__module__ == "collimate._mirror"


def test_condition_analyzer_has_extracted_owner_F1284():
    from collimate.lint.conditions import check_set_yaml_conditions
    assert check_set_yaml_conditions.__module__ == "collimate.lint.conditions"


def test_agent_config_and_event_helpers_preserve_contract_F1217(monkeypatch):
    monkeypatch.syspath_prepend(str(GENERATORS))
    import _agent_shared as engine
    from collimate.permissions import Grant
    config = engine.parse_agent_config({"surface": "annotation", "provider": "anthropic", "model": "claude-sonnet-4-6",
                                        "_account": {"endpoint": "http://127.0.0.1:9"}},
                                       grant=Grant(docker="none", internet="none"), can_ask=True)
    assert config["backend"] == "pydantic-ai"
    assert config["commands"] == "deny"
    assert "exec_enabled" not in config
    with pytest.raises(ValueError, match="unknown config key"):
        engine.parse_agent_config({"surface": "annotation", "unknown": True}, grant=Grant(), can_ask=True)
    assert engine.trace_event("text", text="answer", missing=None) == {"type": "text", "text": "answer"}
    assert list(engine.iter_attachments([{"data": b"a", "media_type": "text/plain", "name": "a"}])) == [(b"a", "text/plain", "a")]
    first, second = engine.AgentResult(), engine.AgentResult()
    first.trace.append({"type": "text"})
    assert second.trace == []


def test_agent_config_and_events_have_separate_owners_F1217(monkeypatch):
    monkeypatch.syspath_prepend(str(GENERATORS))
    import _agent_shared as engine
    from _agent_shared import agent_config, agent_events
    assert engine.parse_agent_config is agent_config.parse_agent_config
    assert engine.trace_event is agent_events.trace_event
    assert engine.AgentResult is agent_events.AgentResult
