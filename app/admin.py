"""管理端：单管理员 + Bearer 令牌 + 内嵌单页后台。

安全取向：
- 不做注册、不做多角色，只有一个管理员密钥（存在 data/.env）
- 后台页面本身不含密钥，用户在页面里输入一次，存 sessionStorage
- 写操作全部落 audit_logs
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import content, content_store, security, stats
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


# ---------------------------------------------------------------- 内容管理

def _content_meta(content_dir, name: str) -> dict[str, Any]:
    path = content_dir / f"{name}.json"
    if not path.exists():
        return {"name": name, "version": "0", "size": 0, "updatedAt": ""}
    stat = path.stat()
    return {
        "name": name,
        "version": content.version(content_dir, name),
        "size": stat.st_size,
        "updatedAt": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


@router.get("/admin/content")
def admin_content_list(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    cfg: Config = config(request)
    return {"items": [_content_meta(cfg.content_dir, name) for name in content_store.NAMES]}


@router.get("/admin/content/{name}")
def admin_content_get(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    cfg: Config = config(request)
    try:
        data = content_store.read(cfg.content_dir, name)
    except content_store.ContentError as exc:
        fail(400, "invalid_content_name", str(exc))
    meta = _content_meta(cfg.content_dir, name)
    meta["data"] = data
    return meta


@router.put("/admin/content/{name}")
async def admin_content_put(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    cfg: Config = config(request)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict) or "data" not in body:
        fail(400, "missing_data", "请求体需要包含 data 字段")
    try:
        version = content_store.save(cfg.content_dir, name, body["data"])
    except content_store.ContentError as exc:
        fail(400, "invalid_content", str(exc))
    with db(request) as conn:
        audit(conn, "content_saved", f"name={name} version={version}")
    return {"ok": True, "name": name, "version": version}


@router.get("/admin/content/{name}/history")
def admin_content_history(
    name: str,
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    cfg: Config = config(request)
    try:
        return {"items": content_store.history(cfg.content_dir, name, limit)}
    except content_store.ContentError as exc:
        fail(400, "invalid_content_name", str(exc))


@router.post("/admin/content/{name}/restore")
async def admin_content_restore(
    name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    cfg: Config = config(request)
    try:
        body = await request.json()
        filename = str(body.get("file") or "")
        version = content_store.restore(cfg.content_dir, name, filename)
    except content_store.ContentError as exc:
        fail(400, "invalid_content", str(exc))
    with db(request) as conn:
        audit(conn, "content_restored", f"name={name} file={filename} version={version}")
    return {"ok": True, "version": version}


# ---------------------------------------------------------------- 反馈

@router.get("/admin/feedback")
def admin_feedback(
    request: Request,
    status: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    if status and status not in ("new", "read", "done"):
        fail(400, "invalid_status", "状态只能是 new / read / done")
    with db(request) as conn:
        return {"items": stats.feedback(conn, status)}


@router.patch("/admin/feedback/{feedback_id}/status")
async def set_feedback_status(
    feedback_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in ("new", "read", "done"):
        fail(400, "invalid_status", "状态只能是 new / read / done")
    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if row is None:
            fail(404, "feedback_not_found", "反馈不存在")
        handled = stamp if status == "done" else None
        conn.execute(
            "UPDATE feedback SET status = ?, handled_at = ? WHERE id = ?",
            (status, handled, feedback_id),
        )
        audit(conn, f"feedback_{status}", f"feedback_id={feedback_id}")
    return {"ok": True, "status": status}


# ---------------------------------------------------------------- 资源审核

@router.get("/admin/resources")
def admin_resources(
    request: Request,
    status: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    if status and status not in ("pending", "approved", "rejected"):
        fail(400, "invalid_status", "状态只能是 pending / approved / rejected")
    with db(request) as conn:
        rows = conn.execute(
            """SELECT * FROM resource_uploads
               WHERE (? = '' OR status = ?)
               ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC
               LIMIT 500""",
            (status, status),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            files = conn.execute(
                """SELECT id, name, size, kind, downloads
                   FROM resource_files WHERE upload_id = ? ORDER BY id""",
                (row["id"],),
            ).fetchall()
            items.append(
                {
                    "id": row["id"],
                    "courseName": row["course_name"],
                    "bookName": row["book_name"],
                    "area": row["area"],
                    "note": row["note"],
                    "status": row["status"],
                    "reviewNote": row["review_note"],
                    "createdAt": row["created_at"],
                    "reviewedAt": row["reviewed_at"],
                    "files": [
                        {
                            "id": f["id"],
                            "name": f["name"],
                            "size": f["size"],
                            "kind": f["kind"],
                            "downloads": f["downloads"],
                        }
                        for f in files
                    ],
                }
            )
    return {"items": items}


@router.patch("/admin/resources/{upload_id}/status")
async def set_resource_status(
    upload_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in ("approved", "rejected"):
        fail(400, "invalid_status", "状态只能是 approved / rejected")
    review_note = str(body.get("reviewNote") or "").strip()[:500]
    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM resource_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if row is None:
            fail(404, "resource_not_found", "上传记录不存在")
        conn.execute(
            """UPDATE resource_uploads
               SET status = ?, reviewed_at = ?, review_note = ?
               WHERE id = ?""",
            (status, stamp, review_note, upload_id),
        )
        audit(conn, f"resource_{status}", f"upload_id={upload_id}")
    return {"ok": True, "status": status}


@router.get("/admin/resources/traffic")
def admin_resource_traffic(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """资源下载流量：总量与按设备聚合。"""
    require_admin(request, authorization)
    with db(request) as conn:
        totals = conn.execute(
            """SELECT COUNT(*) AS requests,
                      COALESCE(SUM(CASE WHEN mode = 'download' THEN 1 ELSE 0 END), 0) AS downloads,
                      COALESCE(SUM(CASE WHEN mode = 'preview' THEN 1 ELSE 0 END), 0) AS previews,
                      COALESCE(SUM(bytes), 0) AS bytes
               FROM download_events"""
        ).fetchone()
        rows = conn.execute(
            """SELECT e.device_id, d.device_serial, d.status,
                      COUNT(*) AS requests,
                      COALESCE(SUM(CASE WHEN e.mode = 'download' THEN 1 ELSE 0 END), 0) AS downloads,
                      COALESCE(SUM(CASE WHEN e.mode = 'preview' THEN 1 ELSE 0 END), 0) AS previews,
                      COALESCE(SUM(e.bytes), 0) AS bytes,
                      MAX(e.created_at) AS last_at
               FROM download_events e
               LEFT JOIN devices d ON d.id = e.device_id
               GROUP BY e.device_id
               ORDER BY bytes DESC, requests DESC
               LIMIT 200"""
        ).fetchall()
    return {
        "totals": dict(totals),
        "devices": [dict(r) for r in rows],
    }


@router.get("/admin/resources/files/{file_id}")
def admin_resource_file(
    file_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_admin(request, authorization)
    cfg: Config = config(request)
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM resource_files WHERE id = ?", (file_id,)
        ).fetchone()
    if row is None:
        fail(404, "resource_not_found", "文件不存在")
    path = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
    if not path.exists():
        fail(404, "resource_not_found", "文件不存在")
    return FileResponse(path, filename=row["name"])


@router.get("/admin/resources/files/{file_id}/preview")
def admin_resource_preview(
    file_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """管理端内联预览 PDF；非 PDF 返回错误。"""
    require_admin(request, authorization)
    cfg: Config = config(request)
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM resource_files WHERE id = ?", (file_id,)
        ).fetchone()
    if row is None:
        fail(404, "resource_not_found", "文件不存在")
    path = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
    if not path.exists() or path.suffix.lower() != ".pdf":
        fail(404, "preview_unsupported", "该文件不支持预览")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=row["name"],
        content_disposition_type="inline",
    )


# ---------------------------------------------------------------- 页面

@router.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)
