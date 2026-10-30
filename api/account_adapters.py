"""Account-owned authentication and explicit manager discovery operations.

``check_account``, ``check_draft`` and ``discover_models`` are called only by
manager routes. Runtime consumers use saved snapshots; they never call the
check/discovery operations as a preflight.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

import httpx
from collimate import git_hosts
from collimate.account_taxonomy import (
    DEFAULT_MODEL_HARNESS, CLAUDE_MODEL_ALIASES, HARNESS_DEFAULT_MODEL_OVERRIDES, HARNESS_MODEL_VOCABULARY,
    MODEL_API_PROTOCOLS, UNCHECKED_MODEL_CONSEQUENCE,
    UNCHECKED_MODEL_FALLBACK, default_model_for, is_cli_model_login, model_account_auth, model_account_provider,
    supported_model_harnesses,
)
from collimate.providers import PROVIDER_KEY_ENV

import account_catalog
from accounts import AccountError
from plan_logins import blank_model_note

_OPENAI_PROTOCOLS = frozenset(protocol for protocol, family in MODEL_API_PROTOCOLS.items() if family == "openai")


def secret_environment(account: dict) -> dict[str, str]:
    secret = account.get("secrets", {})
    result = {}
    api_key = secret.get("api_key")
    name = PROVIDER_KEY_ENV.get(account["service"])
    if api_key and name:
        result[name] = api_key
    if account["service"] == "anthropic" and account["auth_type"] == "subscription":
        token = secret.get("access_token")
        if token:
            result["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if account.get("protocol") in _OPENAI_PROTOCOLS:
        if api_key:
            result["OPENAI_API_KEY"] = api_key
        if account.get("endpoint"):
            result["OPENAI_BASE_URL"] = account["endpoint"]
    return result


def request_service(method: str, url: str, **kwargs) -> dict:
    """Bounded, no-redirect service request with sanitized errors."""
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.request(method, url, **kwargs)
        response.raise_for_status()
        result = response.json() if response.content else {}
    except httpx.HTTPStatusError as exc:
        raise AccountError(f"Service returned HTTP {exc.response.status_code}") from None
    except httpx.RequestError:
        raise AccountError("Could not connect to the account's service") from None
    except (ValueError, TypeError):
        raise AccountError("Service returned an unreadable response") from None
    if not isinstance(result, dict):
        raise AccountError("Service returned an unexpected response")
    return result


def _headers(account: dict) -> dict[str, str]:
    secret = account.get("secrets", {})
    token = secret.get("access_token") or secret.get("api_key") or secret.get("token")
    protocol = account["protocol"]
    if protocol == "anthropic":
        return {"x-api-key": token or "", "anthropic-version": "2023-06-01"}
    if protocol == "gemini":
        return {"x-goog-api-key": token or ""}
    if account["auth_type"] == "basic":
        username = account.get("metadata", {}).get("username", "")
        encoded = base64.b64encode(f"{username}:{secret.get('password', '')}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {token}"} if token else {}


def claude_oauth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}


def discover_models(account: dict, *, bearer: str | None = None) -> dict:
    """Explicit key-manager action; never infer capabilities from model names.
    A plan login lists through ``plan_logins.plan_models``; it passes
    ``bearer`` back here when the provider's API takes the login's token."""
    protocol = account["protocol"]
    if is_cli_model_login(account) and bearer is None:
        from plan_logins import plan_models
        return plan_models(account)
    if bearer:
        # A plan login lists through the SERVICE's API, whatever harness
        # protocol the entry itself is filed under.
        protocol = next((row["protocol"] for row in account_catalog.service_catalog() if row["id"] == account["service"]), protocol)
    headers = claude_oauth_headers(bearer) if bearer else _headers(account)
    if protocol not in MODEL_API_PROTOCOLS:
        return {"models": [], "supported": False, "message": "This service does not expose model discovery; enter the model manually"}
    base = account_catalog.endpoint(account)
    if not base:
        raise AccountError("Set the service endpoint in Key manager")
    models = []
    cursor = None
    for _ in range(20):
        params = {}
        if protocol == "gemini":
            params["pageSize"] = 100
            if cursor:
                params["pageToken"] = cursor
        elif protocol == "anthropic":
            params["limit"] = 100
            if cursor:
                params["after_id"] = cursor
        data = request_service("GET", base + "/models", headers=headers, params=params)
        rows = data.get("models", []) if protocol == "gemini" else data.get("data", [])
        if not isinstance(rows, list):
            raise AccountError("Service returned an invalid model list")
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = row.get("name", "") if protocol == "gemini" else row.get("id", "")
            if not isinstance(identity, str) or not identity:
                continue
            if protocol == "gemini":
                identity = identity.removeprefix("models/")
            caps = row.get("capabilities", [])
            if isinstance(caps, dict):
                caps = [key for key, enabled in caps.items() if enabled is True]
            if not isinstance(caps, list):
                caps = []
            caps = [cap for cap in caps if isinstance(cap, str) and cap]
            if protocol == "gemini":
                methods = row.get("supportedGenerationMethods", [])
                if isinstance(methods, list):
                    if "generateContent" in methods:
                        caps.append("text")
                    if "embedContent" in methods:
                        caps.append("embeddings")
            # Some compatible endpoints (such as OpenRouter) advertise
            # modalities and supported parameters alongside each model.
            # Use explicit facts only; a model's name proves no capability.
            architecture = row.get("architecture", {})
            if isinstance(architecture, dict):
                modalities = architecture.get("input_modalities", [])
                if isinstance(modalities, list):
                    if "image" in modalities:
                        caps.append("vision")
                    if "audio" in modalities:
                        caps.append("audio")
                outputs = architecture.get("output_modalities", [])
                if isinstance(outputs, list) and "text" in outputs:
                    caps.append("text")
            parameters = row.get("supported_parameters", [])
            if isinstance(parameters, list):
                if "tools" in parameters:
                    caps.append("tools")
                if "reasoning" in parameters:
                    caps.append("reasoning")
            models.append({
                "id": identity, "name": row.get("display_name") or row.get("displayName") or row.get("name") or identity,
                "capabilities": list(dict.fromkeys(caps)),
                "capabilities_source": "service" if caps else "unavailable",
            })
        cursor = data.get("nextPageToken") if protocol == "gemini" else data.get("last_id") if data.get("has_more") else None
        if not cursor:
            break
    return {"models": models, "supported": True, "checked_at": datetime.now(timezone.utc).isoformat()}


def _git_needed(account: dict) -> str:
    """What an unconfigured kind-git entry lacks first: the commit author, a
    basic entry's server URL, its credential, an SSH entry's known_hosts."""
    metadata = account.get("metadata", {})
    secrets = account.get("secrets", {})
    auth = account.get("auth_type", "")
    if not (str(metadata.get("author_name") or "").strip() and str(metadata.get("author_email") or "").strip()):
        return "Enter the commit author name and email"
    if auth == "basic" and not account.get("endpoint"):
        return "Enter the server URL"
    if auth == "api_key" and not secrets.get("api_key"):
        return "Enter the personal access token"
    if auth == "basic" and not (secrets.get("password") and metadata.get("username")):
        return "Enter the username and token"
    if auth == "ssh_key" and not secrets.get("private_key"):
        return "Paste the SSH private key"
    if auth == "ssh_key":
        return "Paste the server's known_hosts line"
    return "Enter the credentials"


_GITLAB_NEEDS_READ_USER = ("GitLab accepted the token but not for its API: add the read_user scope so Verify can "
                           "sign in (clone and push need only read_repository or write_repository)")


def _check_git(account: dict) -> dict:
    """Verify a configured kind-git entry without starting a process: a forge
    token signs in to the forge's API, an SSH key is read offline (its host is
    never contacted), and a basic or public entry has no check."""
    checked_at = datetime.now(timezone.utc).isoformat()
    auth, service = account["auth_type"], account["service"]
    secrets = account.get("secrets", {})
    if auth == "ssh_key":
        try:
            info = git_hosts.inspect_private_key(secrets.get("private_key", ""))
            git_hosts.parse_known_hosts(str(account.get("metadata", {}).get("known_hosts") or ""))
        except ValueError as exc:
            return {"valid": False, "message": str(exc)}
        return {"valid": True, "message": f"Key accepted ({info.keytype} {info.fingerprint}); the host is not contacted",
                "public_key": info.public_key, "checked_at": checked_at}
    if auth == "api_key" and service in ("github", "gitlab"):
        api = git_hosts.git_api_base(service, account_catalog.endpoint(account))
        token = secrets.get("api_key", "")
        if service == "github":
            result = request_service("GET", f"{api}/user", headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            login = result.get("login", "")
        else:
            try:
                result = request_service("GET", f"{api}/user", headers={"PRIVATE-TOKEN": token})
            except AccountError as exc:
                if str(exc) == "Service returned HTTP 403":
                    return {"valid": False, "message": _GITLAB_NEEDS_READ_USER}
                raise
            login = result.get("username", "")
        login = login if isinstance(login, str) else ""
        checked = {"valid": True, "identity": login, "checked_at": checked_at}
        if login:
            checked["message"] = f"Signed in as {login}"
        return checked
    return {"valid": None, "supported": False, "message": "This account type has no credential check; its saved settings remain available"}


def check_account(account: dict) -> dict:
    """Explicit manager action. Failure details never contain provider payloads."""
    if account.get("kind") == "git":
        if account.get("configured") is False:
            return {"valid": False, "message": f"{_git_needed(account)} before this account can be verified"}
        return _check_git(account)
    if account.get("configured") is False:
        needed = {"api_key": "Enter the API key", "basic": "Enter the username and password",
                  "subscription": "Paste the token from `claude setup-token`", "cli": "Paste the app's login document"}.get(account.get("auth_type", ""), "Enter the credentials")
        return {"valid": False, "message": f"{needed} before this account can be verified"}
    if is_cli_model_login(account):
        from plan_logins import check_plan_login, plan_models
        if account["kind"] != "model":
            return check_plan_login(account)
        # Where the provider's API takes the login's token, the model list IS
        # the login check — one request answers both. Elsewhere the login is
        # read offline and the id is reported as unchecked.
        available = plan_models(account)
        if available.get("supported"):
            note = "the service accepted the login"
        else:
            result = check_plan_login(account)
            if not result.get("valid"):
                return result
            note = result["message"]
        return verify_model_entry(account, available, note)
    elif account["service"] == "tavily":
        request_service("POST", account_catalog.endpoint(account) + "/search", json={"api_key": account["secrets"].get("api_key"), "query": "connection check", "max_results": 1})
    elif account["protocol"] in MODEL_API_PROTOCOLS:
        if account["kind"] != "model":
            discover_models(account)
            return {"valid": True, "message": "The service accepted the credential", "checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            available = discover_models(account)
        except AccountError as exc:
            # No model list to ask (a self-hosted endpoint without /models):
            # the entry is verified by one minimal request to the model itself.
            available = {"models": [], "supported": False, "unlisted": str(exc)}
        return verify_model_entry(account, available, "the service accepted the credential")
    elif account.get("metadata", {}).get("check_path"):
        path = account["metadata"]["check_path"]
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise AccountError("check_path must be a path within the service endpoint")
        request_service("GET", account_catalog.endpoint(account) + path, headers=_headers(account))
    else:
        return {"valid": None, "supported": False, "message": "This account type has no credential check; its saved settings remain available"}
    return {"valid": True, "checked_at": datetime.now(timezone.utc).isoformat()}


def _answers(account: dict, model: str) -> str:
    """One minimal request to ``model`` itself, for an endpoint that lists
    nothing: a one-token completion. Returns a note for the Verify line;
    raises AccountError with the service's status when the model does not
    answer."""
    protocol = account["protocol"]
    base = account_catalog.endpoint(account)
    if not base:
        raise AccountError("Set the service endpoint in Key manager")
    headers = _headers(account)
    if protocol == "openai-responses":
        request_service("POST", base + "/responses", headers=headers, json={"model": model, "input": "ping", "max_output_tokens": 16})
    elif protocol == "anthropic":
        request_service("POST", base + "/messages", headers=headers, json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
    elif protocol == "gemini":
        request_service("POST", f"{base}/models/{model}:generateContent", headers=headers,
                 json={"contents": [{"parts": [{"text": "ping"}]}], "generationConfig": {"maxOutputTokens": 1}})
    else:
        # OpenAI's own reasoning models reject max_tokens on chat/completions;
        # compatible servers (groq, cerebras, vLLM) still take it.
        cap = {"max_completion_tokens": 1} if protocol in {"openai", "openai-chat"} else {"max_tokens": 1}
        request_service("POST", base + "/chat/completions", headers=headers, json={"model": model, "messages": [{"role": "user", "content": "ping"}], **cap})
    return f"model {model!r} answered a one-token request"


def verify_model_entry(account: dict, available: dict, credential_note: str) -> dict:
    """The model-entry half of Verify, after the credential or login passed:
    the id is chosen or filled, the service lists it (or, with nothing to
    list, answers a one-token request for it), the selected harness can run
    this entry, and the entry carries the ``text`` capability the model role
    demands. A blank model is filled with ``default_model_for``, the SDK's
    answer to what a blank model runs (equal to the engine's
    ``resolve_model`` for every selection), and checked like a chosen one.
    The per-harness model-id rules (spelling, own vocabulary, what an
    unchecked id does) are the SDK taxonomy's. Where a plan exposes no list
    the id is honestly reported as unchecked."""
    chosen = account.get("model") or ""
    model_id = chosen or default_model_for(account)
    if model_id is None:
        return {"valid": False, "message": "Enter a model ID: this endpoint has no default model"}
    capabilities = account.get("capabilities") or []
    if "text" not in capabilities:
        return {"valid": False, "message": "This entry's capabilities must include text for a runner to select it as a model"}
    metadata = account.get("metadata") or {}
    harness = str(metadata.get("harness") or ("" if is_cli_model_login(account) else DEFAULT_MODEL_HARNESS))
    supported = account.get("supported_harnesses")
    if not isinstance(supported, list):
        supported = supported_model_harnesses(account)
    if harness not in supported:
        return {"valid": False, "message": account.get("model_configuration_error") or
                f"Harness {harness or '(not selected)'!r} is incompatible with this account's provider, authentication, or endpoint"
                + (f"; it supports {', '.join(supported)}" if supported else "")}
    provider = model_account_provider(account)
    notes = [credential_note]
    if not chosen:
        # "" is the harness's own pick at run time: nothing to check.
        notes.append(f"no model chosen, so {harness} uses {model_id!r}" if model_id else blank_model_note(harness))
    rows = [row for row in available.get("models", []) if isinstance(row, dict)]
    listed = {row["id"] for row in rows}
    vocabulary = HARNESS_MODEL_VOCABULARY.get(harness)
    consequence = UNCHECKED_MODEL_CONSEQUENCE.get(harness, UNCHECKED_MODEL_FALLBACK)
    own_pick = HARNESS_DEFAULT_MODEL_OVERRIDES.get(harness)
    if not model_id:
        pass
    elif (own_pick and model_id == own_pick and vocabulary is None
          and model_id.lower() not in CLAUDE_MODEL_ALIASES):
        # The harness's own sentinel for "let it choose" (antigravity's
        # ``default``; claude's ``sonnet`` is an alias the list check resolves):
        # no list holds it, and the harness takes it.
        notes.append(f"model {model_id!r} lets {harness} choose its own")
    elif vocabulary is not None:
        # The harness's own names; the provider's list proves the key, not the model.
        own = HARNESS_DEFAULT_MODEL_OVERRIDES.get(harness)
        if model_id == own or model_id in vocabulary:
            notes.append(f"model {model_id!r} is one {harness} serves")
        elif available.get("supported") and model_id in listed:
            return {"valid": False, "message": f"{model_id!r} is a {provider} API model ID; {harness} takes its own model names "
                    f"({', '.join(vocabulary[:3])}, …)" + (f" or {own!r} to let it choose" if own else "") + f" — {consequence}"}
        else:
            notes.append(f"model {model_id!r} is not in {harness}'s known model list; {consequence} if it is unknown to it too")
    elif available.get("supported"):
        full = CLAUDE_MODEL_ALIASES.get(model_id.lower(), model_id) if account.get("service") == "anthropic" else model_id
        if model_id not in listed and full not in listed:
            return {"valid": False, "message": f"Model {model_id!r} is not in the service's current model list"
                    + ("" if chosen else f"; it is {harness}'s default for a blank model, so enter a model ID the service lists")}
        row = next((row for row in rows if row["id"] in {model_id, full}), {})
        reported = row.get("capabilities") or []
        if row.get("capabilities_source") == "service" and "text" not in reported:
            return {"valid": False, "message": f"{model_id!r} is not a text model; the service reports {', '.join(reported) or 'no text output'}"}
        notes.append(f"model {model_id!r} is in the service's model list" + (f" (as {full})" if full != model_id else ""))
    elif "unlisted" in available:
        try:
            notes.append(_answers(account, model_id))
        except AccountError as exc:
            raise AccountError(f"Model {model_id!r} did not answer a one-token request: {exc}") from None
    else:
        notes.append(f"the model ID is not checked: {available.get('message') or 'this login exposes no model list'} — {consequence}")
    notes.append(_harness_availability(harness, model_account_auth(account)))
    return {"valid": True, "message": "; ".join(notes), "checked_at": datetime.now(timezone.utc).isoformat()}


def _harness_availability(harness: str, auth: str = "") -> str:
    """Where this entry's harness runs, per runner ``docker`` level: the
    SDK's structured placement answer (``harness_placement.availability``,
    the same one the run start adopts with) in a sentence. A level that runs
    the harness as granted says where; a level it needs more than is one an
    Annotation runner adopts ``docker: full`` from (with ``internet: full``)
    and a chat runner is refused at; a level that cannot place it at all (a
    host harness under ``limited``) is said to be refused. Then where its
    CLI surface runs for the entry's ``auth`` (:func:`_cli_availability`).
    The machine's CLIs are probed fresh."""
    from collimate import host_harness
    from collimate.harness_placement import availability, describe_host, row
    host_harness.forget()
    facts = row(harness)
    levels = availability(harness)
    notes: list[str] = []
    for placed in levels.values():
        notes += [note for note in placed.notes if note not in notes]
    granted: dict[str, list[str]] = {}
    refused: list[str] = []
    for level, placed in levels.items():
        if placed.adopted:
            continue
        if not placed.ok:
            refused.append(level)
            continue
        if placed.runtime == "standard":
            where = f"uses this machine's {describe_host(placed.host or {})}"
        elif placed.runtime == "host":
            where = "runs the host adapter"
        else:
            where = "uses its container"
        granted.setdefault(where, []).append(level)
    parts = ["; ".join(notes)] if notes else []
    if facts["host_twin"] and levels["none"].runtime != "standard":
        parts[-1:] = [(parts[-1] + " — " if parts else "")
                      + f"install {facts['host_binary']} outside Snap to run it under docker: none"]
    parts += [f"docker: {_levels(found)} {where}" for where, found in granted.items()]
    adopting = [level for level, placed in levels.items() if placed.adopted]
    if adopting:
        parts.append(f"an Annotation runner on docker: {_levels(adopting)} adopts docker: full for it "
                     "(with internet: full; a chat runner there is refused)")
    if refused:
        # A host harness under docker: limited: none already places it, so
        # nothing is raised and the run is refused.
        parts.append(f"docker: {_levels(refused)} cannot run it")
    if facts["locus"] == "host":
        # A sandboxed harness (the catalog's commands_sandboxed fact) runs
        # its shell in a Docker sandbox under docker: full and on this
        # machine otherwise; every other host harness's run here.
        parts.append("docker: full sandboxes the commands its grant allows, recommended for commands: allow"
                     if facts.get("commands_sandboxed") else "the commands its grant allows run on this machine")
    if facts["locus"] == "container" and not facts["host_twin"]:
        parts.append("no supported host twin")
    if facts["terminal"]:
        parts.append(_cli_availability(harness, auth))
    return f"{harness} can run it: " + "; ".join(parts)


def _cli_availability(harness: str, auth: str) -> str:
    """Where the entry's CLI surface runs: in its container under ``docker:
    full``, or as this machine's own CLI (``harness_placement``'s terminal
    placement) — with the copy found, or why there is none."""
    from collimate.harness_placement import describe_host, terminal_availability, terminal_locus
    if terminal_locus(harness, auth) == "container":
        return "its CLI runs in its container under docker: full"
    placed = terminal_availability(harness, auth)["none"]
    if placed.ok:
        return (f"its CLI runs on this machine ({describe_host(placed.host or {})}) under docker: none or full "
                "with commands ask or allow, outside any sandbox, signed in once for this entry")
    why = placed.notes[0] if placed.notes else placed.refusal
    return (f"its CLI runs on this machine under docker: none or full with commands ask or allow, "
            f"but {why}")


def _levels(levels: list[str]) -> str:
    return levels[0] if len(levels) == 1 else ", ".join(levels[:-1]) + " or " + levels[-1]


def check_draft(body: dict) -> dict:
    """Verify an unsaved editor draft: exactly what Save would write, checked
    without writing. The instruction for the credential is the catalog's
    ``help`` under the form's Authentication method, so a failure carries
    only its message."""
    import accounts
    try:
        name = body.get("name")
        name = name if isinstance(name, str) else ""
        result = check_account(accounts.draft_account({**body, "name": name.strip() or "Unnamed account"}))
    except AccountError as exc:
        result = {"valid": False, "message": str(exc)}
    return result
