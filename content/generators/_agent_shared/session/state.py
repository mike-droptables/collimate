# SPDX-License-Identifier: MIT
"""The agent session: one run, one surface — annotation, chat or terminal.

A run of the Agent cell's generator works on the surface its ``surface``
setting picks and keeps it. :class:`AgentSession` holds that surface and
everything the run's lifecycle needs: the host-refusal corrections, the
one-shot's self-ending pass and the turn boundaries. The sibling modules
``annotation`` and ``terminal`` implement the two surfaces that are more
than a chat on it. ``entry.run_session`` builds one per run and hands
``serve_chat`` its ``on_turn`` hooks. A run ends when the user stops it, a
terminal's program exits, or a one-shot ends itself.

A ONE-SHOT is an annotation run whose prompt is its first message: it
answers it in one opening turn and ends ITSELF once that turn — plus any
correction turn it pulled behind it — has settled and its final push is
acknowledged.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio

import gen


from . import annotation, terminal
from .consts import OPTION_POLL_S, SessionPaths, Sources
from .corrections import Corrections


class AgentSession:
    """The run's surface and everything its lifecycle needs.

    ``paths`` are the run's surface files, all in its run folder
    (:class:`consts.SessionPaths`). ``surface`` is the run's one surface.
    ``annotations`` is the annotation surface's leg
    (:class:`annotation.AnnotationLeg`, set by ``annotation.open_annotations``);
    ``corrections`` the host-refusal corrections (annotation and chat).
    ``oneshot`` / ``oneshot_turn_done`` / ``finish_requested`` are the
    one-shot's self-ending pass, and ``flush_output`` the live mirror's flush
    its final push acknowledges. ``oneshot_error`` / ``oneshot_stopped`` say
    why a one-shot ended without an answer; ``tty_up`` whether a terminal's
    TTY came up (its runner then ends by itself on a Stop, teardown
    included); ``terminal_error`` why a terminal run ends failed;
    ``refused`` the model service's refusal of a turn (:meth:`end_refused`),
    which ends any run failed. ``selected`` is the selected file as the first
    message and ``selected_source`` the source of the turn that carries its
    media until that turn has run."""

    def __init__(self, cfg: dict, *, paths: "SessionPaths | None" = None) -> None:
        self.cfg = cfg
        self.paths = paths or SessionPaths.for_run(cfg["backend"])
        self.surface = cfg["surface"]
        self.oneshot = self.surface == "annotation" and cfg["session"] == "oneshot"
        self.annotations = None
        self.corrections = Corrections(oneshot=self.oneshot)
        self.finish_requested = False
        # The one-shot pass: `oneshot_turn_done` once the OPENING turn has
        # ended. Why a one-shot ended without an answer: its opening FAILED
        # (`oneshot_error`, the explained failure) or the user STOPPED it
        # (`oneshot_stopped`). The entrypoint raises on either once the run
        # end has logged the session, so the host sees a failed run.
        self.oneshot_turn_done = False
        self.oneshot_error = ""
        self.oneshot_stopped = False
        self.tty_up = False
        self.terminal_error = ""
        self.refused = ""
        self.trace: list = []           # the run's accumulated raw record
        self.selected = None
        self.selected_source: "str | None" = None
        # The live mirror's flush (bound by the entrypoint): the one-shot's
        # final push waits on it.
        self.flush_output = None

    def say(self, text: str, *, log: bool = True) -> None:
        """Tell the user something where they are looking: a chat run's
        transcript, an annotation run's context (a top-level note); a
        terminal has only its TTY. The run log gets it too (``log``)."""
        if log:
            _secret_log(text)
        try:
            if self.surface == "chat":
                gen.history.append("system", text)
            elif self.surface == "annotation":
                annotation.note(self, text)
        except Exception as exc:  # noqa: BLE001 — never block the caller
            _secret_log(f"could not show the notice on the surface ({exc})")

    def finish_run(self, message: str = "") -> None:
        """Ask serve_chat to end the run once idle."""
        gen.session.request_finish()
        self.finish_requested = True
        if message:
            _secret_log(message)

    def end_refused(self) -> None:
        """The model service refused a turn (``TurnRefused``): every later
        turn — a queued batch, a correction — would be refused the same way,
        so none of them is sent (the entrypoint skips every turn still
        queued), and the run ends failed with the refusal: the entrypoint
        raises ``refused`` once the run end has logged the session."""
        self.finish_run(f"{self.refused} — ending the run failed")

    # ── the one-shot's self-ending pass ─────────────────────────────

    async def try_oneshot_finish(self, turn, *, stopped: bool, failed: str) -> None:
        """After every turn of a one-shot: once the opening turn AND the
        corrections it pulled behind it have settled, acknowledge the final
        push and finish the run.

        A stopped or failed opening ends the run with no result. A
        correction that stops ends it stopped; one that fails settles the
        pass without a further correction."""
        source = getattr(turn, "source", "")
        if source == Sources.OPENING:
            self.oneshot_turn_done = True
            if stopped:
                self.oneshot_stopped = True
                self.finish_run("one-shot stopped by the user — finishing with no result")
                return
            if failed:
                self.oneshot_error = failed
                self.finish_run(f"one-shot failed ({failed}) — finishing with no result")
                return
        elif source != Sources.CORRECTION or not self.oneshot_turn_done:
            return
        elif stopped:
            self.oneshot_stopped = True
            self.finish_run("one-shot correction stopped by the user")
            return
        elif failed:
            await self._settle_oneshot(allow_correction=False)
            return
        if self.corrections.pending:
            return
        await self._settle_oneshot(allow_correction=True)

    async def _settle_oneshot(self, *, allow_correction: bool) -> None:
        """The final push: flush the live mirror and acknowledge every staged
        write. A host refusal (``gen.PartialRefusal``) queues its correction
        (the pass waits for it) while corrections remain; anything else, or
        a refusal past them, ends the run failed."""
        try:
            if self.flush_output is not None:
                await self.flush_output()
            await gen.commit()
        except (asyncio.CancelledError, gen.Cancelled):
            raise
        except gen.PartialRefusal as exc:
            detail = f"Final output was not acknowledged: {exc}"
            if allow_correction and not gen.cancelled:
                self.corrections.refused_exc(exc)
                if self.corrections.pending:
                    _secret_log(detail)
                    return
            self.oneshot_error = detail
            self.finish_run(detail)
            return
        except Exception as exc:  # noqa: BLE001 — an unacknowledged push fails the run
            detail = f"Final output was not acknowledged: {exc}"
            self.oneshot_error = detail
            self.finish_run(detail)
            return
        self.finish_run("one-shot finished")

    # ── turn boundaries (called by the entrypoint's on_turn) ────────

    async def on_turn_end(self, turn, *, stopped: bool, failed: str = "") -> None:
        """The turn boundary: the corrections ledger, the annotation batch's
        settling and the one-shot's finish. ``failed`` is the explained error
        of a turn whose ``run_agent`` raised ("" when it completed)."""
        source = getattr(turn, "source", "")
        self.corrections.after_turn(source, stopped=stopped, failed=failed)
        if self.annotations is not None:
            await annotation.after_turn(self, turn, stopped=stopped, failed=failed)
        if self.refused:
            self.end_refused()
            return
        if self.oneshot and not self.finish_requested and not gen.cancelled:
            await self.try_oneshot_finish(turn, stopped=stopped, failed=failed)

    # ── the companion loops ─────────────────────────────────────────

    async def config_loop(self, ready) -> None:
        """The run's live options (``acp_run``). Once the session has
        booted it publishes Collimate's own (claude's ultracode)
        — the harness's follow when its ACP session opens — then polls the
        user's changes (``gen.pop_option_change``) and applies each, to the
        run's open sessions or recorded for the next open, republishing the
        set after every one, applied or not, so the status pane settles the
        value it showed as pending.

        A terminal run publishes nothing and polls nothing: its TTY is the
        harness's own UI, with its own model, effort and mode controls (and
        claude's own ultracode toggle), and the host refuses every change to
        an empty set."""
        from _agent_shared.acp_run import apply_config_change, publish_run_options

        cfg = self.cfg
        await ready.wait()
        if self.surface == "terminal":
            return
        publish_run_options(cfg["backend"])
        while True:
            try:
                change = await asyncio.to_thread(gen.pop_option_change)
            except (asyncio.CancelledError, gen.Cancelled):
                raise
            except Exception as exc:  # noqa: BLE001 — never kill the poll loop
                _secret_log(f"option changes could not be read ({exc})")
                change = None
            if change is None:
                await asyncio.sleep(OPTION_POLL_S)
                continue
            try:
                await apply_config_change(cfg["backend"], change["id"], change["value"],
                                          commands=cfg["commands"])
                _secret_log(f"option {change['id']} set to {change['value']!r}")
            except (asyncio.CancelledError, gen.Cancelled):
                raise
            except Exception as exc:  # noqa: BLE001 — never kill the poll loop
                self.say(f"{change['id']} could not be changed: {exc}")
            publish_run_options(cfg["backend"], force=True)

    async def surface_loop(self, ready) -> None:
        """The run's surface, once serve_chat has booted (anything earlier
        would be clobbered by the boot reset): a terminal run hands the
        session to the TTY (``terminal.run_terminal``); an interactive
        annotation run starts reading its messages (``annotation.start``).
        A chat and a one-shot need nothing more."""
        await ready.wait()
        if self.surface == "terminal":
            await terminal.run_terminal(self)
            return
        if self.surface == "annotation" and not self.oneshot:
            annotation.start(self)

    # ── teardown ────────────────────────────────────────────────────

    async def close(self) -> None:
        """Run-end teardown: the annotation leg's subscription and watcher."""
        if self.annotations is not None:
            await annotation.close(self)
