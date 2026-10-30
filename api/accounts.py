"""User/machine account registry and private, per-run account snapshots.

Only nonsecret metadata and opaque keyring slot references reach this file.
The registry is outside channel/build data and is shared by native app profiles.
Network discovery is deliberately absent: explicit manager operations live in
``account_adapters``. A run reads the registry exactly once.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collimate import git_hosts
from collimate.lint import ACCOUNT_KINDS
from collimate.account_taxonomy import (
    DEFAULT_MODEL_HARNESS, MODEL_HARNESS_LABELS, MODEL_API_PROTOCOLS, MODEL_HARNESS_RULES, MODEL_LOGIN_PLANS, PLAN_DEFAULT_HARNESS, PLAN_LOGINS, default_model_for,
    is_cli_model_login, signs_in_on_host, supported_model_harnesses,
)

import keystore

logger = logging.getLogger(__name__)


def _config_path() -> Path:
    override = os.environ.get("COLLIMATE_ACCOUNTS_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "collimate" / "accounts.json"


CONFIG_PATH = _config_path()
_LOCK = threading.RLock()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SENSITIVE_FIELDS = frozenset({
    "api_key", "password", "access_token", "refresh_token", "client_secret",
    "credential_json", "credentials", "secrets", "authorization", "token",
    "id_token", "private_key", "oauth_token", "credential_file",
})
_FIELDS = frozenset({
    "name", "kind", "service", "protocol", "endpoint", "model",
    "capabilities", "auth_type", "metadata", "credential_account_id",
})
_MANAGER_CACHES = frozenset({"model_discovery"})


class AccountError(ValueError):
    """Safe-to-display account error; never includes credential contents."""


class AccountNotFound(AccountError):
    pass


class AccountConflict(AccountError):
    pass


class AccountConfigError(AccountError):
    pass


class KeyringUnavailable(AccountError):
    pass


class LoginUnreadable(AccountError):
    """A lease's login document the server cannot read (_read_login)."""


@contextmanager
def _file_lock(lock_path: Path):
    """Hold an exclusive OS lock on ``lock_path`` across processes."""
    with lock_path.open("a+b") as handle:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked():
    """Coordinate readers and writes in every process sharing this registry."""
    with _LOCK:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _file_lock(CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".lock")):
            yield


def _empty() -> dict:
    return {"version": 1, "accounts": {}, "defaults": {}, "pending_delete": []}


def _no_secrets(value: Any, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _SENSITIVE_FIELDS:
                raise AccountError(f"{path}.{key} must be stored as a secret")
            _no_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            _no_secrets(child, path)


def _custom_fields(metadata: dict) -> dict[str, bool]:
    custom_type = metadata.get("custom_type")
    if not isinstance(custom_type, str) or not custom_type.strip():
        raise AccountError("a custom entry requires a custom type")
    schema = metadata.get("field_schema", [])
    if not isinstance(schema, list):
        raise AccountError("custom field_schema must be a list")
    fields = {}
    for field in schema:
        if (not isinstance(field, dict) or set(field) != {"name", "secret"}
                or not isinstance(field["name"], str)
                or not _SECRET_FIELD.fullmatch(field["name"])
                or not isinstance(field["secret"], bool)):
            raise AccountError("custom fields require a name using letters, digits, or underscores and a boolean secret flag")
        if field["name"] in fields:
            raise AccountError("custom field names must be unique")
        fields[field["name"]] = field["secret"]
    values = metadata.get("fields", {})
    if not isinstance(values, dict):
        raise AccountError("custom fields must be an object")
    for name in values:
        if name not in fields:
            raise AccountError("custom values must have a matching field definition")
        if fields[name]:
            raise AccountError(f"Custom field {name!r} must be stored as a secret")
    return fields


_PRIVATE_KEY_MARK = "PRIVATE KEY-----"
_AUTHOR_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
_SECRET_TEXT = re.compile(r"[\s\x00-\x1f\x7f]")
# What each authentication method of a kind-git entry keeps in the keyring,
# and how a refusal names it.
_GIT_SECRET = {"api_key": ("api_key", "token"), "basic": ("password", "token or password"),
               "ssh_key": ("private_key", "SSH private key")}


def _no_private_keys(value: Any, path: str = "metadata") -> None:
    """Refuse a private key pasted into any metadata string: metadata is public and plain."""
    if isinstance(value, dict):
        for key, child in value.items():
            _no_private_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            _no_private_keys(child, path)
    elif isinstance(value, str) and _PRIVATE_KEY_MARK in value:
        raise AccountError(f"{path} holds a private key; paste it into SSH private key, which the keyring keeps")


def _git_metadata(metadata: dict) -> None:
    """The shapes of a kind-git entry's plain metadata. Presence of the commit
    author is the save path's rule (and ``_configured``'s), never a read's, so
    a hand-edited registry missing it stays readable."""
    name = metadata.get("author_name")
    if name not in (None, "") and (not isinstance(name, str) or len(name) > 200
                                   or any(c in name for c in "\n\r\0<>")):
        raise AccountError("Commit author name must be one line without < or >")
    email = metadata.get("author_email")
    if email not in (None, "") and (not isinstance(email, str) or len(email) > 254
                                    or not _AUTHOR_EMAIL.fullmatch(email)):
        raise AccountError("Commit author email must look like name@host")
    username = metadata.get("username")
    if username not in (None, "") and (not isinstance(username, str) or len(username) > 200
                                       or any(c in username for c in ":\n\r")):
        raise AccountError("The username cannot contain ':' or a line break")
    known_hosts = metadata.get("known_hosts")
    if known_hosts is not None:
        if not isinstance(known_hosts, str):
            raise AccountError("Known hosts must be text")
        try:
            git_hosts.parse_known_hosts(known_hosts)
        except ValueError as exc:
            raise AccountError(str(exc)) from None


def _has_author(record: dict) -> bool:
    metadata = record["metadata"]
    return bool(str(metadata.get("author_name") or "").strip() and str(metadata.get("author_email") or "").strip())


def _git_secrets(record: dict, secrets: dict, *, retire: bool) -> dict:
    """A kind-git entry's secrets as they reach the keyring: only the field its
    authentication uses, a token without whitespace, a private key normalised
    and readable. ``retire`` (the save path) also clears every stored slot the
    authentication no longer uses."""
    from account_catalog import auth_label
    auth = record["auth_type"]
    field, what = _GIT_SECRET.get(auth, (None, ""))
    result = dict(secrets)
    for name, value in secrets.items():
        if value is None or name == field:
            continue
        if field is None:
            raise AccountError("A public Git account stores no secret")
        raise AccountError(f"A Git account signing in with {auth_label(record['service'], auth)} stores only its {what}")
    if retire:
        for name in set(record["secret_refs"]) - {field}:
            result[name] = None
    value = result.get(field) if field else None
    if isinstance(value, str) and field in ("api_key", "password"):
        value = value.strip()
        # An empty value is left for the shared "nonempty" refusal.
        if value and (len(value) > 4096 or _SECRET_TEXT.search(value)):
            raise AccountError("The token cannot contain spaces or line breaks")
        result[field] = value
    elif isinstance(value, str) and value and field == "private_key":
        value = git_hosts.normalise_private_key(value)
        try:
            git_hosts.inspect_private_key(value)
        except ValueError as exc:
            raise AccountError(str(exc)) from None
        result[field] = value
    return result


def _validate_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise AccountError("an account must be an object")
    if set(record) - _FIELDS - {"id", "revision", "generation", "secret_refs"}:
        raise AccountError("account config contains unknown fields")
    if not _IDENTIFIER.fullmatch(str(record.get("id", ""))):
        raise AccountError("invalid account id")
    if not isinstance(record.get("name"), str) or not record["name"].strip():
        raise AccountError("account name is required")
    if record.get("kind") not in ACCOUNT_KINDS:
        raise AccountError(f"account kind must be one of {', '.join(sorted(ACCOUNT_KINDS))}")
    for field in ("service", "protocol", "endpoint", "model", "auth_type"):
        if not isinstance(record.get(field), str):
            raise AccountError(f"{field} must be a string")
    if not record["service"] or not record["auth_type"]:
        raise AccountError("service and authentication type are required")
    from account_catalog import service_catalog
    service = next((row for row in service_catalog() if row["id"] == record["service"]), None)
    if service is None:
        raise AccountError(f"Unknown account service: {record['service']!r}")
    if record["kind"] not in service["kinds"]:
        raise AccountError(f"Account kind {record['kind']!r} does not fit service {record['service']!r} "
                           f"(expected {' or '.join(service['kinds'])})")
    if record["auth_type"] not in service["auth_types"]:
        raise AccountError(
            f"Unsupported authentication type {record['auth_type']!r} for service {record['service']!r}"
        )
    if record["kind"] == "git":
        problem = git_hosts.endpoint_problem(record["service"], record["auth_type"], record["endpoint"])
        if problem:
            raise AccountError(problem)
    elif record["endpoint"]:
        endpoint = urlsplit(record["endpoint"])
        if endpoint.scheme not in {"https", "http"} or not endpoint.netloc:
            raise AccountError("endpoint must be an http(s) URL")
        if endpoint.username or endpoint.password:
            raise AccountError("endpoint credentials must be stored as secrets")
    caps = record.get("capabilities")
    if not isinstance(caps, list) or any(not isinstance(c, str) or not c for c in caps):
        raise AccountError("capabilities must be a list of names")
    if not isinstance(record.get("metadata"), dict):
        raise AccountError("metadata must be an object")
    _no_private_keys(record["metadata"])
    if record["kind"] == "custom":
        _custom_fields(record["metadata"])
    if record["kind"] == "git":
        _git_metadata(record["metadata"])
    _no_secrets(record["metadata"])
    source = record.get("credential_account_id")
    if source is not None and (not isinstance(source, str) or not _IDENTIFIER.fullmatch(source)):
        raise AccountError("invalid credential account reference")
    refs = record.get("secret_refs", {})
    if not isinstance(refs, dict) or any(
        not _SECRET_FIELD.fullmatch(k) or not isinstance(v, str)
        or not v.startswith(f"collimate_{record['id']}_")
        for k, v in refs.items()
    ):
        raise AccountError("invalid keyring slot references")
    if not isinstance(record.get("revision"), str) or not record["revision"]:
        raise AccountError("account revision is required")
    if not isinstance(record.get("generation"), str) or not _IDENTIFIER.fullmatch(record["generation"]):
        raise AccountError("account generation is required")
    try:
        json.dumps(record, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AccountError("account metadata must contain finite JSON values") from exc
    return record


def _read() -> dict:
    if not CONFIG_PATH.exists():
        return _empty()
    try:
        doc = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("version") != 1:
            raise AccountError("expected accounts config version 1")
        if not isinstance(doc.get("accounts"), dict):
            raise AccountError("accounts must be an object")
        if not isinstance(doc.get("defaults", {}), dict):
            raise AccountError("defaults must be an object")
        if not isinstance(doc.get("pending_delete", []), list) or any(
            not isinstance(slot, str) or not slot.startswith("collimate_")
            for slot in doc.get("pending_delete", [])
        ):
            raise AccountError("pending_delete must contain keyring references")
        doc.setdefault("defaults", {})
        doc.setdefault("pending_delete", [])
        dropped = _drop_retired_services(doc)
        for identity, record in doc["accounts"].items():
            _validate_record(record)
            if identity != record["id"]:
                raise AccountError("account index does not match its id")
    except json.JSONDecodeError as exc:
        raise AccountConfigError(
            f"Cannot parse {CONFIG_PATH}: {exc.msg} at line {exc.lineno}, column {exc.colno}. "
            "The original file has been preserved."
        ) from exc
    except (OSError, UnicodeError, AccountError, TypeError) as exc:
        raise AccountConfigError(
            f"Cannot read {CONFIG_PATH}: {exc}. The original file has been preserved."
        ) from exc
    if dropped:
        try:
            _write(doc)
        except OSError:
            # The dropped records stay on disk; the next read drops them again.
            return doc
        _cleanup(doc)
    return doc


# Services the catalog no longer offers. A saved record of one is dropped
# when the registry is read, its keyring slots queued for deletion, so it
# never makes the rest of the registry unreadable.
_RETIRED_SERVICES = frozenset({"google_drive"})


def _drop_retired_services(doc: dict) -> bool:
    """Remove records of retired services (and the Drive selection) from
    ``doc``; True when anything was removed."""
    dropped = False
    for identity, record in list(doc["accounts"].items()):
        if isinstance(record, dict) and record.get("service") in _RETIRED_SERVICES:
            del doc["accounts"][identity]
            refs = record.get("secret_refs")
            slots = [slot for slot in refs.values() if isinstance(slot, str) and slot.startswith("collimate_")] \
                if isinstance(refs, dict) else []
            doc["pending_delete"] = sorted(set(doc["pending_delete"] + slots))
            dropped = True
    if "drive" in doc["defaults"]:
        del doc["defaults"]["drive"]
        dropped = True
    return dropped


def _write(doc: dict) -> None:
    """Replace only after a complete fsynced write; never truncate the registry."""
    data = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".accounts-", suffix=".json", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG_PATH)
        try:
            directory = os.open(CONFIG_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Replacement committed; directory durability failures cannot
            # trigger rollback of credentials now referenced by the file.
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _owner(doc: dict, record: dict) -> dict:
    source = record.get("credential_account_id")
    if not source:
        return record
    owner = doc["accounts"].get(source)
    if not owner or owner.get("credential_account_id"):
        raise AccountNotFound(f"Credential account {source!r} is missing or is not a base account")
    if owner["service"] != record["service"] or owner["auth_type"] != record["auth_type"]:
        raise AccountError("subitems must match their credential account's service and authentication")
    return owner


_AUTH_METADATA_FIELDS = frozenset({
    "harness", "plan", "username", "auth_state",
})
# Token bookkeeping kept in metadata.auth_state, not the keyring.
_AUTH_STATE_FIELDS = ("expires_at", "token_type")


def _private_metadata(record: dict, owner: dict) -> dict:
    metadata = copy.deepcopy(record["metadata"])
    if record["id"] != owner["id"]:
        for field in _AUTH_METADATA_FIELDS:
            if field == "harness" and not is_cli_model_login(owner):
                # API credentials can power several harness/model subitems.
                if field not in metadata and field in owner["metadata"]:
                    metadata[field] = copy.deepcopy(owner["metadata"][field])
                continue
            metadata.pop(field, None)
            if field in owner["metadata"]:
                metadata[field] = copy.deepcopy(owner["metadata"][field])
    if record["kind"] == "model" and not is_cli_model_login(owner) and not metadata.get("harness"):
        metadata["harness"] = DEFAULT_MODEL_HARNESS
    return metadata


def _configured(record: dict, refs: dict) -> bool:
    """Whether ``record``'s authentication is complete. A plan whose CLI
    surface runs the vendor's CLI on this machine (``signs_in_on_host``:
    Claude Code, Antigravity on the Google plan) is complete without its
    token or pasted document: that CLI signs in itself, and only the
    surfaces that read the credential refuse without it."""
    if record["kind"] == "git":
        if not _has_author(record):
            return False
        auth = record["auth_type"]
        if auth == "api_key":
            return "api_key" in refs
        if auth == "basic":
            return "password" in refs and bool(record["metadata"].get("username")) and bool(record["endpoint"])
        if auth == "ssh_key":
            from account_catalog import endpoint
            return "private_key" in refs and (bool(str(record["metadata"].get("known_hosts") or "").strip())
                                              or git_hosts.ships_host_keys(record["service"], endpoint(record)))
        return True
    if record["kind"] == "custom":
        return all(name in refs for name, secret in _custom_fields(record["metadata"]).items() if secret)
    auth = record["auth_type"]
    if auth == "none":
        return True
    if auth == "api_key":
        return "api_key" in refs
    if auth == "basic":
        return "password" in refs and bool(record["metadata"].get("username"))
    if signs_in_on_host(record):
        return True
    if auth == "subscription":
        return "access_token" in refs
    # cli: the pasted login document, saved as the selected app's own field
    login = PLAN_LOGINS.get(record["metadata"].get("harness"))
    return bool(login and login.field in refs)


def _public(record: dict, doc: dict) -> dict:
    result = {k: copy.deepcopy(v) for k, v in record.items() if k != "secret_refs"}
    try:
        owner = _owner(doc, record)
        if owner["id"] != record["id"]:
            for field in ("endpoint", "protocol"):
                result[field] = owner[field]
        result["metadata"] = _private_metadata(record, owner)
        result["secret_fields"] = sorted(owner.get("secret_refs", {}))
        result["configured"] = _configured(result, owner.get("secret_refs", {}))
    except AccountError:
        result["secret_fields"] = []
        result["configured"] = False
    if record["kind"] == "model" or (record["kind"] == "account" and result["protocol"] in {*MODEL_API_PROTOCOLS, "cli"}):
        result["supported_harnesses"] = supported_model_harnesses(result)
        # What a blank model on this entry (or on a model entry under this
        # account) runs; None where the endpoint has no default, so the form
        # asks for a model instead of re-deriving the rule.
        result["default_model"] = default_model_for(result)
        harness = result["metadata"].get("harness")
        if record["kind"] == "model" and harness not in result["supported_harnesses"]:
            result["model_configuration_error"] = (
                f"Harness {harness or '(not selected)'!r} is incompatible with this account's provider, "
                "authentication, or endpoint. Edit the model in Key manager."
            )
        elif record["kind"] == "model" and not record["model"] and result["default_model"] is None:
            # A blank model is the harness's default wherever one exists; a
            # custom endpoint has none.
            result["model_configuration_error"] = "Choose a model: this endpoint has no default model. Edit the model in Key manager."
    return result


def _attach_private(result: dict, record: dict, owner: dict) -> set[str]:
    """Turn a _public view into the private snapshot: the owner's credential
    identity and secrets. Returns the fields whose keyring slot could not be read."""
    result["credential_account_id"] = owner["id"]
    result["credential_revision"] = owner["revision"]
    result["credential_generation"] = owner["generation"]
    result["metadata"] = _private_metadata(record, owner)
    secrets = copy.deepcopy(owner["metadata"].get("auth_state", {})) if record["kind"] != "custom" else {}
    unreadable = set()
    for field, slot in owner["secret_refs"].items():
        value = keystore.get_key(slot)
        if value is None:
            unreadable.add(field)
        else:
            secrets[field] = value
    result["secrets"] = secrets
    return unreadable


def list_accounts() -> dict:
    from account_catalog import service_catalog
    status = {"available": keystore.available(), "config_path": str(CONFIG_PATH)}
    try:
        with _locked():
            doc = _read()
            rows = [_public(record, doc) for record in doc["accounts"].values()]
    except (AccountConfigError, OSError) as exc:
        rows = []
        status["error"] = str(exc)
    from plan_logins import plan_login_catalog
    return {
        "accounts": sorted(rows, key=lambda record: (record["name"].casefold(), record["id"])),
        "services": service_catalog(),
        "plan_logins": plan_login_catalog(),
        **harness_catalog(),
        "status": status,
    }


def harness_catalog() -> dict:
    """The harness facts the Key manager's forms offer, from the SDK's
    taxonomy: every harness a model entry can select, and per service with a
    subscription plan, that plan and the harness a new login starts on."""
    return {
        "model_harnesses": list(MODEL_HARNESS_RULES),
        "default_model_harness": DEFAULT_MODEL_HARNESS,
        "harness_labels": MODEL_HARNESS_LABELS,
        "login_plans": {service: {"plan": plan, "harness": PLAN_DEFAULT_HARNESS[plan]}
                        for service, plan in MODEL_LOGIN_PLANS.items()},
    }


def get_account(identity: str) -> dict:
    with _locked():
        doc = _read()
        record = doc["accounts"].get(identity)
        if record is None:
            raise AccountNotFound(f"Account {identity!r} does not exist")
        return _public(record, doc)


def _retire_slots(pending: list) -> list:
    """Delete journaled keyring slots; return the ones that must stay journaled."""
    remaining = []
    for slot in pending:
        if not keystore.available():
            remaining.append(slot)
        elif not keystore.delete_key(slot) and keystore.get_key(slot) is not None:
            remaining.append(slot)
    return remaining


def _cleanup(doc: dict) -> None:
    """Best-effort retirement. Failed deletes stay journaled for later saves."""
    pending = doc.get("pending_delete", [])
    remaining = _retire_slots(pending)
    if remaining != pending:
        doc["pending_delete"] = remaining
        try:
            _write(doc)
        except OSError:
            # The previous committed journal can safely retry these deletes.
            pass


def _service_capabilities(service: str) -> list:
    """What a service's accounts can do. Capabilities are a service fact for
    every kind but ``model``, whose entry names the model's own capabilities."""
    from account_catalog import service_catalog
    entry = next((row for row in service_catalog() if row["id"] == service), {})
    return copy.deepcopy(entry.get("capabilities", []))


def _compose_record(doc: dict, identity: str, body: dict, old: dict | None, *, create: bool) -> dict:
    """Merge a request body onto a record: service defaults, credential
    inheritance and the capability rule. Secrets and persistence stay outside."""
    from account_catalog import service_catalog
    unknown = set(body) - _FIELDS - {"id", "revision", "secrets"}
    if unknown:
        raise AccountError(f"Unknown account fields: {', '.join(sorted(unknown))}")
    if body.get("id", identity) != identity:
        raise AccountError("account id cannot change")
    record = copy.deepcopy(old) if old else {
        "id": identity, "name": "", "kind": "account", "service": "",
        "protocol": "", "endpoint": "", "model": "", "capabilities": [],
        "auth_type": "api_key", "metadata": {}, "credential_account_id": None,
        "secret_refs": {},
    }
    for field in _FIELDS & body.keys():
        record[field] = copy.deepcopy(body[field])
    if isinstance(record.get("model"), str):
        # One blank rule for every reader: a model of only spaces is no model,
        # and the id a run is handed carries no stray whitespace.
        record["model"] = record["model"].strip()
    source = record.get("credential_account_id")
    parent = doc["accounts"].get(source) if isinstance(source, str) else None
    if create:
        if parent:
            for field in ("service", "auth_type", "protocol", "endpoint"):
                if field not in body:
                    record[field] = copy.deepcopy(parent[field])
        if record["kind"] == "custom":
            if "service" not in body:
                record["service"] = "custom"
            if "auth_type" not in body:
                record["auth_type"] = "none"
        else:
            defaults = next((service for service in service_catalog() if service["id"] == record["service"]), {})
            if "auth_type" not in body and not parent and defaults.get("auth_types"):
                record["auth_type"] = defaults["auth_types"][0]
            for field in ("protocol", "endpoint"):
                if field not in body and not parent and field in defaults:
                    record[field] = copy.deepcopy(defaults[field])
    if record["kind"] == "git":
        row = next((service for service in service_catalog() if service["id"] == record["service"]), None)
        if row and "git" in row["kinds"]:
            if "protocol" in body and body["protocol"] != row["protocol"]:
                raise AccountError("A git account's protocol is fixed by its service")
            record["protocol"] = row["protocol"]
            if isinstance(record["endpoint"], str):
                address = record["endpoint"].strip() or row["endpoint"]
                # A Git server entry with an SSH key or no credentials has no server
                # URL at all: any one is left for _validate_record to refuse with E-E.
                if row["id"] == "git" and record["auth_type"] in ("ssh_key", "none"):
                    record["endpoint"] = address
                else:
                    try:
                        record["endpoint"] = git_hosts.git_base(address) if address else ""
                    except ValueError as exc:
                        raise AccountError(str(exc)) from None
    if record["kind"] != "model":
        if "capabilities" in body:
            raise AccountError("capabilities belong to model entries; other accounts carry their service's capabilities")
        record["capabilities"] = _service_capabilities(record["service"])
    elif create and "capabilities" not in body:
        record["capabilities"] = (copy.deepcopy(parent["capabilities"]) if parent
                                  else _service_capabilities(record["service"]))
    return record


def _save_locked(doc: dict, identity: str, body: dict, *, create: bool = False, refresh: bool = False) -> dict:
    old = doc["accounts"].get(identity)
    if create and old:
        raise AccountConflict(f"Account {identity!r} already exists")
    if not create and not old:
        raise AccountNotFound(f"Account {identity!r} does not exist")
    expected = body.get("revision")
    if expected is not None and old and expected != old["revision"]:
        raise AccountConflict("This account was changed elsewhere; reopen it before saving")
    record = _compose_record(doc, identity, body, old, create=create)
    record["revision"] = uuid.uuid4().hex
    if not refresh or not old:
        record["generation"] = uuid.uuid4().hex
    _validate_record(record)
    secrets = body.get("secrets", {})
    if not isinstance(secrets, dict):
        raise AccountError("secrets must be an object")
    secrets = dict(secrets)
    if record["kind"] == "git":
        if not _has_author(record):
            raise AccountError("Enter the commit author name and email: pushes are committed as them")
        secrets = _git_secrets(record, secrets, retire=True)
    if record["kind"] == "custom":
        schema = _custom_fields(record["metadata"])
        allowed = {name for name, secret in schema.items() if secret}
        if any(value is not None and name not in allowed for name, value in secrets.items()):
            raise AccountError("custom secrets must have a field definition marked secret")
        # Removing/renaming a field or making it public must retire the old
        # credential too. Never copy a saved secret into public metadata.
        for name in set(record["secret_refs"]) - allowed:
            secrets[name] = None
    # Native adapter expiration and token type are authentication bookkeeping.
    # A custom entry owns its field meanings, including these same names.
    # Adapters can submit a refresh atomically; only token material reaches
    # keyring, while this state stays in the nonsecret account document.
    if record["kind"] != "custom":
        for field in _AUTH_STATE_FIELDS:
            if field in secrets:
                value = secrets.pop(field)
                if value is not None and not isinstance(value, str):
                    raise AccountError(f"{field} must be a string")
                state = dict(record["metadata"].get("auth_state", {}))
                if value is None:
                    state.pop(field, None)
                else:
                    state[field] = value
                record["metadata"]["auth_state"] = state
    if record.get("credential_account_id"):
        if record["kind"] != "model":
            raise AccountError("only model entries can draw on a credential account")
        if secrets or record["secret_refs"]:
            raise AccountError("a subitem with a credential account cannot store separate secrets")
        if record["credential_account_id"] == identity:
            raise AccountError("an account cannot refer to itself")
        owner = _owner(doc, record)
        if is_cli_model_login(owner):
            chosen_harness = record["metadata"].get("harness")
            if chosen_harness and chosen_harness != owner["metadata"].get("harness"):
                if "metadata" in body:
                    raise AccountError("CLI model choices must use their account's signed-in harness")
                # The parent may have changed since this child was saved.
                # Unrelated edits keep following that signed-in account.
                record["metadata"].pop("harness", None)
    for field, value in secrets.items():
        if not isinstance(field, str) or not _SECRET_FIELD.fullmatch(field):
            raise AccountError("secret field names must contain letters, digits, or underscores")
        if value is not None and (not isinstance(value, str) or not value):
            raise AccountError("secret values must be nonempty strings; use null to remove one")
    connection_changed = bool(old and not refresh and (
        secrets or any(record.get(field) != old.get(field) for field in (
            "service", "protocol", "endpoint", "auth_type", "credential_account_id",
        )) or {key: value for key, value in record["metadata"].items() if key not in _MANAGER_CACHES}
        != {key: value for key, value in old["metadata"].items() if key not in _MANAGER_CACHES}
    ))
    if connection_changed:
        for key in _MANAGER_CACHES:
            record["metadata"].pop(key, None)
    if secrets and not keystore.available():
        raise KeyringUnavailable("OS keyring is unavailable; credentials were not saved. Unlock the keyring, then restart the app")
    created = []
    retired = []
    try:
        for field, value in secrets.items():
            previous = record["secret_refs"].pop(field, None)
            if previous:
                retired.append(previous)
            if value is not None:
                slot = f"collimate_{identity}_{record['revision']}_{field}"
                if not keystore.set_key(slot, value):
                    raise KeyringUnavailable("OS keyring could not save credentials; the account was not changed")
                created.append(slot)
                record["secret_refs"][field] = slot
        if connection_changed:
            for child in doc["accounts"].values():
                if child.get("credential_account_id") != identity:
                    continue
                for key in _MANAGER_CACHES:
                    child["metadata"].pop(key, None)
                # The effective connection changed, so a stale child editor
                # or in-flight check must not restore its previous settings.
                child["revision"] = uuid.uuid4().hex
                child["generation"] = uuid.uuid4().hex
        doc["accounts"][identity] = record
        doc["pending_delete"] = sorted(set(doc["pending_delete"] + retired))
        _write(doc)
    except Exception:
        for slot in created:
            keystore.delete_key(slot)
        raise
    _cleanup(doc)
    return _public(record, doc)


def create_account(body: dict) -> dict:
    identity = body.get("id") or uuid.uuid4().hex
    if not isinstance(identity, str) or not _IDENTIFIER.fullmatch(identity):
        raise AccountError("invalid account id")
    with _locked():
        return _save_locked(_read(), identity, body, create=True)


def update_account(identity: str, body: dict) -> dict:
    with _locked():
        return _save_locked(_read(), identity, body)


def draft_account(body: dict) -> dict:
    """The private account an open editor would save, without saving it.

    Verify runs against exactly what Save would write: the same defaults,
    credential inheritance and kind rules, with the body's secrets overlaid on
    the ones already in the keyring. Neither the registry nor the keyring is
    written, and ``revision`` in the body is ignored.
    """
    identity = body.get("id") or uuid.uuid4().hex
    if not isinstance(identity, str) or not _IDENTIFIER.fullmatch(identity):
        raise AccountError("invalid account id")
    secrets = body.get("secrets", {})
    if not isinstance(secrets, dict):
        raise AccountError("secrets must be an object")
    with _locked():
        doc = _read()
        old = doc["accounts"].get(identity)
        record = _compose_record(doc, identity, {k: v for k, v in body.items() if k != "revision"},
                                 old, create=old is None)
        record["revision"] = uuid.uuid4().hex
        record["generation"] = old["generation"] if old else uuid.uuid4().hex
        _validate_record(record)
        if record["kind"] == "custom":
            allowed = {name for name, secret in _custom_fields(record["metadata"]).items() if secret}
            if any(value is not None and name not in allowed for name, value in secrets.items()):
                raise AccountError("custom secrets must have a field definition marked secret")
        if record["kind"] == "git":
            secrets = _git_secrets(record, secrets, retire=False)
        # This registry copy is never written; indexing the draft lets the
        # public/private views resolve it exactly as a saved account.
        doc["accounts"][identity] = record
        owner = _owner(doc, record)
        result = _public(record, doc)
        unreadable = _attach_private(result, record, owner)
        values = result["secrets"]
        for field, value in secrets.items():
            if not isinstance(field, str) or not _SECRET_FIELD.fullmatch(field):
                raise AccountError("secret field names must contain letters, digits, or underscores")
            if value is None:
                values.pop(field, None)
            elif isinstance(value, str) and value:
                values[field] = value
            else:
                raise AccountError("secret values must be nonempty strings; use null to remove one")
        # A slot the record declares but the keyring cannot return is a keyring
        # failure, not an unconfigured account — say so instead of letting the
        # draft look like one that was never given a credential. The editor can
        # still verify a retyped secret, so only the ones it left alone count.
        if unreadable - set(secrets):
            raise KeyringUnavailable("OS keyring could not read this account's credentials")
        result["secrets"] = values
        result["configured"] = _configured(result, values)
        return result


def delete_account(identity: str, *, revision: str | None = None) -> None:
    with _locked():
        doc = _read()
        record = doc["accounts"].get(identity)
        if not record:
            raise AccountNotFound(f"Account {identity!r} does not exist")
        if revision is not None and record["revision"] != revision:
            raise AccountConflict("This account changed before deletion")
        del doc["accounts"][identity]
        doc["pending_delete"] = sorted(set(doc["pending_delete"] + list(record["secret_refs"].values())))
        _write(doc)
        _cleanup(doc)


def _validate_requirement(role: str, record: dict, requirement: dict) -> None:
    if not isinstance(requirement, dict):
        raise AccountError(f"Account requirement {role!r} must be an object")
    for field in ("kind", "service", "protocol", "auth_type", "custom_type", "harness"):
        accepted = requirement.get("harnesses" if field == "harness" else field + "s", requirement.get(field))
        if accepted is None:
            continue
        if isinstance(accepted, str):
            accepted = [accepted]
        value = record["metadata"].get(field) if field in {"custom_type", "harness"} else record[field]
        if not isinstance(accepted, list) or value not in accepted:
            raise AccountError(f"Account {record['name']!r} has an incompatible {field} for {role!r}")
    required = requirement.get("capabilities", [])
    if not isinstance(required, list) or any(not isinstance(cap, str) for cap in required):
        raise AccountError(f"Capabilities for {role!r} must be a list")
    missing = set(required) - set(record["capabilities"])
    if missing:
        raise AccountError(f"Account {record['name']!r} lacks capabilities for {role!r}: {', '.join(sorted(missing))}")
    if record["kind"] == "model" and record.get("model_configuration_error"):
        raise AccountError(f"Account {record['name']!r}: {record['model_configuration_error']}")


def _validate_bindings(bindings: dict[str, str], requirements: dict[str, dict] | None) -> dict[str, dict]:
    if not isinstance(bindings, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in bindings.items()):
        raise AccountError("account selections must map role names to account ids")
    requirements = {} if requirements is None else requirements
    if not isinstance(requirements, dict):
        raise AccountError("account requirements must be an object")
    for role, requirement in requirements.items():
        if not isinstance(requirement, dict):
            raise AccountError(f"Account requirement {role!r} must be an object")
        if requirement.get("required", True) and not bindings.get(role):
            raise AccountError(f"Select an account for {role!r}")
    if requirements and set(bindings) - set(requirements):
        raise AccountError("runner selects undeclared account roles")
    return requirements


def resolve_public_accounts(bindings: dict[str, str], requirements: dict[str, dict] | None = None) -> dict[str, dict]:
    """Resolve saved metadata for editing/preflight without reading credentials.

    Authentication completeness uses saved secret references only. Execution
    must still resolve its private snapshot to read the actual keyring values.
    """
    requirements = _validate_bindings(bindings, requirements)
    if not bindings:
        return {}
    result = {}
    with _locked():
        doc = _read()
        for role, identity in bindings.items():
            if not identity and not requirements.get(role, {}).get("required", True):
                continue
            record = doc["accounts"].get(identity)
            if not record:
                raise AccountNotFound(f"Selected account {identity!r} for {role!r} does not exist")
            _owner(doc, record)
            public = _public(record, doc)
            _validate_requirement(role, public, requirements.get(role, {}))
            if not public["configured"]:
                raise AccountError(f"Complete authentication for account {record['name']!r} in Key manager")
            result[role] = public
    return result


def resolve_accounts(bindings: dict[str, str], requirements: dict[str, dict] | None = None) -> dict[str, dict]:
    """Return detached private snapshots. No checks, discovery, or network I/O.

    Call once at run creation and retain this context privately. Changes to the
    registry cannot mutate it. Only public_accounts_snapshot may be persisted.
    """
    requirements = _validate_bindings(bindings, requirements)
    if not bindings:
        return {}
    result = {}
    with _locked():
        doc = _read()
        for role, identity in bindings.items():
            if not identity and not requirements.get(role, {}).get("required", True):
                continue
            record = doc["accounts"].get(identity)
            if not record:
                raise AccountNotFound(f"Selected account {identity!r} for {role!r} does not exist")
            owner = _owner(doc, record)
            private = _public(record, doc)
            _validate_requirement(role, private, requirements.get(role, {}))
            if owner["secret_refs"] and not keystore.available():
                raise KeyringUnavailable("OS keyring is unavailable; selected credentials cannot be read")
            if _attach_private(private, record, owner):
                raise KeyringUnavailable(f"Credentials for account {record['name']!r} are unavailable in the OS keyring")
            if not _configured(private, private["secrets"]):
                raise AccountError(f"Complete authentication for account {record['name']!r} in Key manager")
            result[role] = private
    return result


def private_account(identity: str) -> dict:
    """Manager/adapters only (Verify, model discovery): an account's private snapshot, even when its authentication is incomplete."""
    with _locked():
        doc = _read()
        record = doc["accounts"].get(identity)
        if not record:
            raise AccountNotFound(f"Account {identity!r} does not exist")
        owner = _owner(doc, record)
        result = _public(record, doc)
        if _attach_private(result, record, owner):
            raise KeyringUnavailable("OS keyring could not read this account's credentials")
        return result


def public_accounts_snapshot(context: dict[str, dict]) -> dict[str, dict]:
    """The sole serialization boundary for persisted run provenance."""
    return {
        role: {key: copy.deepcopy(value) for key, value in record.items()
               if key not in {"secrets", "secret_refs", "secret_fields"}}
        for role, record in context.items()
    }


def account_environment(context: dict[str, dict]) -> dict[str, str]:
    """Selected-account environment. Ambiguous same-provider envs are omitted.

    Structured SDK account access remains authoritative for multiple roles.
    Only explicitly selected accounts contribute values; host env is not read.
    """
    from account_adapters import secret_environment
    values: dict[str, str] = {}
    ambiguous: set[str] = set()
    for account in context.values():
        for name, value in secret_environment(account).items():
            if name in values and values[name] != value:
                ambiguous.add(name)
            else:
                values[name] = value
    return {name: value for name, value in values.items() if name not in ambiguous}


def commit_refreshed_secrets(identity: str, expected_revision: str, secrets: dict) -> str | None:
    """A run's rotated credentials (``gen.commit_account``), compare-and-swap:
    never overwrite a manager edit or recreate a deleted account.

    A run replaces only credentials the account already stores, plus a native
    account's token bookkeeping (``expires_at``, ``token_type``), each with a
    nonempty string. It cannot add a field or remove one (a null value), so
    one run cannot erase or plant a credential every other run of the account
    relies on. A null or malformed value refuses the whole commit, whatever
    its revision; against the current revision, so does a field the account
    does not store. A refused commit raises AccountError before anything is
    written.

    Return the new revision, or None if another update won. The active runtime
    context keeps its refreshed values even when global write-back is skipped.
    """
    if not isinstance(secrets, dict):
        raise AccountError("secrets must be an object")
    removed = sorted(str(field) for field, value in secrets.items() if value is None)
    if removed:
        raise AccountError(f"A run cannot remove saved credentials: {', '.join(removed)}")
    malformed = sorted(str(field) for field, value in secrets.items() if not isinstance(value, str) or not value)
    if malformed:
        raise AccountError(f"A run commits each credential as a nonempty string, not: {', '.join(malformed)}")
    with _locked():
        doc = _read()
        record = doc["accounts"].get(identity)
        if not record or record["revision"] != expected_revision:
            return None
        stored = set(record["secret_refs"])
        if record["kind"] != "custom":
            stored.update(_AUTH_STATE_FIELDS)
        added = sorted(str(field) for field in secrets if field not in stored)
        if added:
            raise AccountError(f"A run can replace only credentials its account already stores, not: {', '.join(added)}")
        saved = _save_locked(doc, identity, {"revision": expected_revision, "secrets": secrets}, refresh=True)
        return saved["revision"]


def cache_manager_result(identity: str, expected_revision: str, key: str, result: dict, *, expected_generation: str | None = None) -> dict:
    """Explicit check/discovery caches cannot overwrite edits made in flight."""
    with _locked():
        doc = _read()
        record = doc["accounts"].get(identity)
        if not record or (record["revision"] != expected_revision and (not expected_generation or record["generation"] != expected_generation)):
            raise AccountConflict("Account changed while the operation was running; retry it")
        metadata = _private_metadata(record, _owner(doc, record))
        metadata[key] = result
        return _save_locked(doc, identity, {"revision": record["revision"], "metadata": metadata}, refresh=True)


# Shared, temporary CLI stores avoid replaying one rotating refresh token in
# concurrent runs. Generation changes isolate key-manager edits; operational
# refreshes deliberately retain their generation. Lease metadata contains no
# secrets and the last release collects the login and removes the store; a
# store whose server exited without releasing it, or whose last release could
# not collect its login, is judged by the sweep (sweep_expired_account_leases)
# once the server starts again, or sooner when a run holding a rotating
# credential starts: collected and removed, or kept, and named in a warning,
# while its login cannot be read or saved. A login kept because it cannot be
# read is never leased to another run (materialize_cli_account). Which
# harnesses lease a login document, the secret it is saved as and its path in
# the lease are the SDK's PLAN_LOGINS rows (collimate.account_taxonomy), the
# same rows the generator's login store reads.
_OWNED_CLI_LEASES: dict[str, tuple[Path, str]] = {}


def _cli_root() -> Path:
    """The runtime root of this user's registry. Servers on other registries
    (a second profile, a test server) share HOME and temp but not this
    registry's accounts or its lock, so each registry keeps its own root: a
    sweep only ever judges stores leased from the registry it has locked."""
    scope = f"{Path.home()}\0{os.path.realpath(CONFIG_PATH)}"
    digest = hashlib.sha256(scope.encode()).hexdigest()[:16]
    root = Path(tempfile.gettempdir()) / f"collimate-account-runtime-{digest}"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or (hasattr(os, "getuid") and root.stat().st_uid != os.getuid()):
        raise AccountError("The temporary account directory is not owned by this user")
    root.chmod(0o700)
    return root


# What decoding a runtime file can raise besides OSError: bytes that are not
# UTF-8 or not JSON (ValueError, UnicodeDecodeError among them), JSON nested
# deeper than the parser recurses (RecursionError), or too much to hold
# (MemoryError). Any of them means the file cannot be read, not that it is
# empty.
_UNPARSEABLE = (ValueError, RecursionError, MemoryError)


def _lease_manifest(directory: Path) -> dict:
    try:
        return json.loads((directory / "leases.json").read_text())
    except FileNotFoundError:
        return {"leases": {}}
    except (OSError, *_UNPARSEABLE):
        raise AccountError("Temporary CLI account state could not be read") from None


def _save_leases(directory: Path, state: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".leases-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle)
        os.replace(temporary, directory / "leases.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prune_dead_leases(state: dict, owned: dict) -> None:
    """Drop the leases of server processes that exited. A lease under this
    process's own pid that it does not hold (``owned``) was left by an earlier
    server that ran with the same pid, as a restarted container's does; one
    whose pid names no process a server could run as is held by none."""
    for lease, metadata in list(state["leases"].items()):
        pid = metadata.get("pid") if isinstance(metadata, dict) else None
        if type(pid) is not int or not 0 < pid < 2**31:
            del state["leases"][lease]
            continue
        if pid == os.getpid():
            if lease not in owned:
                del state["leases"][lease]
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            del state["leases"][lease]
        except PermissionError:
            pass


# The harness container owns its login document read-write, so the server
# reads at most this much of it: a login document is a few KB, and a larger
# file is not one. Reading no further keeps a harness that fills the file
# from costing the server memory or time each time a sweep looks at it.
_MAX_LEASE_BYTES = 1 << 20


def _open_lease_file(directory: Path, parts: tuple[str, ...]) -> int | None:
    """Open a lease's credential file without following any link.

    Harness containers mount a lease directory read-write, so a harness can
    swap the credential file for a link to any host path. Following it would
    commit that host file into the keyring as the account's login. Walking
    from the lease directory with O_NOFOLLOW at each step refuses links;
    O_NONBLOCK keeps a planted FIFO from hanging the server. Returns a
    descriptor for a regular file, None when the file is missing, and raises
    AccountError for anything else so callers never mistake it for a login.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    folder = flags | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, folder)
    try:
        for part in parts[:-1]:
            child = os.open(part, folder, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        handle = os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptor)
    except FileNotFoundError:
        return None
    except OSError:
        raise AccountError("Temporary CLI account state is not a regular file") from None
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(os.fstat(handle).st_mode):
        os.close(handle)
        raise AccountError("Temporary CLI account state is not a regular file")
    return handle


def _read_login(directory: Path, harness: str) -> str:
    """The login document a lease holds, as the text to commit back.

    "" when it holds none to commit: the document is missing or empty, is a
    link or other non-file (the harness tampered with it), or, on a
    credential_json row, is JSON other than a login object. A document the
    server cannot read raises LoginUnreadable instead: one larger than
    _MAX_LEASE_BYTES or not UTF-8 on any row, and on a credential_json row
    one that is not JSON or nests deeper than the parser recurses (a
    credential_file row, antigravity's token file, is opaque text and never
    parsed). The harness owns the file, and one it left unreadable is
    neither committed nor taken as no login, so its store stays as it is.
    """
    login = PLAN_LOGINS[harness]
    try:
        handle = _open_lease_file(directory, (harness, *login.lease))
    except AccountError:
        return ""
    if handle is None:
        return ""
    with os.fdopen(handle, "rb") as source:
        data = source.read(_MAX_LEASE_BYTES + 1)
    if len(data) <= _MAX_LEASE_BYTES:
        try:
            content = data.decode("utf-8")
            if content and login.field == "credential_json":
                parsed = json.loads(content)
                content = content if isinstance(parsed, dict) and parsed else ""
            return content
        except _UNPARSEABLE:
            pass
    raise LoginUnreadable("The login document in this temporary account store cannot be read")


def missing_login_message(account: dict) -> str:
    """What a surface that reads a plan's pasted login document says when
    the entry holds none: paste it; and, for a plan whose CLI surface signs
    in on this machine, that only that surface runs without it."""
    harness = str((account.get("metadata") or {}).get("harness") or "")
    login = PLAN_LOGINS.get(harness)
    name = os.path.basename(login.relative) if login and login.relative else "login document"
    if not signs_in_on_host(account):
        return f"Paste the selected plan account's {name} into its Key manager entry"
    return (f"Paste the selected plan account's {name} into its Key manager entry for the Annotation "
            f"and Chat surfaces; without it only the CLI surface runs, signing in through "
            f"{MODEL_HARNESS_LABELS.get(harness, harness)}'s own CLI on this machine")


def materialize_cli_account(account: dict, lease_owner: str) -> dict:
    """Lease a temporary selected-account store, shared across server processes.

    Refreshing CLI processes share the vendor's native credential locking.
    Manager edits get a new generation/path and cannot rebind existing runs.
    A document a store kept because the server cannot read it (_read_login)
    is never handed to another run: while no live run holds the store, the
    lease is refused, the document left as it is, until the login is pasted
    in Key manager again (a new generation, a new store).
    """
    harness = account.get("metadata", {}).get("harness")
    if harness not in PLAN_LOGINS:
        raise AccountError("Select a CLI harness for this account in Key manager")
    login = PLAN_LOGINS[harness]
    identity = account["credential_account_id"]
    generation = account["credential_generation"]
    if not _IDENTIFIER.fullmatch(identity) or not _IDENTIFIER.fullmatch(generation):
        raise AccountError("Invalid runtime account identity")
    with _locked():
        root = _cli_root()
        directory = root / f"{identity}-{generation}"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise AccountError("Invalid temporary account directory")
        state = _lease_manifest(directory)
        if state.get("harness", harness) != harness:
            raise AccountError("A credential account cannot be shared across different CLI harnesses")
        _prune_dead_leases(state, _OWNED_CLI_LEASES)
        target = directory.joinpath(harness, *login.lease)
        existing = _open_lease_file(directory, (harness, *login.lease))
        if existing is not None:
            os.close(existing)
            if not state["leases"]:
                # No live run's harness is rewriting the document, so one the
                # server cannot read was left that way: by a run killed
                # mid-write, or by a hostile harness.
                try:
                    _read_login(directory, harness)
                except LoginUnreadable:
                    raise AccountError("A previous run left this plan account's login document unreadable; "
                                       "paste the login document in Key manager again") from None
        else:
            secret = account["secrets"]
            # Operational refresh may have completed after this run's startup
            # snapshot but before its first CLI access. The generation check
            # admits only those refreshes, never a key-manager edit/rebinding.
            current = _read()["accounts"].get(identity)
            if current and current["generation"] == generation:
                secret = {field: keystore.get_key(slot) for field, slot in current["secret_refs"].items()}
            content = secret.get(login.field)
            if not isinstance(content, str) or not content:
                if not state["leases"]:
                    shutil.rmtree(directory)
                raise AccountError(missing_login_message(account))
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
        lease_id = uuid.uuid4().hex
        state.update(identity=identity, generation=generation, harness=harness)
        state["leases"][lease_id] = {"pid": os.getpid(), "owner": lease_owner}
        _save_leases(directory, state)
        _OWNED_CLI_LEASES[lease_id] = (directory, lease_owner)
        return {"path": str(directory), "lease_id": lease_id}


def _collect_cli_login(directory: Path, state: dict) -> dict:
    """Commit a lease's login document back to its account (under _locked()).

    Only while the registry still holds the lease's generation, so a document
    rotated by a run never revives a key-manager edit; a lease whose
    generation is gone has nothing to collect and its document is never
    read. Only a document that reads as a login is committed (_read_login)
    and passes its plan's login validator (PlanLogin.document_problem), and
    one the server cannot read raises LoginUnreadable.
    """
    doc = _read()
    current = doc["accounts"].get(state["identity"])
    if not current or current["generation"] != state["generation"]:
        return {"committed": False}
    login = PLAN_LOGINS[state["harness"]]
    field = login.field
    content = _read_login(directory, state["harness"])
    if not content:
        return {"committed": False}
    # The run's harness owns the document: it is committed only when it still
    # reads as the account's plan login (the validator a paste passes), so a
    # harness that rewrote it with anything else leaves the login as it was.
    problem = login.document_problem(content, str((current.get("metadata") or {}).get("plan") or ""))
    if problem is not None:
        logger.warning("CLI account store %s: the run left a %s login document that is not a login (%s); "
                       "the account keeps its login", directory.name, state["harness"], problem)
        return {"committed": False}
    old_value = keystore.get_key(current["secret_refs"].get(field, "collimate_missing"))
    if content == old_value:
        return {"committed": True, "revision": current["revision"]}
    saved = _save_locked(doc, current["id"], {"revision": current["revision"], "secrets": {field: content}}, refresh=True)
    return {"committed": True, "revision": saved["revision"]}


def release_cli_account(lease_id: str, lease_owner: str) -> dict:
    """Collect refreshes and retire a lease; rejected writes never revive users' edits.

    Every release collects the document, and a collection that fails is
    raised, save one case: a document that cannot be read while another
    lease still holds the store may be that run's harness rewriting it in
    place, so this release collects nothing ({"committed": False}) and
    leaves the document to the last lease out or the sweep. The last lease
    out deletes the store once its document is collected; when that
    collection fails for any reason (the registry could not be read or
    saved, the keyring refused the write, the document cannot be read), the
    store stays with no lease: the next sweep judges it again and collects a
    rotation it can read instead of losing it.
    """
    with _locked():
        owned = _OWNED_CLI_LEASES.get(lease_id)
        if not owned or owned[1] != lease_owner:
            raise AccountError("CLI account lease does not belong to this run")
        directory = owned[0]
        state = _lease_manifest(directory)
        collected = False
        try:
            result = _collect_cli_login(directory, state)
            collected = True
            return result
        except LoginUnreadable:
            if state["leases"].keys() - {lease_id}:
                logger.warning("CLI account store %s: its login document cannot be read yet and is left "
                               "to the lease still holding it", directory.name)
                return {"committed": False}
            raise
        finally:
            state["leases"].pop(lease_id, None)
            _OWNED_CLI_LEASES.pop(lease_id, None)
            if state["leases"] or not collected:
                _save_leases(directory, state)
            else:
                shutil.rmtree(directory)


# Start unwind and an abandoned retain worker may release the same run.
# Serialize only that run, and discard the mutex after its last waiter exits.
_RUN_RELEASES: dict[str, tuple[threading.RLock, int]] = {}


@contextmanager
def _run_release_locked(lease_owner: str):
    with _LOCK:
        mutex, users = _RUN_RELEASES.get(lease_owner, (threading.RLock(), 0))
        _RUN_RELEASES[lease_owner] = (mutex, users + 1)
    try:
        with mutex:
            yield
    finally:
        with _LOCK:
            users = _RUN_RELEASES[lease_owner][1] - 1
            if users:
                _RUN_RELEASES[lease_owner] = (mutex, users)
            else:
                del _RUN_RELEASES[lease_owner]


def release_run_account_leases(lease_owner: str) -> None:
    """Release this run's leases once even when cancellation owners race.

    Ownership snapshots and releases share a per-run mutex. Other runs proceed
    independently; direct per-lease releases retain their owner checks. Try all
    leases and re-raise the last collection failure as before.
    """
    with _run_release_locked(lease_owner):
        failure = None
        for lease_id, (_, owner) in list(_OWNED_CLI_LEASES.items()):
            if owner == lease_owner:
                try:
                    release_cli_account(lease_id, lease_owner)
                except Exception as exc:
                    failure = exc
        if failure:
            raise failure

def _has_login_folder(directory: Path) -> bool:
    """Whether a runtime-root entry holds a harness's login folder. Every CLI
    store with a manifest does (its document is written before the manifest
    is saved)."""
    return any((directory / harness).is_dir() for harness in PLAN_LOGINS)


def _cli_lease_state(directory: Path) -> dict | None:
    """The manifest of a CLI store under the runtime root; None for any other
    entry there. A store whose server died before
    saving its manifest reads as a manifest with no leases. A store whose
    manifest cannot be used (it does not parse; it lacks, as strings, a known
    harness and the identity and generation its folder is named for; or it
    has no lease map) may still be held by a live run, so it is left in
    place and named in a warning."""
    if directory.is_symlink() or not directory.is_dir():
        return None
    login_folder = _has_login_folder(directory)
    try:
        state = json.loads((directory / "leases.json").read_text())
    except FileNotFoundError:
        return {"leases": {}} if login_folder else None
    except (OSError, *_UNPARSEABLE):
        state = None
    if (isinstance(state, dict) and isinstance(state.get("harness"), str) and state["harness"] in PLAN_LOGINS
            and all(isinstance(state.get(key), str) and _IDENTIFIER.fullmatch(state[key]) for key in ("identity", "generation"))
            and directory.name == f"{state['identity']}-{state['generation']}"
            and isinstance(state.get("leases"), dict)):
        return state
    if login_folder:
        logger.warning("CLI account store %s has an unusable lease manifest and is left in place", directory.name)
    return None


def _dead_cli_leases(directory: Path) -> dict | None:
    """A CLI store's manifest with the leases of exited servers pruned, when
    there were any or no lease remains; None when every lease is live and for
    any entry that is not a CLI store."""
    state = _cli_lease_state(directory)
    if state is None:
        return None
    leases = len(state["leases"])
    _prune_dead_leases(state, _OWNED_CLI_LEASES)
    return state if not state["leases"] or len(state["leases"]) != leases else None


def _warn_kept(kind: str, entry: Path, exc: Exception) -> None:
    """Name an entry a sweep leaves for the next one. An error no rule of the
    sweep anticipates (neither AccountError nor OSError) carries its
    traceback."""
    logger.warning("%s %s kept for the next sweep: %s", kind, entry.name, exc,
                   exc_info=None if isinstance(exc, (AccountError, OSError)) else exc)


def sweep_expired_account_leases() -> None:
    """Collect back and remove the CLI stores of server processes that exited.

    A server that exits without releasing (a crash, a kill) leaves its store:
    the plaintext login document, with any rotation its runs made. A store no
    live server holds is collected exactly as release_cli_account collects it
    and then deleted. One whose collection fails for any reason (the registry
    could not be read or saved, the keyring refused the write, the document
    cannot be read) is kept, document and all, and named in a warning at
    every sweep until one collects it or the registry stops holding its
    generation (a Key manager edit, the account's deletion). Each store is
    judged on its own: one the sweep cannot judge, whatever the error, is
    kept and named, and the sweep goes on to the next. Only this registry's
    root is scanned, and the registry lock is taken only when a store there
    has a dead lease or none; each store is judged again under it, the same
    lock materialize_cli_account holds from creating a store to saving its
    manifest.
    """
    stale = []
    for entry in _cli_root().iterdir():
        try:
            if _dead_cli_leases(entry) is not None:
                stale.append(entry)
        except Exception as exc:
            _warn_kept("CLI account store", entry, exc)
    if not stale:
        return
    with _locked():
        for directory in stale:
            try:
                state = _dead_cli_leases(directory)
                if state is None:
                    continue
                if state["leases"]:
                    _save_leases(directory, state)
                    continue
                if "harness" in state:
                    _collect_cli_login(directory, state)
                shutil.rmtree(directory)
            except Exception as exc:
                _warn_kept("CLI account store", directory, exc)


def retain_account_context(context: dict[str, dict], lease_owner: str) -> list[str]:
    """Retain a starting run's CLI login stores, without network I/O; what
    exited servers left is swept first.

    A start that fails releases every lease it took, each on its own: a
    release that fails is named in a warning, never left to hold the rest or
    to hide the error that stopped the start, which is what is raised."""
    acquired = []
    seen = set()

    def leased(row: dict) -> bool:
        login = PLAN_LOGINS.get(row.get("metadata", {}).get("harness"))
        return row.get("kind") != "custom" and bool(login and row.get("secrets", {}).get(login.field))

    if any(leased(row) for row in context.values()):
        sweep_expired_account_leases()
    try:
        for account in context.values():
            if not leased(account):
                continue
            identity = (account.get("credential_account_id"), account.get("credential_generation"), "cli")
            if identity not in seen:
                seen.add(identity)
                acquired.append(materialize_cli_account(account, lease_owner)["lease_id"])
        return acquired
    except Exception:
        for lease in acquired:
            try:
                release_cli_account(lease, lease_owner)
            except Exception:
                logger.warning("Account lease %s of %s, whose start failed, could not be released",
                               lease, lease_owner, exc_info=True)
        raise
