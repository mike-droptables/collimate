# SPDX-License-Identifier: MIT
"""Smoke tests: the public surface imports cleanly."""


def test_top_level_collimate_package():
    """``collimate`` exports only its public modules — each resolves (loaded
    on first use); ``gen`` is the runtime API (``gen.Message``)."""
    import importlib
    import importlib.util

    import collimate

    assert collimate.__all__ == sorted(collimate.__all__)
    assert set(collimate.__all__) == {
        "account_taxonomy", "config_values", "fs", "git_hosts", "handlers", "harness_catalog",
        "harness_placement",
        "host_harness", "lint", "options_stack", "permissions", "pointers", "providers",
        "shared", "surfaces", "testing", "transcript",
    }
    for name in collimate.__all__:
        assert getattr(collimate, name) is importlib.import_module(f"collimate.{name}")
    for internal in ("native", "_host", "_results"):
        assert internal not in collimate.__all__
    # The host plumbing lives under the private ``collimate._host`` only.
    for name in ("channel_events", "ipc", "renderer_bundle", "module_mirror", "module_bump",
                 "esm_lexer", "packaging", "verification", "verifier_engine", "package_hash", "bridge"):
        assert importlib.util.find_spec(f"collimate._host.{name}") is not None, name
        assert importlib.util.find_spec(f"collimate.{name}") is None, f"collimate.{name} is host plumbing"
    for name in ("Generator", "Artifact", "Message", "StopSignal"):
        assert not hasattr(collimate, name), f"collimate exports no {name}"


def test_the_api_reference_lists_exactly_the_public_modules():
    """The API reference's "Public modules" section names every module in
    ``collimate.__all__`` and calls every other one internal."""
    import re
    from pathlib import Path

    import collimate

    reference = (Path(__file__).resolve().parents[1] / "shared" / "docs"
                 / "api-reference.md").read_text(encoding="utf-8")
    import importlib.util

    def modules(text: str) -> set:
        return {w for w in re.findall(r"`(\w+)`", text)
                if importlib.util.find_spec(f"collimate.{w}") is not None}

    section = reference.split("## Public modules", 1)[1].split("\n## ", 1)[0]
    public, _, internal = section.partition("Every other module")
    assert modules(public) == set(collimate.__all__)
    assert modules(internal) == {"native"}


def test_public_modules_say_they_are_public():
    """The modules generators import beside ``gen`` say so in their
    docstrings."""
    import collimate.config_values
    import collimate.fs
    import collimate.pointers
    import collimate.transcript

    for mod in (collimate.fs, collimate.pointers, collimate.transcript,
                collimate.config_values):
        assert "public" in (mod.__doc__ or "").lower(), mod.__name__


def test_gen_package_re_exports_native():
    """``import gen`` exposes exactly the :mod:`collimate.native` public
    surface."""
    import gen
    from collimate import native

    assert gen.__all__ == native.__all__
    # Spot-check the surface — the canonical helpers a generator would
    # actually use. The full list lives in ``collimate.native.__all__``;
    # an exhaustive equality check would just duplicate that list.
    for name in (
        "account", "serve_chat", "ChatTurn", "config", "history", "options",
        "grant", "client_caps",
        "run_info", "proxy_url", "ask", "can_ask", "Message", "Grant", "RunInfo",
        "place_environment", "exec_environment", "expose_environment_port",
        "close_environment",
        "config_folder", "config_folder_path", "config_folders",
        "list_files", "main", "read_file",
        "commit_account", "serve_port", "start_terminal", "commit", "update_file",
        "update_ui_file", "list_ui_files",
        "workspace_path",
    ):
        assert getattr(gen, name) is getattr(native, name), f"gen.{name} should re-export native"
    # The Agent generator's session machinery is gen.session, never gen.
    import gen.session
    assert set(gen.session.__all__) == {"chat_busy", "emit_stream_event", "interrupt_turn",
                                        "pop_config_request", "request_finish", "submit_turn"}
    for name in gen.session.__all__:
        assert name not in gen.__all__
        assert getattr(gen.session, name) is getattr(native, name)


def test_gen_forwards_only_its_exports_and_the_per_run_attributes(native_bridge):
    """``gen`` reaches nothing in :mod:`collimate.native` beyond ``__all__``
    and the per-run ``gen.prompt`` / ``gen.cancelled``: a private helper, an
    internal name or a module the implementation imports is not part of it."""
    import pytest

    import gen

    native_bridge.prompt = "hello"
    assert gen.prompt == "hello" and gen.cancelled is False
    for name in ("_require_bridge", "_bridge", "_set_bridge", "_permission_answers",
                 "HostBridge", "os", "asyncio", "messages", "pop_permission_answer",
                 "DYNAMIC_ATTRS", "current_bridge", "set_bridge", "require_bridge",
                 "final_flush", "deliver_permission_answer"):
        with pytest.raises(AttributeError):
            getattr(gen, name)
    with pytest.raises(ImportError):
        from gen import _require_bridge  # noqa: F401


def test_packaging_module_imports_TR04():
    import collimate._host.packaging as packaging
    from collimate._host.packaging import CellSpec, DEFAULT_CELL_ORDER, FileSpec, read_asset_cells

    assert callable(read_asset_cells)
    assert CellSpec(id="c", name="C").order == DEFAULT_CELL_ORDER
    assert FileSpec(name="x", language="text").blob_hash is None
    assert sorted(packaging.__all__) == [
        "BUILTIN_DIR", "CELL_MANIFEST", "CellLayout", "CellRef", "CellSpec", "DEFAULT_CELL_ORDER",
        "FileSpec", "SUGGESTED_DIR", "Suggestion", "read_asset_cells", "read_cell", "read_cell_manifest",
        "read_suggested_cell", "read_suggestions", "read_tutorial_cell", "scan_cells", "shared_dir",
        "shared_libraries",
    ]


def test_host_bridge_is_internal():
    """``collimate._host.bridge`` is internal to the SDK — generator code
    must never import it. The leading underscore is the contract;
    this test pins it so an accidental rename to ``host`` (which would
    look like a public surface) shows up as a test failure."""
    import collimate._host.bridge as host

    # Spot-check the names the singleton actually depends on.
    for name in (
        "HostBridge",
        "IPCError", "IPCNakError", "IPCTimeoutError", "IPCDisconnectedError",
    ):
        assert hasattr(host, name), f"_host.{name} missing"
