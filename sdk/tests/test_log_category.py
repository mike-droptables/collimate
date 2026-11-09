"""``gen.update_file(path, content, category='log')`` — the one-keyword
Logs surface.

A log-declared path is a run artifact, not cell content: it stays readable
through ``read_file`` / ``has_file`` (the agent's per-turn trace rewrite
depends on that), but it is excluded from ``list_files``, live-file
mirroring, workspace snapshots, and the offline
harness routes it into ``GeneratorResult.log_files`` instead of ``.files``.
A later plain write reclassifies it back to cell content; deleting it never
emits a phantom cell deletion."""

import pytest

from collimate.testing import run_generator_callable


def test_log_write_routes_to_log_files_not_files():
    def run():
        import gen
        gen.update_file("out.txt", "result")
        gen.update_file("_agent/trace.json", '{"steps": []}', category="log")

    res = run_generator_callable(run)
    res.raise_for_status()
    assert res.files.get("out.txt") == "result"
    assert "_agent/trace.json" not in res.files
    assert res.log_files.get("_agent/trace.json") == '{"steps": []}'


def test_log_path_readable_but_unlisted():
    def run():
        import gen
        gen.update_file("trace.json", "v1", category="log")
        assert gen.has_file("trace.json")
        assert gen.read_file("trace.json") == "v1"
        assert "trace.json" not in gen.list_files()
        # Per-turn rewrite keeps working (read-modify-write).
        gen.update_file("trace.json", gen.read_file("trace.json") + "+v2",
                        category="log")

    res = run_generator_callable(run)
    res.raise_for_status()
    assert res.log_files.get("trace.json") == "v1+v2"


def test_plain_write_reclassifies_back_to_file():
    def run():
        import gen
        gen.update_file("notes.md", "draft", category="log")
        gen.update_file("notes.md", "final")  # category='file' default

    res = run_generator_callable(run)
    res.raise_for_status()
    assert res.files.get("notes.md") == "final"
    assert "notes.md" not in res.log_files


def test_delete_log_path_is_silent():
    def run():
        import gen
        gen.update_file("scratch.log", "x", category="log")
        gen.delete_file("scratch.log")
        gen.update_file("out.txt", "done")

    res = run_generator_callable(run)
    res.raise_for_status()
    assert "scratch.log" not in res.files
    assert "scratch.log" not in res.log_files
    # No phantom deletion reached the flush (the path was never cell content).
    from collimate import native
    # (Behavioural check only — the record shows a clean single update.)
    assert res.files.get("out.txt") == "done"
    assert native is not None


def test_invalid_category_raises():
    def run():
        import gen
        with pytest.raises(ValueError):
            gen.update_file("x.txt", "x", category="config")
        with pytest.raises(ValueError):
            gen.update_file("x.txt", "x", category="junk")
        gen.update_file("ok.txt", "ok")

    res = run_generator_callable(run)
    res.raise_for_status()
    assert res.files.get("ok.txt") == "ok"


def test_update_cell_wire_carries_category(tmp_path):
    """The native flush marks log paths with ``"category": "log"`` on the
    artifact dict — the one additive wire key the host routes on."""
    from collimate._host.bridge import HostBridge

    captured: list[dict] = []

    class _Bridge(HostBridge):
        def emit_and_wait(self, payload, *, wait_op, timeout=None):
            captured.append(payload)
            return {"cell_id":"x", "operation_id":payload["operation_id"], "outcome":"accepted", "files":[
                {'path':path, 'file_revision':base+1, 'writer_id':payload['writer_id'],
                 'edit_seq':payload['edit_seq'], 'deleted':path in payload.get('deleted_files', [])}
                for path, base in payload['base_revisions'].items()]}

    import logging
    import os

    ws = str(tmp_path)
    b = _Bridge.__new__(_Bridge)
    b.run_id = "test-run"
    b.workspace = ws
    b.run_dir = os.path.join(ws, ".collrun")
    b._initialize_workspace()
    b._initialize_persistence()
    b.log = logging.getLogger("test")

    b.fs_write_any("a.txt", "cell content")
    b.fs_write_any("t.json", "log content")
    b.log_paths.add("t.json")
    b.update_cell(name="n", writes=sorted(b.tracked_writes))

    assert captured and captured[0]["op"] == "update_cell"
    by_path = {a["cell_path"]: a for a in captured[0]["artifacts"]}
    assert "category" not in by_path["a.txt"]
    assert by_path["t.json"].get("category") == "log"
    # initial latch is gone — no payload key, and log_paths persists.
    assert "initial" not in captured[0]
    assert b.log_paths == {"t.json"}
    assert os.path.isfile(os.path.join(ws, "t.json"))


def test_reclassifying_committed_file_to_log_emits_cell_deletion():
    """A path committed as cell content and then rewritten with
    category='log' must not leave a stale, un-removable Files copy —
    the flush carries a deletion for it."""
    def run():
        import asyncio

        import gen
        gen.update_file("report.md", "v1")
        asyncio.run(gen.commit())
        gen.update_file("report.md", "debug now", category="log")
        asyncio.run(gen.commit())

    res = run_generator_callable(run)
    res.raise_for_status()
    assert res.log_files.get("report.md") == "debug now"
    # The final flushed cell snapshot no longer carries the stale copy.
    assert "report.md" not in res.files


def test_log_paths_are_normalized():
    """'./trace.json' and 'trace.json' are the same artifact — the sweeps
    (fs_list / mirrors / snapshots) compare normalized relpaths."""
    def run():
        import gen
        gen.update_file("./trace.json", "t", category="log")
        assert "trace.json" not in gen.list_files()

    res = run_generator_callable(run)
    res.raise_for_status()
    assert res.log_files.get("trace.json") == "t"
    assert "trace.json" not in res.files


def test_unflushed_log_write_still_surfaces_in_result():
    """A generator that writes its trace and then raises never reaches the
    auto-flush — the harness result must still expose the log artifact."""
    def run():
        import gen
        gen.update_file("trace.json", "partial trace", category="log")
        raise RuntimeError("boom")

    res = run_generator_callable(run)
    assert not res.ok
    assert res.log_files.get("trace.json") == "partial trace"
    assert "trace.json" not in res.files
