import re
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request

from app.api.endpoints.auth import require_auth
from app.adapters.onebot_v11.config import onebot_v11_config


router = APIRouter()


def _public_account(account: Dict[str, Any], status: Dict[str, Any], reverse_count: int) -> Dict[str, Any]:
    result = dict(account)
    result["access_token"] = ""
    result["access_token_set"] = bool(account.get("access_token"))
    mode = str(account.get("connection_mode") or "forward")
    if reverse_count:
        state = "connected"
    elif not account.get("enabled"):
        state = "disabled"
    elif mode == "reverse":
        state = "waiting"
    else:
        state = str(status.get("state") or "stopped")
    result["status"] = {
        **status,
        "state": state,
        "reverse_connections": reverse_count,
    }
    result["reverse_ws_path"] = f"/onebot/v11/ws/{account['id']}"
    return result


@router.get("/api/adapters/onebot_v11/accounts")
async def get_onebot_accounts(_: bool = Depends(require_auth)):
    from app.adapters.onebot_v11.network.client import onebot_v11_client
    from app.adapters.onebot_v11.network.reverse_ws import reverse_connection_status

    statuses = onebot_v11_client.status()
    reverse = reverse_connection_status()
    accounts = onebot_v11_config.get_accounts(force_reload=True)
    return {
        "accounts": [
            _public_account(account, statuses.get(str(account["id"]), {}), reverse.get(str(account["id"]), 0))
            for account in accounts
        ]
    }


@router.put("/api/adapters/onebot_v11/accounts")
async def put_onebot_accounts(request: Request, _: bool = Depends(require_auth)):
    body = await request.json()
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(status_code=400, detail="至少需要一个 OneBot 账户")
    existing = {str(item["id"]): item for item in onebot_v11_config.get_accounts(force_reload=True)}
    cleaned: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in accounts:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="账户配置格式错误")
        account_id = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id) or account_id in seen:
            raise HTTPException(status_code=400, detail=f"账户 ID 无效或重复: {account_id}")
        seen.add(account_id)
        account = dict(raw)
        account.pop("status", None)
        account.pop("access_token_set", None)
        account.pop("reverse_ws_path", None)
        if not str(account.get("access_token") or ""):
            account["access_token"] = str(existing.get(account_id, {}).get("access_token") or "")
        mode = str(account.get("connection_mode") or "forward")
        if mode not in {"forward", "reverse", "both"}:
            raise HTTPException(status_code=400, detail=f"不支持的 connection_mode: {mode}")
        cleaned.append(account)
    from app.adapters.onebot_v11.network.client import onebot_v11_client

    await onebot_v11_client.stop()
    onebot_v11_config.save_accounts(cleaned)
    await onebot_v11_client.start()
    return await get_onebot_accounts(True)


@router.post("/api/adapters/onebot_v11/reload")
async def reload_onebot_accounts(_: bool = Depends(require_auth)):
    from app.adapters.onebot_v11.network.client import onebot_v11_client

    await onebot_v11_client.stop()
    onebot_v11_config.get_config(force_reload=True)
    await onebot_v11_client.start()
    return await get_onebot_accounts(True)
