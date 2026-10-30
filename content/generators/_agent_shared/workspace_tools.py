# SPDX-License-Identifier: MIT
"""Agent workspace tools and the one tool registry used by every rendering."""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import stat

import gen
from .cell_caps import skills_index as _cc_index
from .web_tools import fetch, search

def skill_tool_entries(skills: list) -> list:
    """The ``list_skills``/``load_skill`` registry rows for one turn's skills —
    built per turn as closures over the skill list (a plain parameter now:
    the module-global stash existed only because the old eight runner call
    shapes demanded signature-stable rows)."""
    async def list_skills() -> str:
        """List the skills available for this run — the name and description of
        each skill the runner provides. Load a skill's full instructions with
        load_skill before applying it."""
        return _cc_index(skills) or "No skills provided for this run."

    async def load_skill(name: str) -> str:
        """Load a skill's full instructions by its name (from list_skills).
        Returns the skill's frontmatter + body; an unknown name lists what
        exists."""
        for s in skills:
            if s.name == name:
                desc = f"description: {s.description}\n" if s.description else ""
                return f"# skill: {s.name}\n{desc}\n{s.body}"
        known = ", ".join(s.name for s in skills) or "none"
        return f"ERROR: no skill named {name!r} (available: {known})"

    return [
        ("list_skills", "skills", False, "cell _skills/ index", list_skills),
        ("load_skill",  "skills", False, "read one _skills/ SKILL.md", load_skill),
    ]


def tool_description(fn, gen_twin: str) -> str:
    """The impl's docstring plus its authoring twin, so the calling surface
    always points back at the ``gen.*`` an author would write for the same act."""
    doc = (fn.__doc__ or "").strip()
    return f"{doc}\n\nAuthoring-API twin (when writing a generator): {gen_twin}.".strip()


READ_FILE_BYTE_CAP = 200_000


GREP_BYTE_CAP = 65_536


EXEC_OUTPUT_CAP = 65_536


RESTAGE_FILE_CAP = 20_000_000


class PathEscape(Exception):
    """A tool path that would read/write outside the workspace root."""


def contain_ws_path(path: str) -> str:
    """A workspace-relative path guaranteed to stay inside the workspace, or
    raise :class:`PathEscape`.

    Lexical AND physical: rejects absolute paths and ``..`` traversal, then
    resolves symlinks (``realpath``) and asserts the result is still under the
    workspace root — the container harnesses are root in the bind-mounted
    ``/workspace`` and can plant a symlink whose target the host generator
    would otherwise dereference (the file tools run in the host process). This
    is the single containment used by every file tool the MCP bridge serves
    and the ACP ``fs/*`` handlers alike (grep, which walks rather than taking a
    path, refuses symlinks itself).

    This check alone can be raced (a regular file swapped for a link between
    it and the open), so the SDK's workspace opens (``collimate.fs``) are
    what make it atomic: they never follow a symlink at any component. A race that loses there surfaces as an ``OSError`` the
    file tools report as an ERROR."""
    p = str(path or "")
    # Lexical guarantees hold everywhere, no workspace needed.
    if os.path.isabs(p):
        raise PathEscape(f"absolute path refused: {p}")
    norm = os.path.normpath(p).replace("\\", "/")
    if norm == ".." or norm.startswith("../"):
        raise PathEscape(f"path escapes the workspace: {p}")
    # The physical (symlink) check needs a real workspace; in a bridge-free /
    # unit context there is none and nothing to escape, so the lexical result
    # stands.
    try:
        root = os.path.realpath(gen.workspace_path())
    except Exception:  # noqa: BLE001 — no bridge: lexical containment only
        return norm
    try:
        real = os.path.realpath(os.path.join(root, norm))
    except OSError as e:  # e.g. a link swapped mid-resolution (ELOOP/EINVAL)
        raise PathEscape(f"path could not be resolved: {p} ({e})") from e
    if real != root and not real.startswith(root + os.sep):
        raise PathEscape(f"path resolves outside the workspace: {p}")
    return norm


def bounded_file_listing() -> str:
    from collimate import fs

    paths = [p for p in gen.list_files()
             if not p.startswith("_agent/")
             and not any(segment in fs.WORKSPACE_SKIP_DIRS for segment in p.split("/")[:-1])]
    out = []
    size = 0
    for path in paths:
        cost = len(path.encode("utf-8")) + bool(out)
        if size + cost > GREP_BYTE_CAP:
            out.append("…[truncated]")
            break
        out.append(path)
        size += cost
    return "\n".join(out) if out else "(empty workspace)"


async def list_files() -> str:
    """List workspace paths, hiding vendored/private folders; cap at 64 KiB."""
    return await asyncio.to_thread(bounded_file_listing)


def read_workspace_lines(path: str, offset: int, limit: int) -> str:
    from collimate import fs
    import io
    import itertools

    try:
        path = contain_ws_path(path)
        fd = fs.open_file(gen.workspace_path(), path, write=False)
        with os.fdopen(fd, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            sniff = fh.read(8192)
            if b"\x00" in sniff:
                return f"BINARY ({size} bytes)"
            fh.seek(0)
            if size <= 8_000_000:
                raw = fh.read()
                try:
                    lines = iter(raw.decode("utf-8").splitlines())
                except UnicodeDecodeError:
                    return f"BINARY ({len(raw)} bytes)"
            else:
                # Bounded readline also handles a single enormous line;
                # normal line paging remains lazy through islice.
                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                def streamed_lines():
                    while True:
                        chunk = text.readline(READ_FILE_BYTE_CAP + 1)
                        if not chunk:
                            return
                        yield chunk
                        # When the caller advances past a huge line, drain
                        # its remainder without inventing additional lines.
                        while not chunk.endswith("\n") and len(chunk) == READ_FILE_BYTE_CAP + 1:
                            chunk = text.readline(READ_FILE_BYTE_CAP + 1)
                            if not chunk:
                                return
                lines = streamed_lines()
            start = max(0, int(offset))
            rendered = []
            total = 0
            for i, line in enumerate(itertools.islice(lines, start, start + max(0, int(limit)))):
                piece = f"{start + i + 1:>6}\t{line.rstrip(chr(10) + chr(13))}"
                raw_piece = piece.encode("utf-8")
                cost = len(raw_piece) + bool(rendered)
                if total + cost > READ_FILE_BYTE_CAP:
                    if not rendered:
                        rendered.append(raw_piece[:READ_FILE_BYTE_CAP].decode("utf-8", "ignore"))
                        rendered.append(f"…[line truncated at {READ_FILE_BYTE_CAP} bytes; use grep to search within it]")
                    else:
                        rendered.append("…[truncated: use offset/limit]")
                    break
                rendered.append(piece)
                total += cost
            return "\n".join(rendered)
    except PathEscape as exc:
        return f"ERROR: {exc}"
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as exc:
        return f"ERROR: {exc}"


async def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read numbered lines off-loop, capped at 200 KB; stream files over 8 MB."""
    return await asyncio.to_thread(read_workspace_lines, path, offset, limit)


def grep_cap(lines, max_results) -> str:
    """Cap ripgrep output lines at the grep line + byte budget."""
    out: list[str] = []
    total = 0
    truncated = False
    for ln in lines:
        if len(out) >= max_results:
            truncated = True
            break
        add = len(ln.encode("utf-8")) + (1 if out else 0)
        if total + add > GREP_BYTE_CAP:
            truncated = True
            break
        out.append(ln)
        total += add
    if not out:
        return "(no matches)"
    return "\n".join(out) + ("\n…[truncated]" if truncated else "")


class GrepStreamTooLarge(Exception):
    pass


def rg_records(proc):
    """Bound stdout memory and total wait even before a newline arrives."""
    import selectors
    import subprocess
    import time

    deadline = time.monotonic() + 20
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(proc.args, 20)
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                if pending:
                    yield json.loads(pending)
                return
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield json.loads(line)
            if len(pending) > 2 * GREP_BYTE_CAP:
                raise GrepStreamTooLarge


def grep_impl(workspace, pattern, path_glob="", max_results=200, context=0) -> str:
    """Search workspace files for ``pattern`` (a regular expression). Pure and
    unit-testable.

    Prefers ``rg`` when it is on PATH; otherwise a stdlib ``os.walk`` + ``re``
    fallback. Both skip ``_agent/`` at the workspace root and ``.collrun``; the
    fallback also skips dotdirs and binary files (null-byte sniff). Emits
    ``path:lineno:text`` per match (context lines as ``path-lineno-text``,
    ripgrep-style). Capped at ``max_results`` matches and
    65 536 bytes. Returns ``(no matches)`` when nothing hits, or ``ERROR: ...``
    on a bad regex / an escaping glob / an rg failure. A ``path_glob`` may never
    escape the workspace root (absolute or ``..`` is rejected)."""
    import fnmatch
    import subprocess

    workspace = str(workspace)
    max_results = max(1, int(max_results))
    context = max(0, int(context))
    glob = (path_glob or "").strip()
    if glob and (os.path.isabs(glob) or ".." in glob.replace("\\", "/").split("/")):
        return f"ERROR: path_glob must stay inside the workspace: {glob!r}"

    if shutil.which("rg"):
        cmd = ["rg", "--json", "--color", "never"]
        if context:
            cmd += ["-C", str(context)]
        if glob:
            cmd += ["-g", glob]
        cmd += ["-g", "!/_agent/", "-g", "!/.collrun/", "-e", pattern]
        out = []
        matches = total = 0
        truncated = False
        try:
            with subprocess.Popen(cmd, cwd=workspace, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                  ) as proc:
                try:
                    for record in rg_records(proc):
                        kind = record.get("type")
                        if kind not in {"match", "context"}:
                            continue
                        data = record["data"]
                        path = data.get("path", {}).get("text")
                        value = data.get("lines", {}).get("text")
                        if path is None or value is None:
                            continue
                        if kind == "match" and matches >= max_results:
                            truncated = True
                            break
                        separator = ":" if kind == "match" else "-"
                        piece = f"{path}{separator}{data['line_number']}{separator}{value.rstrip(chr(10) + chr(13))}"
                        cost = len(piece.encode("utf-8")) + bool(out)
                        if total + cost > GREP_BYTE_CAP:
                            truncated = True
                            break
                        out.append(piece)
                        total += cost
                        if kind == "match":
                            matches += 1
                    if truncated:
                        proc.terminate()
                    try:
                        rc = proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                        return "ERROR: rg timed out"
                except GrepStreamTooLarge:
                    truncated = True
                    proc.terminate()
                    rc = proc.wait(timeout=20)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            if rc not in (0, 1) and not truncated:
                return f"ERROR: rg failed ({rc})"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {exc}"
        if not out:
            return "…[truncated]" if truncated else "(no matches)"
        return "\n".join(out) + ("\n…[truncated]" if truncated else "")

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"
    skip_dirs = {"_agent", ".collrun"}
    chunks: list[str] = []
    matches = 0
    total = 0
    truncated = False
    skipped = 0
    # Never follow a symlinked file (or dir) out of the workspace: this runs
    # in the host process, and a container harness is root in the bind mount
    # and can plant `x -> ~/.ssh/id_rsa`. fwalk pins each directory by fd and
    # the leaf open is O_NOFOLLOW, so a link swapped in mid-walk is refused
    # too; a FIFO is skipped (O_NONBLOCK + the regular-file check).
    use_fd = hasattr(os, "fwalk") and hasattr(os, "O_NOFOLLOW") \
        and os.open in os.supports_dir_fd
    if use_fd:
        walk = ((r, d, f, rfd) for r, d, f, rfd in os.fwalk(workspace))
    else:
        walk = ((r, d, f, None) for r, d, f in os.walk(workspace))
    for root, dirs, files, rootfd in walk:
        at_root = os.path.normpath(root) == os.path.normpath(workspace)
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and not (at_root and d in skip_dirs)]
        for fname in sorted(files):
            abs_path = os.path.join(root, fname)
            rel = os.path.relpath(abs_path, workspace).replace("\\", "/")
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                if rootfd is not None:
                    fd = os.open(fname, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=rootfd)
                else:
                    if os.path.islink(abs_path):
                        continue
                    fd = os.open(abs_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                with os.fdopen(fd, "rb") as fh:
                    info = os.fstat(fh.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if info.st_size > 20_000_000:
                        skipped += 1
                        continue
                    sniff = fh.read(8192)
                    if b"\x00" in sniff:
                        skipped += 1
                        continue
                    raw = sniff + fh.read(20_000_001 - len(sniff))
                    if len(raw) > 20_000_000:
                        skipped += 1
                        continue
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue                               # binary — skip
            flines = raw.decode("utf-8", errors="replace").splitlines()
            for idx, line in enumerate(flines):
                if not rx.search(line):
                    continue
                if matches >= max_results:
                    truncated = True
                    break
                out: list[str] = []
                if context > 0:
                    for j in range(max(0, idx - context), idx):
                        out.append(f"{rel}-{j + 1}-{flines[j]}")
                out.append(f"{rel}:{idx + 1}:{line}")
                if context > 0:
                    for j in range(idx + 1, min(len(flines), idx + context + 1)):
                        out.append(f"{rel}-{j + 1}-{flines[j]}")
                piece = "\n".join(out)
                add = len(piece.encode("utf-8")) + (1 if chunks else 0)
                if total + add > GREP_BYTE_CAP:
                    truncated = True
                    break
                chunks.append(piece)
                total += add
                matches += 1
            if truncated:
                break
        if truncated:
            break
    note = f"\n…[skipped {skipped} oversized/binary file(s)]" if skipped else ""
    if not chunks:
        return "(no matches)" + note
    return "\n".join(chunks) + ("\n…[truncated]" if truncated else "") + note


async def grep(
    pattern: str, path_glob: str = "", max_results: int = 200, context: int = 0,
) -> str:
    """Search the workspace for ``pattern`` (a regular expression).

    ``path_glob`` limits the search to matching relative paths (e.g. ``"*.py"``);
    ``context`` adds N lines of context around each hit. Matches come back as
    ``path:lineno:text``. Capped at ``max_results`` matches and 65 536 bytes."""
    return await asyncio.to_thread(
        grep_impl, gen.workspace_path(), pattern, path_glob, max_results, context,
    )


async def write_file(path: str, content: str) -> str:
    """Create or overwrite ``path`` with UTF-8 ``content``. Files under
    ``_runs/`` are runs' interface files: another run's are read-only, and in
    your own run folder you write only the declared interface targets."""
    from .run_files import write_file
    try:
        path = contain_ws_path(path)
    except PathEscape as e:
        return f"ERROR: {e}"
    try:
        write_file(path, content)
    except (OSError, ValueError) as e:
        # A _runs/ path the run may not write (PermissionError), or a
        # symlink raced in after contain_ws_path — refused either way.
        return f"ERROR: {e}"
    return f"wrote {len(content)} bytes to {path}"


async def edit_file(path: str, old: str, new: str) -> str:
    """Replace the first occurrence of ``old`` with ``new`` inside ``path``
    (the same ``_runs/`` rules as write_file). ``old`` must be non-empty."""
    from .run_files import write_file, write_refusal
    try:
        path = contain_ws_path(path)
    except PathEscape as e:
        return f"ERROR: {e}"
    refusal = write_refusal(path)
    if refusal:
        return f"ERROR: {refusal}"
    if not old:
        return "ERROR: old must be non-empty (use write_file to replace a whole file)"
    if not gen.has_file(path):
        return f"ERROR: file not found: {path}"
    try:
        cur = gen.read_file(path)
        if not isinstance(cur, str):
            return f"ERROR: {path} is binary"
        if old not in cur:
            return f"ERROR: old text not found in {path}"
        write_file(path, cur.replace(old, new, 1))
    except (OSError, ValueError) as e:  # a symlink raced in after contain_ws_path
        return f"ERROR: {e}"
    return f"edited {path}"


IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


VIEW_IMAGE_BYTE_CAP = 8_000_000


def ws_file_size(path: str) -> int:
    """The size of workspace file ``path``, probed through the same no-follow
    open ``read_file`` uses (a symlink raced in raises ``OSError``)."""
    from collimate import fs as _cfs

    fd = _cfs.open_file(gen.workspace_path(), path, write=False)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


async def load_view_image(path: str) -> tuple:
    """The validation both view_image renderings share. Returns ``(error,
    contained_path, data, ext)``; ``error`` is an ERROR string or ``None``."""
    try:
        path = contain_ws_path(path)
    except PathEscape as e:
        return f"ERROR: {e}", path, b"", ""
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_MEDIA_TYPES:
        return (
            f"ERROR: {path} is not a viewable image "
            f"(expected {', '.join(sorted(IMAGE_MEDIA_TYPES))})"
        ), path, b"", ext
    if not gen.has_file(path):
        return f"ERROR: file not found: {path}", path, b"", ext
    # Refuse an oversize file before reading it into memory; the post-read
    # check below stays as a backstop (the file can grow in between).
    try:
        size = ws_file_size(path)
    except (OSError, ValueError) as e:
        return f"ERROR: {e}", path, b"", ext
    if size > VIEW_IMAGE_BYTE_CAP:
        return f"ERROR: {path} is too large to view ({size} bytes)", path, b"", ext
    try:
        data = await asyncio.to_thread(gen.read_file, path)
    except OSError as e:  # a symlink raced in after contain_ws_path — refused
        return f"ERROR: {e}", path, b"", ext
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > VIEW_IMAGE_BYTE_CAP:
        return f"ERROR: {path} is too large to view ({len(data)} bytes)", path, b"", ext
    return None, path, data, ext


async def view_image(path: str):
    """Look at an image workspace file — return its pixels to the model so it can
    actually SEE the screenshot / diagram, not just its byte count.

    Use this for `.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` files (e.g. the
    app screenshots under `images/`). `read_file` only reports a binary's size;
    this surfaces the image itself. A missing path, a non-image extension, or an
    oversized file returns an ERROR string instead."""
    # The MCP server's Image content block: the image shape every bridged
    # client renders.
    err, path, data, ext = await load_view_image(path)
    if err:
        return err
    try:
        from mcp.server.mcpserver import Image
    except Exception as e:  # noqa: BLE001 — missing MCP → degrade to a plain note
        return f"ERROR: image display unavailable on this surface ({e})"
    return Image(data=bytes(data), format=IMAGE_MEDIA_TYPES[ext].split('/', 1)[1])


def safe_snapshot():
    """``gen.snapshot()`` or ``None`` when it isn't available (stub / no-host)."""
    try:
        return gen.snapshot()
    except Exception:  # noqa: BLE001
        return None


#: Pydantic AI Harness keeps its own files (shell job logs, spilled tool
#: output) under ``.pydantic-ai-harness/`` in its workspace: never run output.
HARNESS_METADATA_DIR = ".pydantic-ai-harness"


def not_restaged(path: str) -> bool:
    return (path.startswith(("_agent/", "_runs/", ".collrun/", HARNESS_METADATA_DIR + "/"))
            or path in (".collrun", HARNESS_METADATA_DIR))


class RestageTooLarge(Exception):
    """A changed file over :data:`RESTAGE_FILE_CAP`, left unstaged."""

    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes")
        self.size = size


def track_restage_file(ws: str, path: str) -> None:
    """Track a safe-open existing file; preserve its bytes and enforce cap."""
    from collimate import fs as _cfs

    fd = _cfs.open_file(ws, path, write=False)
    try:
        size = os.fstat(fd).st_size
        if size > RESTAGE_FILE_CAP:
            raise RestageTooLarge(size)
    finally:
        os.close(fd)
    gen.track_file(path, max_bytes=RESTAGE_FILE_CAP)


def restage_exec_writes(before: dict, after: dict) -> list:
    """Re-stage files the harness or its commands created / changed / deleted
    in the run workspace into the output cell after a turn.

    ``before``/``after`` are ``gen.snapshot()`` maps (``{relpath: sha}``).
    ``gen.commit()`` flushes only ``gen.update_file``-tracked writes, so files a
    subprocess wrote never reach the cell on the oneshot path unless re-staged
    here: new / changed paths are tracked without rewriting their bytes
    (per-file cap 20 MB — larger files are left unstaged with a note), and
    vanished paths are deleted. ``_agent/``, ``_runs/`` (runs' interface files
    — the live mirror carries this run's own) and ``.collrun`` paths are
    ignored. Returns note lines to append to the tool output. Used by
    ``run_agent``'s per-turn workspace restage."""
    notes: list = []
    ws = gen.workspace_path()
    before = before or {}
    after = after or {}
    for path, sha in after.items():
        if not_restaged(path):
            continue
        if before.get(path) == sha:
            continue                                    # unchanged
        try:
            # Safe-open and track the existing file without a staging write.
            # A raced symlink or oversize file is refused.
            track_restage_file(ws, path)
        except RestageTooLarge as e:
            notes.append(f"note: {path} ({e.size} bytes) left unstaged (too large)")
            continue
        except Exception as e:  # noqa: BLE001
            # e.g. a root-owned file a container exec left behind — surface it
            # instead of silently dropping the artifact from the output cell.
            notes.append(f"note: {path} could not be re-staged ({e})")
            continue
    for path in before:
        if path in after:
            continue
        if not_restaged(path):
            continue
        # The snapshot skips a file it cannot read, so a regular file still on
        # disk (e.g. rewritten 0600 by a container's root) is not a deletion.
        try:
            st = os.lstat(os.path.join(ws, path))
        except OSError:
            st = None
        if st is not None and stat.S_ISREG(st.st_mode):
            notes.append(f"note: {path} could not be read back — left as is")
            continue
        try:
            gen.delete_file(path)
        except Exception as e:  # noqa: BLE001
            notes.append(f"note: {path} could not be un-staged ({e})")
            continue
    return notes


def list_handlers() -> list[dict]:
    """List the channel's accepted file-type handlers and their metadata."""
    return gen.handlers()


def handler_skills(ext_or_id: str) -> dict[str, str]:
    """Read accepted authoring skills for a filename extension or handler ID."""
    return gen.handler_skills(ext_or_id)


def validate_file(path: str, verifier: str | None = None) -> dict:
    """Validate a workspace file against its accepted handler by default.

    Omit verifier for normal validation. If supplied, verifier must be an
    existing workspace-relative draft verifier file path, not a handler ID.
    Local file errors return valid=false so the input can be corrected.
    """
    from collimate import fs

    try:
        # Check local inputs without reading their bodies. Host-mediated
        # validation remains authoritative; its unexpected failures propagate.
        for local_path in (path,) if verifier is None else (path, verifier):
            fd = fs.open_file(gen.workspace_path(), local_path, write=False)
            os.close(fd)
    except OSError as exc:
        return {'valid': False, 'error': str(exc)}
    return gen.validate(path, verifier=verifier)


def with_handler_tools(entries=()) -> list:
    """Every agent surface has these ungated tools, once, with their SDK impl."""
    handlers = [
        ('list_handlers', 'handler', False, 'gen.handlers()', list_handlers),
        ('handler_skills', 'handler', False, 'gen.handler_skills()', handler_skills),
        ('validate_file', 'handler', False, 'gen.validate()', validate_file),
    ]
    by_name = {row[0]: row for row in handlers}
    extra = []
    for row in entries:
        if row[0] in by_name:
            if row != by_name[row[0]]:
                raise ValueError(f'Handler tool cannot be replaced: {row[0]}')
        else:
            extra.append(row)
    return handlers + extra


def tool_entries(
    *, enable_file_tools: bool, websearch: bool,
    skills: "list | None" = None,
    commands: str = "deny", command_context=None,
    annotation_tools: bool = False,
) -> list:
    """THE workspace registry: rows ``(name, gate_label, main_loop, twin_note,
    impl)`` for always-on handler and selected file/web/annotation tools — the
    MCP bridge (and its in-memory FastMCP twin) derives its membership from
    THIS list.
    Native harness tools do not replace the confined reader and host-mediated writer.
    ``gate`` is informational
    (``"handler"``/``"file"``/``"web"``/``"annotation"``/``"command"``);
    ``main_loop`` marks loop-affine operations: ``annotate``,
    ``list_annotations`` and ``test_generator``. Other file impls call sync
    ``gen.*`` and the web impls use httpx.

    ``annotation_tools`` adds the Annotation surface's rows — ``annotate``
    (post to the run's annotation context) and ``list_annotations`` —
    whatever ``enable_file_tools`` says.

    The test_generator command row uses an explicit command context; every
    harness runs other commands with its own shell."""
    entries: list = []
    if enable_file_tools:
        entries += [
            ("list_files", "file", False, "gen.list_files()", list_files),
            ("read_file",  "file", False, "gen.read_file()",   read_file),
            ("grep",       "file", False, "workspace regex search", grep),
            ("view_image", "file", False, "image pixels to the model", view_image),
            ("write_file", "file", False, "gen.update_file()", write_file),
            ("edit_file",  "file", False, "gen.update_file()", edit_file),
        ]
    if annotation_tools:
        from .annotation_tools import annotation_tool_entries
        entries += annotation_tool_entries()
    if websearch:
        entries += [
            ("web_fetch",  "web", False, "httpx fetch + readability", fetch),
            ("web_search", "web", False, "Tavily / DuckDuckGo search", search),
        ]
    # Cell skills: the lazy list/load pair rides the bridge whenever the
    # turn has skills — so even a text-only turn (enable_file_tools=False) can
    # discover and read the cell's skills. ``skills`` is the turn's own list
    # (a per-run parameter now, never module state).
    if skills:
        entries += skill_tool_entries(list(skills))
    from .generator_test import entries as generator_test_entries
    entries += generator_test_entries(commands=commands, context=command_context)
    return with_handler_tools(entries)
