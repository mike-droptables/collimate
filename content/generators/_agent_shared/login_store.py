# SPDX-License-Identifier: MIT
"""Temporary CLI credential materialization from the selected model account.

The key manager owns sign-in and persistent credentials. A generator gets one
private account snapshot and its adapters materialize only that snapshot.
Temporary stores are never in a cell, export, persistent cache, or user CLI home.
Token refreshes use compare-and-swap write-back through the host.
"""
from __future__ import annotations

import atexit
import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from collimate.account_taxonomy import (
    MODEL_HARNESS_RULES, MODEL_LOGIN_PLANS, PLAN_LOGINS, PLAN_PROVIDERS,
    model_account_provider, supported_model_harnesses,
)
from collimate.fs import open_file
from .secret_hygiene import remember, remember_account, remember_document

PLAN_LABELS = {"chatgpt": "ChatGPT", "github_copilot": "GitHub Copilot", "xai": "xAI", "google": "Google", "claude_code": "Claude Code"}
LOGIN_REQUIRED_TEMPLATE = "Paste the selected {plan_label} login into Key manager before running {harness}."
_LOCK = threading.RLock()
_DIRECTORY = None
_ACCOUNT = None
_LEASE_ID = None
_COPIES = {}
# A login document is a few KB; anything far larger is not one.
_MAX_COPY_BYTES = 1 << 20


def login_required(*, plan: str, harness: str, plan_label: str = "") -> str:
    return LOGIN_REQUIRED_TEMPLATE.format(plan_label=plan_label or PLAN_LABELS.get(plan, plan), harness=harness)


def ensure_store_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    ensure_store_dir(directory)
    descriptor, temporary = tempfile.mkstemp(prefix=".credential-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def store_root() -> str:
    global _DIRECTORY, _ACCOUNT, _LEASE_ID
    with _LOCK:
        if _DIRECTORY is None:
            import gen
            _ACCOUNT = copy.deepcopy(remember_account(gen.account("model")))
            lease = gen.materialize_account("model")
            _DIRECTORY, _LEASE_ID = lease["path"], lease["lease_id"]
        return _DIRECTORY


def store_path(harness: str, *rel: str) -> str:
    """The login document of the selected account's lease, or a folder on
    its path: ``rel`` must lead along ``PLAN_LOGINS[harness].lease``
    (collimate.account_taxonomy), the one place the host writes and collects
    the document, so a harness backend can bind nothing else."""
    with _LOCK:
        root = Path(store_root())
        login = PLAN_LOGINS.get(harness)
        if login is None or tuple(rel) != login.lease[:len(rel)]:
            raise ValueError("Unsupported credential materialization path")
        if (_ACCOUNT.get("metadata") or {}).get("harness") != harness:
            raise ValueError("The selected account belongs to a different CLI harness")
        return str(root.joinpath(harness, *rel))


def login_path(harness: str) -> str:
    """The selected account's login document for ``harness`` in its lease."""
    return store_path(harness, *PLAN_LOGINS[harness].lease)






def selected_login_ready(harness: str, plan: str) -> bool:
    """Read one supported plan from the already selected host-leased snapshot."""
    plan = (plan or "").strip().lower()
    login = PLAN_LOGINS.get(harness)
    if (login is None or plan not in PLAN_PROVIDERS
            or plan not in MODEL_HARNESS_RULES[harness]["auths"]):
        return False
    with _LOCK:
        store_root()
        if harness not in supported_model_harnesses(_ACCOUNT):
            return False
        provider = model_account_provider(_ACCOUNT)
        selected = (_ACCOUNT.get("metadata") or {}).get("plan") or MODEL_LOGIN_PLANS.get(provider)
        if plan != selected or PLAN_PROVIDERS[plan] != provider:
            return False
        path = login_path(harness)
        if login.opaque:
            try:
                content = Path(path).read_bytes()
            except OSError:
                return False
            # An opaque token file has no shape to check (any bytes are a
            # login); a pasted token is checked by its format's validator.
            return bool(content) and (login.validator == "opaque" or login.document_problem(
                content.decode("utf-8", "replace"), plan) is None)
        try:
            content = Path(path).read_text(encoding="utf-8")
            remember_document(json.loads(content))
        except (OSError, ValueError):
            return False
        return login.document_problem(content, plan) is None


def single_file_bind(host_path: str, container_path: str) -> dict:
    return {host_path: {"bind": container_path, "mode": "rw"}}


def dir_bind(host_path: str, container_path: str) -> dict:
    ensure_store_dir(host_path)
    return {host_path: {"bind": container_path, "mode": "rw"}}


def flush_account_credentials() -> None:
    """Refreshes remain in the shared transient document until lease release.

    CLI processes use the vendor's file and refresh locks on this document.
    The host collects it directly and generation-checks refreshed writeback.
    """
    with _LOCK:
        for harness, path in _COPIES.items():
            # register_materialized_copy admits only a login whose harness
            # takes a copy (PlanLogin.copy).
            content = _read_copy_nofollow(*path)
            if content is None:
                continue
            if content and _DIRECTORY:
                # The harness container writes the copy, and the host commits
                # the lease to the account: only a copy that still reads as
                # the selected plan's login (the check a paste passes) goes
                # back, so anything else leaves the leased login as it was.
                if copy_problem(harness, content) is not None:
                    continue
                write_private_atomic(str(Path(_DIRECTORY).joinpath(harness, *PLAN_LOGINS[harness].lease)), content)


def copy_problem(harness: str, content: bytes) -> Optional[str]:
    """Why a harness's returned login copy is not the selected plan's login
    (``PlanLogin.document_problem``), or ``None`` when it is."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "the login copy is not UTF-8 text"
    account = _ACCOUNT or {}
    plan = (account.get("metadata") or {}).get("plan") or ""
    if not plan:
        try:
            plan = MODEL_LOGIN_PLANS.get(model_account_provider(account)) or ""
        except ValueError:
            plan = ""
    return PLAN_LOGINS[harness].document_problem(text, plan)


def _read_copy_nofollow(root: str, path: str) -> Optional[bytes]:
    """Read a regular file under root without following any link.

    The copy lives in a vendor home the harness container mounts read-write,
    so the container can swap the file, or a directory above it, for a link
    to any host path. Following it would copy that host file into the lease
    and the host would commit it to the account. :func:`collimate.fs.open_file`
    walks from root with O_NOFOLLOW at each step and opens non-blocking, so a
    link is refused and a planted FIFO cannot hang the flush. Anything that is
    not a plain file under the size cap reads as None, the cap judged on the
    bytes read (the container can grow the file after it is opened, and a
    truncated credential must never reach the account).
    """
    try:
        handle = open_file(root, os.path.relpath(path, root), write=False)
    except OSError:
        return None
    with os.fdopen(handle, "rb") as source:
        try:
            data = source.read(_MAX_COPY_BYTES + 1)
        except OSError:
            return None
    return None if len(data) > _MAX_COPY_BYTES else data


def register_materialized_copy(harness: str, path: str, root: Optional[str] = None) -> None:
    """Remember a per-run credential copy to return at release.

    ``root`` is the vendor home the copy was written into; the flush reads the
    copy from there without following links (see ``_read_copy_nofollow``).
    """
    login = PLAN_LOGINS.get(harness)
    if login is None or not login.copy:
        raise ValueError("Rotating CLI credentials cannot be copied")
    root = os.path.abspath(root or os.path.dirname(path))
    path = os.path.abspath(path)
    if os.path.commonpath([root, path]) != root or path == root:
        raise ValueError("A credential copy must stay inside its harness home")
    _COPIES[harness] = (root, path)


def close_account_credentials() -> None:
    global _DIRECTORY, _ACCOUNT, _LEASE_ID
    with _LOCK:
        try:
            if _LEASE_ID is not None:
                import gen
                try:
                    flush_account_credentials()
                finally:
                    gen.release_account("model", _LEASE_ID)
        finally:
            _DIRECTORY = _ACCOUNT = _LEASE_ID = None
            _COPIES.clear()


def _exit_cleanup() -> None:
    try:
        close_account_credentials()
    except Exception:
        # Explicit run teardown reports errors; this only removes leftovers
        # when interpreter shutdown made host IPC unavailable.
        pass


atexit.register(_exit_cleanup)


_REFRESH_SIGNATURES = {}


def remember_current_credentials() -> None:
    """Read only changed, bounded regular documents of the selected native lease."""
    with _LOCK:
        if not _DIRECTORY or not _ACCOUNT:
            return
        harness = (_ACCOUNT.get("metadata") or {}).get("harness")
        login = PLAN_LOGINS.get(harness)
        if login is None:
            return
        paths = [(_DIRECTORY, os.path.join(_DIRECTORY, harness, *login.lease))]
        paths.extend(_COPIES.values())
        for root, path in paths:
            try:
                fd = open_file(root, os.path.relpath(path, root), write=False)
                with os.fdopen(fd, "rb") as stream:
                    st = os.fstat(stream.fileno())
                    stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                    if _REFRESH_SIGNATURES.get(path) == stamp:
                        continue
                    data = stream.read(_MAX_COPY_BYTES + 1)
                if len(data) > _MAX_COPY_BYTES:
                    continue
                if login.opaque:
                    remember(data.decode('utf-8').strip())
                else:
                    remember_document(json.loads(data))
                _REFRESH_SIGNATURES[path] = stamp
            except (OSError, ValueError, TypeError):
                continue
