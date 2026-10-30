"""Plan logins the Key manager takes as PASTED documents, and how it
verifies them. Nothing here starts a process, opens a browser or parses a
CLI's output: the user signs in with the app in a terminal as they always
have, and pastes what that sign-in wrote — the app's own login file, which is
exactly the document the runtime materialises for a run. Claude is the one
token rather than a file: ``claude setup-token`` prints a year-long token that
is pasted as the ``subscription`` access token, and Verify checks it against
the API. Every other plan is checked offline: the document's shape for the
selected plan, and its expiry where the document says so. Model lists for a
plan come from the API where the token is accepted (Claude), otherwise from
the plan's own default. Each harness's login facts (the sign-in command, the
file it writes, the secret the pasted document is saved as, the entry each
plan occupies in a shared store) are the SDK's PLAN_LOGINS rows in
collimate.account_taxonomy.

A plan whose CLI surface runs the vendor's CLI on this machine (the SDK's
``signs_in_on_host``: Claude Code, Antigravity on the Google plan) is valid
with nothing pasted: that CLI signs in itself, once per Key manager entry,
and only the Annotation and Chat surfaces need the token or document.
"""
from __future__ import annotations

import os
from pathlib import Path

from collimate.account_taxonomy import (
    HARNESS_HOST_BINARY, HOST_SIGN_IN_PLANS, MODEL_HARNESS_LABELS, PLAN_LOGINS, PlanLogin,
    default_model_for, signs_in_on_host,
)
from collimate.harness_catalog import HARNESSES

from accounts import AccountError


CLAUDE_COMMAND = "claude setup-token"


def store_path(login: PlanLogin) -> Path | None:
    """The file the app's sign-in writes on this machine, honouring the app's
    own override — the path the form tells the user to paste from. Where an
    install keeps it elsewhere (a Snap's own home), the file that EXISTS wins;
    only existence is looked at, never contents. ``None`` for a login that is
    a token the user creates (Copilot's), which no sign-in writes."""
    if not login.relative:
        return None
    value = os.environ.get(login.env) if login.env else None
    if value:
        return Path(value) / login.suffix if login.suffix else Path(value)
    default = Path.home() / login.relative
    for relative in (login.relative, *login.also):
        candidate = Path.home() / relative
        if candidate.is_file():
            return candidate
    return default


def plan_login_catalog() -> dict:
    """What the Key manager tells a user about each app's login: the command
    that signs in, the file it writes, the slot the pasted contents go to,
    and the sentence shown under the document field."""
    catalog = {}
    for name, login in PLAN_LOGINS.items():
        path = store_path(login)
        store = str(path) if path else ""
        # The document is optional where the app's CLI surface signs in on
        # this machine.
        optional = any((name, plan) in HOST_SIGN_IN_PLANS for plan in HARNESSES[name]["plans"])
        catalog[name] = {"command": login.command, "store": store, "field": login.field,
                         "hint": login.hint.format(store=store), "optional": optional}
    return catalog


def _signs_in_here(account: dict) -> str:
    """For an entry with nothing pasted whose CLI surface signs in on this
    machine, the Verify sentence that says so; ``""`` otherwise."""
    if not signs_in_on_host(account):
        return ""
    harness = str((account.get("metadata") or {}).get("harness") or "claude-code")
    binary = HARNESS_HOST_BINARY.get(harness, harness)
    return (f"nothing is pasted: the CLI surface runs {MODEL_HARNESS_LABELS.get(harness, harness)}'s "
            f"{binary} on this machine, which signs in itself the first time (one sign-in per Key manager "
            "entry); the Annotation and Chat surfaces need the pasted credential")




def document_problem(content: str, harness: str, plan: str = "") -> str | None:
    return PLAN_LOGINS[harness].document_problem(content, plan)


# codex refreshes a login whose access token is near expiry on first use, but
# stops trusting one it has not refreshed for this long (manager.rs).



def document_note(content: str, harness: str, plan: str = "") -> str:
    return PLAN_LOGINS[harness].document_note(content, plan)






def claude_token(account: dict) -> str:
    return (account.get("secrets") or {}).get("access_token") or ""


def _is_claude(account: dict) -> bool:
    return account.get("auth_type") == "subscription" or (account.get("metadata") or {}).get("harness") == "claude-code"


def plan_login(account: dict) -> PlanLogin:
    harness = (account.get("metadata") or {}).get("harness", "")
    if harness not in PLAN_LOGINS:
        raise AccountError("Choose the app this login belongs to")
    return PLAN_LOGINS[harness]


def check_plan_login(account: dict) -> dict:
    """Verify a pasted plan login: one bearer request for a Claude token; the
    document's shape and expiry for every other plan."""
    if _is_claude(account):
        token = claude_token(account)
        if not token:
            here = _signs_in_here(account)
            if here:
                return {"valid": True, "message": f"{here} (the token from `{CLAUDE_COMMAND}`)"}
            return {"valid": False, "message": f"No Claude token is entered. Run `{CLAUDE_COMMAND}` in a terminal on this machine and paste the token it prints"}
        from account_adapters import request_service, claude_oauth_headers
        from account_catalog import endpoint
        request_service("GET", endpoint(account) + "/models", params={"limit": 1}, headers=claude_oauth_headers(token))
        return {"valid": True, "message": "Claude token verified against the API"}
    login = plan_login(account)
    plan = str(account["metadata"].get("plan") or "")
    store = store_path(login)
    # A login no sign-in writes is a token the user creates; its hint says where.
    what, paste = ("login document", f"Sign in with the app in a terminal on this machine (`{login.command}`), "
                                     f"then paste the contents of {store}") if store else ("token", login.hint)
    content = account["secrets"].get(login.field) or ""
    if not content:
        here = _signs_in_here(account)
        if here:
            return {"valid": True, "message": f"{here} ({os.path.basename(login.relative) or what})"}
        return {"valid": False, "message": f"No {login.harness} {what} is entered. {paste}"}
    problem = document_problem(content, login.harness, plan)
    if problem:
        return {"valid": False, "message": f"The pasted {what.split()[-1]} is not a {plan or login.harness} login: {problem}."}
    if not store:
        return {"valid": True, "message": f"{login.harness} token present; its plan permission is checked at the run's first turn"}
    note = document_note(content, login.harness, plan)
    return {"valid": True, "message": f"{login.harness} login present" + (f"; {note}" if note else "")}


def blank_model_note(harness: str) -> str:
    """What a blank model means where the harness keeps its own default:
    the one sentence Verify and the plan's model list both say."""
    return f"with no model chosen, {harness} picks its own default model"


def plan_models(account: dict) -> dict:
    """The models a plan login can select: the provider's list where its API
    takes the login's token (Claude), otherwise the plan's own default."""
    if _is_claude(account):
        token = claude_token(account)
        if token:
            from account_adapters import discover_models
            return discover_models(account, bearer=token)
        return {"models": [], "supported": False, "message": f"Paste the token from `{CLAUDE_COMMAND}` to list Claude's models"}
    login = plan_login(account)
    default = default_model_for(account)
    rows = [{"id": default, "name": default, "capabilities": ["text"], "capabilities_source": "plan"}] if default else []
    if default:
        offer = f"{default} is the plan's default — enter another model ID your plan offers"
    elif default == "":
        offer = f"{blank_model_note(login.harness)}, or enter a model ID your plan offers"
    else:
        offer = "enter a model ID your plan offers"
    return {"models": rows, "supported": False,
            "message": f"{login.harness} exposes no model list to query; {offer}"}
