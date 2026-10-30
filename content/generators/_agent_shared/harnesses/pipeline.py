# SPDX-License-Identifier: MIT
"""The generic harness preparation every ACP harness and CLI leg goes through.

:func:`prepare` takes a :class:`HarnessAdapter` and a leg's normalized config
and runs the steps every harness shares, in order, before and after the
harness's own renderer (``adapter.prepare``):

1. **auth and provider** — the auth must be one the adapter offers and the
   provider one it bills (a plan pins its provider); the adapter's own id for
   that provider rides along (``ctx["native_provider"]``);
2. **commands level** — the grant's level, resolved down to one the
   harness's native mechanism can hold (``Commands.levels``), with a notice;
3. **credentials** — the selected account's API key, or the run's leased
   plan login (refused before anything starts when the lease holds none), as
   ``ctx["credentials"]``; renderers never read an account themselves. A
   CLI leg on this machine under a plan takes none: the vendor's CLI signs
   in itself, in its per-account home (``cli_host``);
4. **instructions** — a ``home_file`` seam is written into the per-cell
   harness home (or removed when the run carries no prompt);
5. the **renderer** — native config, the commands policy, the container
   (``cli_container.ensure_env``), argv, env and spawn;
6. the **spawn profile's facets** — the session flags every harness
   declares statically (strict permission kinds, one session per process,
   model matching and verification, the post-load check) and the fold of the
   system prompt where no seam carried it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from . import CommandsPolicy, HarnessAdapter, PreparedHarness, resolve, terminal_locus
from ..accounts import account_api_key
from ..login_store import login_path, login_required, selected_login_ready


@dataclass(frozen=True)
class Credentials:
    """What bills a leg: ``key`` under an API key, ``login`` (the leased
    login's path) under a plan that leases a document; a plan carried as a
    token (claude's) has neither and its renderer reads the token."""
    provider: str
    plan: str
    key: str = ""
    login: str = ""


def _plan_providers() -> dict:
    from collimate.account_taxonomy import PLAN_PROVIDERS
    return PLAN_PROVIDERS


def selection(adapter: HarnessAdapter, ctx: dict) -> tuple[str, str, str]:
    """``(auth, plan, provider)`` for a leg, checked against the adapter:
    an auth it does not offer or a provider it does not bill raises
    ``ValueError`` naming what it does."""
    auth = (ctx.get("auth") or "api_key").strip().lower()
    if auth not in adapter.auths:
        raise ValueError(f"auth {auth!r} isn't honored by backend {adapter.id!r} — "
                         f"it offers {', '.join(adapter.auths)}")
    plan = "" if auth == "api_key" else auth
    if plan:
        provider = _plan_providers()[plan]
    else:
        provider = (ctx.get("provider") or adapter.auth.default_provider).strip().lower()
    adapter.native_provider(provider, plan)
    return auth, plan, provider


async def credentials(adapter: HarnessAdapter, *, provider: str, plan: str) -> Credentials:
    """The leg's credential, resolved once from the selected model account."""
    if not plan:
        return Credentials(provider, "", key=await account_api_key(provider))
    if adapter.auth.login is None:
        return Credentials(provider, plan)
    if not await asyncio.to_thread(selected_login_ready, adapter.id, plan):
        raise RuntimeError(f"{adapter.id} auth={plan}: {login_required(plan=plan, harness=adapter.id)}")
    return Credentials(provider, plan, login=await asyncio.to_thread(login_path, adapter.id))


async def write_instructions(adapter: HarnessAdapter, system_prompt: str) -> bool:
    """Write a ``home_file`` seam — the prompt, or no file when there is
    none (a stale seam from an earlier leg must not keep instructing this
    one) — and report whether it now carries the prompt."""
    from ..harness_files import write_native_file
    from ..harness_homes import harness_home
    body = (system_prompt or "").strip()
    home = await asyncio.to_thread(harness_home, adapter.id)
    await asyncio.to_thread(write_native_file, home, adapter.instructions.target,
                            (body + "\n").encode() if body else None)
    return bool(body)


def with_facets(adapter: HarnessAdapter, profile, notice: str = ""):
    """``profile`` carrying the adapter's static session facets."""
    flags: dict = {}
    if adapter.commands.strict_kinds:
        flags["strict_permission_kinds"] = True
    if adapter.session.one_per_process:
        flags["one_session_per_process"] = True
    if adapter.model.verify:
        flags["verify_model"] = True
    if adapter.model.matches and profile.model_matches is None:
        flags["model_matches"] = resolve(adapter.model.matches)
    if adapter.session.verify_loaded and profile.verify_loaded is None:
        flags["verify_loaded"] = resolve(adapter.session.verify_loaded)
    if notice and not profile.commands_notice:
        flags["commands_notice"] = notice
    return replace(profile, **flags) if flags else profile


async def prepare(adapter: HarnessAdapter, ctx: dict, *, leg: str = "acp", **kw) -> PreparedHarness:
    """Prepare one leg (``acp`` or ``terminal``) of ``adapter``'s harness."""
    if leg not in ("acp", "terminal"):
        raise ValueError(f"unsupported {adapter.id} leg: {leg}")
    auth, plan, provider = selection(adapter, ctx)
    level, notice = adapter.resolve_commands((ctx.get("commands") or "deny").strip().lower())
    if notice:
        from ..logging import safe_log
        safe_log(f"warning: {notice}")
    # A CLI on this machine signs in itself: a plan leases it nothing.
    if plan and leg == "terminal" and terminal_locus(adapter, auth) == "host":
        creds = Credentials(provider, plan)
    else:
        creds = await credentials(adapter, provider=provider, plan=plan)
    ctx = {**ctx, "auth": auth, "provider": provider, "commands": level, "credentials": creds,
           "native_provider": adapter.native_provider(provider, plan)}
    if adapter.instructions.seam == "home_file":
        ctx["instructions_written"] = await write_instructions(adapter, ctx.get("system_prompt", ""))
    prepared = await resolve(adapter.prepare)(ctx, leg=leg, **kw)
    if not isinstance(prepared, PreparedHarness):
        raise TypeError(f"{adapter.id}: its renderer must return a PreparedHarness")
    commands = prepared.commands
    if notice and not commands.notice:
        commands = CommandsPolicy(commands.level, commands.native, notice)
    profile = prepared.profile
    if profile is not None:
        if adapter.instructions.seam == "home_file":
            profile = replace(profile, fold_system_prompt=not ctx["instructions_written"])
        profile = with_facets(adapter, profile, commands.notice)
    return replace(prepared, commands=commands, profile=profile)


__all__ = ["Credentials", "credentials", "prepare", "selection", "with_facets", "write_instructions"]
