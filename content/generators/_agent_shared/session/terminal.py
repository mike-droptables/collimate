# SPDX-License-Identifier: MIT
"""The terminal surface: the harness's own TTY for the whole run.

Tiers A/B mint the ACP session first and run the TTY on its id, so the live
tail (``terminal_tail``) mirrors exactly that conversation into the run's
terminal transcript, a log artifact; tier C opens a fresh TTY, and a harness
whose TTY names its own conversation (antigravity) is tailed on the one
conversation that appears after the store's earlier ones. The TTY is served
at the run's own ``.terminal`` pointer (``SessionPaths.terminal_pointer``),
which ``gen.serve_terminal`` writes when the TTY registers and deletes when
it ends. A run ends on Stop or when the TTY's program exits."""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio

import gen

from _agent_shared import backend_spec, terminal_tail

from .consts import SessionPaths, identity_directive


def with_terminal_skills(cfg: dict, paths: SessionPaths) -> str:
    """The TTY's system prompt: the runner's own prompt, the wall-clock line,
    the run's identity line and the runner's skills index (when the
    backend takes skills —
    ``BackendSpec.cell_skills`` — and any skill is present) — the terminal
    surface's equivalent of run_agent's per-turn injection. The terminal
    model has no ``load_skill`` tool, so the index points its own file tools
    at the ``_skills`` config folder by its workspace-relative path (staged
    under ``.collrun``, which a container sees through its workspace mount
    and a CLI on this machine in its cwd — see
    ``cell_attach.container_skills_root``). Best-effort:
    discovery problems degrade to the prompt without the index."""
    from _agent_shared import clock_line
    base = cfg["system_prompt"]
    clock = clock_line()
    base = (base + "\n\n" + clock).strip() if base else clock
    base = base + "\n\n" + identity_directive(paths)
    if not backend_spec(cfg["backend"]).cell_skills:
        return base
    try:
        from _agent_shared import discover_cell_caps
        from _agent_shared.cell_attach import container_skills_root
        from _agent_shared.cell_caps import skills_index
        caps = discover_cell_caps()
        idx = skills_index(caps.skills, root=container_skills_root())
    except Exception:  # noqa: BLE001
        idx = ""
    if not idx:
        return base
    return (base + "\n\n" + idx).strip() if base else idx


def opening_message(cfg: dict, selected=None) -> str:
    """The TTY's first message, for a harness whose TTY takes one
    (claude-code): the selected file (``selected``, a
    ``selected.SelectedFile``), then the run's prompt — the generator shows
    its Prompt on the terminal
    surface for claude-code only (``prompt_show_if``), and its "Start from
    the selected file" likewise. A media file cannot ride a TTY message: it
    is named, for the TTY's own file tools."""
    chosen = ""
    if selected is not None:
        chosen = (f'The user started this run from the selected file "{selected.path}"; read it.'
                  if selected.media else selected.message)
    prompt = (gen.prompt or "").strip()
    return "\n\n".join(part for part in (chosen, prompt) if part)


async def terminal_leg(cfg: dict, *, paths: SessionPaths,
                       on_ready=None, selected=None) -> None:
    """Run the harness's TTY — served at the run's terminal pointer
    (``paths.terminal_pointer``) — until Stop or its program exits.

    Tiers A/B mint the run's ACP session and run the TTY on its id, so the
    live tail follows exactly that conversation; a mint that fails degrades
    to a fresh TTY, and tier C always runs one. A fresh TTY's runner may hand
    its store's session ids from before it started to its ready hook
    (``before=``), and the live tail then follows only the one session that
    appears after them. A harness whose TTY names its own conversation
    (``BackendSpec.harness_session_ids``: antigravity) is minted nothing: its
    store's session ids are listed before the TTY starts, and the tail
    follows the one that appears after them.

    ``on_ready()`` fires once the TTY is up. A runner that raises (the TTY
    could not start or crashed) raises out of here."""
    from _agent_shared import surface_runner
    from _agent_shared.harnesses import resolve
    from _agent_shared.harness_homes import record_resume_level
    from _agent_shared.acp_run import close_acp_sessions, mint_acp_session

    spec = backend_spec(cfg["backend"])
    sid = ""
    if spec.terminal_session_tier in ("A", "B") and not spec.harness_session_ids:
        try:
            sid = await mint_acp_session(cfg)
            _secret_log(f"minted ACP session {sid} for the terminal")
        except Exception as exc:  # noqa: BLE001
            _secret_log(f"session mint failed ({exc}) — fresh terminal")
    # One session, one owner: no engine-side client stays open on the store
    # the TTY is about to write.
    await close_acp_sessions()
    # The store's sessions before the TTY starts: the harness home keeps this
    # cell's earlier conversations, and none of those is this leg's. A store
    # that cannot be listed raises — the TTY never starts on a leg that could
    # not tell its conversation from an earlier one.
    list_sessions = (resolve(spec.harness_session_ids)
                     if spec.harness_session_ids else None)
    leg_before = (frozenset(await asyncio.to_thread(list_sessions))
                  if list_sessions is not None else None)

    # The live tail starts with the TTY, not with the leg: before on_ready the
    # harness's store may not exist yet. It is cancelled in the finally below,
    # after the runner's own teardown export — the last reconcile.
    tail_task: "asyncio.Task | None" = None
    tty_up = False

    def _ready(harness_session_id: str, before: "frozenset | None" = None) -> None:
        # ``harness_session_id``: the session the TTY runs on — the minted
        # one, or one a fresh TTY names itself at launch (a claude TTY's own
        # ``--session-id`` when no session could be minted). ``before``: a
        # fresh TTY's store snapshot, taken by its runner (an opencode or
        # goose TTY that did not get a minted session).
        nonlocal tail_task, tty_up
        tty_up = True
        if tail_task is None:
            tail_task = _start_tail(cfg, harness_session_id or sid,
                                    before=before if before is not None else leg_before)
        if on_ready is not None:
            on_ready()

    try:
        # The TTY runs at this leg's level and can write the whole store, the
        # session it runs on included: record that before it starts (raises,
        # so the TTY never starts, when the record cannot be kept).
        record_resume_level(cfg["backend"], sid, cfg["commands"])
        runner = surface_runner(cfg["backend"], "terminal")
        await runner(cfg, pointer=paths.terminal_pointer,
                     extra_system=with_terminal_skills(cfg, paths),
                     resume_harness_session_id=sid,
                     first_message=opening_message(cfg, selected),
                     on_ready=_ready)
    finally:
        try:
            await _stop_tail(tail_task)
        finally:
            # Whatever ended the leg — Stop, the program exiting, a crashed
            # TTY — its transcript is settled here: the marker for a harness
            # the tail cannot read. A leg whose TTY never came up gets no
            # marker — there was no terminal conversation to be opaque about.
            terminal_tail.end_of_leg(cfg["backend"], ran=tty_up)


def _start_tail(cfg: dict, harness_session_id: str, *,
                before: "frozenset | None") -> "asyncio.Task | None":
    """Arm the live transcript tail for this leg (``before``: its store's
    session ids when it started — :func:`terminal_tail.make_tail_reader`), or
    ``None`` when the harness has no readable record (tier C — the
    end-of-leg marker covers it) or the task could not be started. Never
    raises: a leg without a tail is reconciled by its runner's teardown
    export alone."""
    backend = str(cfg.get("backend") or "")
    if not terminal_tail.has_tail_reader(backend):
        return None
    try:
        return asyncio.ensure_future(terminal_tail.tail_transcript(
            backend, harness_session_id=harness_session_id,
            before=before, interval=terminal_tail.TAIL_INTERVAL_S,
        ))
    except Exception as exc:  # noqa: BLE001
        _secret_log(f"terminal tail could not start: {exc}")
        return None


async def _stop_tail(task) -> None:
    """End the tail. The runner's teardown export has already run by the time
    the leg's ``finally`` gets here, so the tail has nothing left to add — and
    could only re-merge (idempotently) what is already on file."""
    if task is None:
        return
    task.cancel()
    try:
        await asyncio.gather(task, return_exceptions=True)
    except Exception:  # noqa: BLE001 — teardown is best-effort by contract
        pass


async def run_terminal(sess) -> None:
    """A terminal run: the TTY leg, then what ended it decides the run's end.

    * Stop — nothing more; the run end follows.
    * The TTY's program exited, or the runner returned on its own — the run
      ends with it.
    * The TTY could not start or crashed — ``error.md`` says why and the run
      ends failed (``sess.terminal_error``). A Stop that surfaces as an error
      is still a Stop."""

    def _on_ready() -> None:
        sess.tty_up = True

    try:
        await terminal_leg(sess.cfg, paths=sess.paths, on_ready=_on_ready,
                           selected=sess.selected)
    except gen.Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        if gen.cancelled:
            return          # a Stop unwinding through the runner
        await _terminal_failed(sess, exc)
        return
    if gen.cancelled:
        return
    sess.finish_run("the terminal ended — the run ends with it")


async def _terminal_failed(sess, exc: BaseException) -> None:
    """The run ends failed, with ``error.md`` saying why — flushed here: a
    failed run's writes are not auto-flushed."""
    backend = sess.cfg["backend"]
    _secret_log(f"terminal failed: {exc}")
    sess.terminal_error = f"the {backend} terminal failed: {exc}"
    try:
        gen.update_file(
            "error.md",
            f"# Terminal failed\n\nThe `{backend}` terminal surface failed, "
            f"so the run ended:\n\n```\n{exc}\n```\n",
        )
        await gen.commit()
    except Exception:  # noqa: BLE001 — the run still ends failed
        pass
    sess.finish_run()
