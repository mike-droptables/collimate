# SPDX-License-Identifier: MIT
"""Private terminal lifecycle; native contexts own launch facts and finalization.

A leg's TTY runs where its harness's ``Terminal.locus`` puts it for the run's
auth (:func:`harnesses.terminal_locus`): in the harness's container
(``cli_container``) or on this machine (``cli_host``). The native context
says which by the launch it yields: a container id, or ``None`` for this
machine.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

import gen

from .harnesses import HARNESS, Terminal, resolve, terminal_locus
from .run_files import AGENT_LIVE_SKIP_GLOBS


@dataclass(frozen=True)
class TerminalRequest:
    resume_harness_session_id: str
    first_message: str
    extra_system: str


@dataclass(frozen=True)
class TerminalLaunch:
    """One TTY to serve. ``container_id`` is the leg's container, or
    ``None`` for a leg on this machine: ``argv`` then runs on the host's PTY
    with ``env`` as its whole environment and ``cwd`` the cell's real
    workspace path."""
    container_id: str | None
    argv: tuple[str, ...]
    cwd: str
    env: dict
    label: str
    buttons: list
    ready_status: str
    harness_session_id: str
    before: frozenset | None = None
    auxiliary_handlers: dict | None = None

    @property
    def locus(self) -> str:
        return "host" if self.container_id is None else "container"


async def run_terminal(
    cfg: dict, *, pointer: str, extra_system: str = "", label: str | None = None,
    resume_harness_session_id: str = "", first_message: str = "",
    on_ready=None,
) -> None:
    from .accounts import git_harness_environment
    row = HARNESS[cfg['backend']]
    facet = row.terminal
    if not isinstance(facet, Terminal):
        raise ValueError(f"harness {row.id!r} has no shared terminal context")
    name = facet.default_label if label is None else label
    host = terminal_locus(row, str(cfg.get('auth') or 'api_key')) == 'host'
    await gen.commit(name=name)
    gen.phase('spinning_up', 'starting the CLI on this machine' if host else 'starting container')
    request = TerminalRequest(resume_harness_session_id, first_message, extra_system)
    preparation: dict = {'leg': 'terminal'}
    if facet.prepare_with_request:
        preparation['request'] = request
    from .harnesses.pipeline import prepare
    prepared = await prepare(row, {**cfg, 'system_prompt': extra_system}, **preparation)
    async with AsyncExitStack() as stack:
        if facet.lifetime_context is not None:
            await stack.enter_async_context(resolve(facet.lifetime_context)(prepared, request))
        bridge = None
        if facet.bridge_enabled:
            from .cli_container import leg_bridge
            from .generator_test import CommandContext
            from .workspace_tools import tool_entries
            context = (CommandContext('host') if host
                       else CommandContext('container', env_obj=prepared.env_obj))
            rows = tool_entries(enable_file_tools=False, websearch=bool(cfg.get('websearch')),
                commands=prepared.commands.level, command_context=context)
            # A CLI on this machine reaches its bridge on loopback (url_host).
            bridge = await stack.enter_async_context(leg_bridge(
                extra_entries=rows or None, **({'bind_host': '127.0.0.1'} if host else {})))
        async with resolve(facet.native_context)(prepared, request, bridge) as launch:
            if (launch.locus == 'host') != host:
                raise RuntimeError(f"{row.id}: its terminal context launched in the "
                                   f"{launch.locus} where its locus is {'host' if host else 'container'}")
            gen.phase('spinning_up', 'Registering terminal session…')
            async with gen.serve_terminal(
                launch.container_id, list(launch.argv), cwd=launch.cwd,
                env={**launch.env, **git_harness_environment(cfg['backend'], launch.locus)},
                label=launch.label, pointer=pointer, keep_alive=True,
                buttons=launch.buttons, name=name,
            ) as tty:
                gen.phase('running', 'terminal ready')
                gen.phase('running', launch.ready_status)
                if on_ready is not None:
                    if launch.before is None:
                        on_ready(launch.harness_session_id)
                    else:
                        on_ready('', before=launch.before)
                from . import serve_tty_until
                async with gen.live_files(skip_globs=AGENT_LIVE_SKIP_GLOBS):
                    await serve_tty_until(terminal_id=tty['terminal_id'],
                                          on_action=launch.auxiliary_handlers)
