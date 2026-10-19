"""管理端：单管理员 + Bearer 令牌 + 内嵌单页后台。

安全取向：
- 不做注册、不做多角色，只有一个管理员密钥（存在 data/.env）
- 后台页面本身不含密钥，用户在页面里输入一次，存 sessionStorage
- 写操作全部落 audit_logs
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from . import security, stats
from .api import config, db, fail
from .config import Config
from .db import DB
from .admin_ui import ADMIN_HTML

router = APIRouter()


def require_admin(request: Request, authorization: str | None) -> None:
    cfg: Config = config(request)
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
    if not token or not secrets.compare_digest(token, cfg.admin_key):
        raise HTTPException(status_code=401, detail={"code": "admin_unauthorized",
                                                     "message": "管理员令牌无效"})


def audit(conn, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_logs (action, actor, detail, created_at) VALUES (?, 'admin', ?, ?)",
        (action, detail, security.iso()),
    )


# ---------------------------------------------------------------- 指标

@router.get("/admin/metrics/overview")
def metrics_overview(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    cfg: Config = config(request)
    with db(request) as conn:
        return stats.overview(conn, cfg.content_dir)


@router.get("/admin/metrics/series")
def metrics_series(
    request: Request,
    days: int = Query(default=30, ge=1, le=90),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {"days": days, "items": stats.series(conn, days)}


@router.get("/admin/metrics/breakdown")
def metrics_breakdown(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {
            "versions": stats.breakdown(conn, "app_version", limit=10),
            "platforms": stats.breakdown(conn, "platform"),
        }


# ---------------------------------------------------------------- 列表

@router.get("/admin/devices")
def admin_devices(
    request: Request,
    query: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {"items": stats.devices(conn, query)}


@router.get("/admin/users")
def admin_users(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {"items": stats.users(conn)}


@router.get("/admin/risk-events")
def admin_risk(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {"items": stats.risk_events(conn)}


@router.get("/admin/audit")
def admin_audit(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {"items": stats.audit(conn)}


# ---------------------------------------------------------------- 写操作

@router.patch("/admin/devices/{device_id}/status")
async def set_device_status(
    device_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in ("active", "banned"):
        fail(400, "invalid_status", "状态只能是 active 或 banned")
    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            fail(404, "device_not_found", "设备不存在")
        conn.execute("UPDATE devices SET status = ? WHERE id = ?", (status, device_id))
        if status == "banned":
            # 封禁即撤销该设备的令牌与全部会话
            conn.execute(
                "UPDATE device_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (stamp, device_id),
            )
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (stamp, device_id),
            )
        audit(conn, f"device_{status}", f"device_id={device_id} serial={row['device_serial']}")
    return {"ok": True, "status": status}


@router.patch("/admin/users/{user_id}/status")
async def set_user_status(
    user_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in ("active", "banned"):
        fail(400, "invalid_status", "状态只能是 active 或 banned")
    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            fail(404, "user_not_found", "用户不存在")
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        if status == "banned":
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (stamp, user_id),
            )
        audit(conn, f"user_{status}", f"user_id={user_id} alias={row['student_alias'][:12]}")
    return {"ok": True, "status": status}


@router.delete("/admin/risk-events")
def clear_risk_events(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        conn.execute("UPDATE risk_events SET cleared_at = ? WHERE cleared_at IS NULL",
                     (security.iso(),))
        audit(conn, "risk_cleared")
    return {"ok": True}


# ---------------------------------------------------------------- 页面

@router.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)
