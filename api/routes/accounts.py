"""Global Key manager API; usable before any channel exists."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

import accounts

router = APIRouter()


async def _call(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except accounts.AccountNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except accounts.AccountConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except accounts.AccountConfigError as exc:
        raise HTTPException(409, str(exc)) from None
    except accounts.KeyringUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError:
        raise HTTPException(503, "Could not update the account configuration; the previous file has been preserved") from None


@router.get("/api/accounts")
async def list_accounts():
    return await _call(accounts.list_accounts)


@router.post("/api/accounts", status_code=201)
async def create_account(body: dict[str, Any] = Body(...)):
    return await _call(accounts.create_account, body)


@router.post("/api/accounts/check")
async def check_draft(body: dict[str, Any] = Body(...)):
    """Verify the account an editor currently holds. Nothing is saved."""
    def check():
        from account_adapters import check_draft as adapter_check
        return adapter_check(body)
    return await _call(check)


@router.get("/api/accounts/{identity}")
async def get_account(identity: str):
    return await _call(accounts.get_account, identity)


@router.put("/api/accounts/{identity}")
async def update_account(identity: str, body: dict[str, Any] = Body(...)):
    return await _call(accounts.update_account, identity, body)


@router.delete("/api/accounts/{identity}")
async def delete_account(identity: str, revision: str | None = Query(default=None)):
    await _call(accounts.delete_account, identity, revision=revision)
    return {"ok": True}


@router.post("/api/accounts/{identity}/models")
async def discover_models(identity: str):
    def discover():
        from account_adapters import discover_models as adapter_models
        account = accounts.private_account(identity)
        result = adapter_models(account)
        updated = accounts.cache_manager_result(identity, account["revision"], "model_discovery", result, expected_generation=account["generation"])
        return {**result, "account": updated}
    return await _call(discover)
