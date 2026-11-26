"""管理端：单管理员 + Bearer 令牌 + 内嵌单页后台。

安全取向：
- 不做注册、不做多角色，只有一个管理员密钥（存在 data/.env）
- 后台页面本身不含密钥，用户在页面里输入一次，存 sessionStorage
- 写操作全部落 audit_logs
"""

from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (APIRouter, Header, HTTPException, Query, Request)
from fastapi.responses import FileResponse, HTMLResponse

from . import content, content_store, resource_jobs, security, stats
from .api import (config, db, fail, _edge_enabled, storage_for,
                  _sha256_file)
from .object_storage import CosError
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


@router.get("/admin/metrics/ratings")
def metrics_ratings(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """五星评价按版本 + 平台聚合（同一用户同一版本只算一次）。"""
    require_admin(request, authorization)
    with db(request) as conn:
        return stats.app_ratings(conn)


# ---------------------------------------------------------------- 列表

@router.get("/admin/devices")
def admin_devices(
    request: Request,
    query: str = Query(default=""),
    include_removed: bool = Query(default=False),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        return {"items": stats.devices(conn, query, include_removed=include_removed)}


@router.get("/admin/devices/duplicates")
def admin_device_duplicates(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """列出同一用户下的重复设备：每组留最后活跃的一台，其余是待归档的。

    为什么会有重复：客户端的 installation_id 曾经是纯随机值，只存在本机安全存储，
    卸载即失效 —— 用户每重装一次，控制台就多一台「新设备」。
    这里只做「找出来」，不自动归档，交给后台按钮确认后再执行。
    """
    require_admin(request, authorization)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT d.id, d.device_serial, d.platform, d.app_version, d.status,
                      d.created_at, d.last_seen,
                      (SELECT s.user_id FROM sessions s WHERE s.device_id = d.id
                         ORDER BY s.created_at DESC LIMIT 1) AS user_id,
                      (SELECT COUNT(*) FROM resource_uploads ru
                         WHERE ru.device_id = d.id) AS uploads
                 FROM devices d
                WHERE d.status != 'removed'
                ORDER BY d.last_seen DESC, d.id DESC"""
        ).fetchall()
        aliases = {
            r["id"]: r["student_alias"]
            for r in conn.execute("SELECT id, student_alias FROM users")
        }

    groups: dict[int, dict[str, Any]] = {}
    for r in rows:
        if r["user_id"] is None:
            continue
        item = {
            "id": r["id"],
            "serial": r["device_serial"],
            "platform": r["platform"],
            "appVersion": r["app_version"],
            "status": r["status"],
            "createdAt": r["created_at"],
            "lastSeen": r["last_seen"],
            "uploads": r["uploads"],
        }
        g = groups.setdefault(
            int(r["user_id"]),
            {
                "userId": int(r["user_id"]),
                "alias": aliases.get(r["user_id"], ""),
                "keep": None,
                "extra": [],
            },
        )
        # rows 已按 last_seen DESC 排好，组内第一台就是「最近活跃」→ 保留它。
        if g["keep"] is None:
            g["keep"] = item
        else:
            g["extra"].append(item)

    out = [g for g in groups.values() if g["extra"]]
    return {"groups": out, "count": sum(len(g["extra"]) for g in out)}


@router.post("/admin/devices/archive")
async def archive_devices(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """把重复设备标记为已归档：status='removed' + 撤销令牌与会话。

    不删行：`resource_uploads.device_id` 还指着它，删了上传归属就断了。
    归档设备若真的被装回来，`/api/v1/devices/register` 会自动把它复活。
    """
    require_admin(request, authorization)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        fail(400, "invalid_json", "请求体必须是 JSON 对象")

    raw_ids = body.get("deviceIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        fail(400, "invalid_device_ids", "缺少 deviceIds")
    device_ids: list[int] = []
    for value in raw_ids:
        try:
            device_ids.append(int(value))
        except (TypeError, ValueError):
            fail(400, "invalid_device_ids", "deviceIds 必须是整数数组")

    raw_keep = body.get("keepId")
    try:
        keep_id = int(raw_keep) if raw_keep not in (None, "") else 0
    except (TypeError, ValueError):
        fail(400, "invalid_device_ids", "keepId 必须是整数")

    stamp = security.iso()
    archived: list[int] = []
    with db(request) as conn:
        for device_id in device_ids:
            if device_id == keep_id:
                continue
            row = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            if row is None or row["status"] == "removed":
                continue
            conn.execute(
                "UPDATE devices SET status = 'removed', merged_into = ? WHERE id = ?",
                (keep_id, device_id),
            )
            conn.execute(
                """UPDATE device_tokens SET revoked_at = ?
                   WHERE device_id = ? AND revoked_at IS NULL""",
                (stamp, device_id),
            )
            conn.execute(
                """UPDATE sessions SET revoked_at = ?
                   WHERE device_id = ? AND revoked_at IS NULL""",
                (stamp, device_id),
            )
            archived.append(device_id)
        if archived:
            audit(conn, "device_archived",
                  f"ids={archived} keep={keep_id}")
    return {"ok": True, "archived": archived}


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
        # 统一走 security.iso(+08:00)：直接 datetime.fromtimestamp() 得到的是
        # **naive 本地时间**，服务器没设 TZ=Asia/Shanghai 时会与其它时间字段差 8 小时
        # （2026-11-01 修）。
        "updatedAt": security.iso(datetime.fromtimestamp(stat.st_mtime, security.TZ)),
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
    if status and status not in ("pending", "approved", "rejected", "removed"):
        fail(400, "invalid_status", "状态只能是 pending / approved / rejected / removed")
    cfg: Config = config(request)
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
                """SELECT id, name, size, kind, downloads, object_key, stored_name
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
                    "contributor": row["contributor"] or "",
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
                            "storage": (
                                "cos" if f["object_key"] else
                                ("edge" if _edge_enabled(cfg)
                                 and (cfg.edge_public_dir / str(row["id"]) / f["stored_name"]).exists()
                                 else "local")
                            ),
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
    if status not in ("approved", "rejected", "removed"):
        fail(400, "invalid_status", "状态只能是 approved / rejected / removed")
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
        job_id = resource_jobs.enqueue(
            conn, upload_id, "publish" if status == "approved" else "delete"
        )
    return {"ok": True, "status": status, "jobId": job_id,
            "queued": True}


@router.get("/admin/resource-reports")
def admin_resource_reports(
    request: Request,
    status: str = Query(default="pending"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    if status and status not in ("pending", "resolved", "dismissed"):
        fail(400, "invalid_status", "状态只能是 pending / resolved / dismissed")
    with db(request) as conn:
        rows = conn.execute(
            """SELECT r.*, u.course_name, u.book_name, u.status AS upload_status,
                      f.name AS file_name
               FROM resource_reports r
               LEFT JOIN resource_uploads u ON u.id = r.upload_id
               LEFT JOIN resource_files f ON f.id = r.file_id
               WHERE (? = '' OR r.status = ?)
               ORDER BY r.id DESC
               LIMIT 500""",
            (status, status),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/admin/resource-reports/{report_id}/resolve")
async def resolve_resource_report(
    report_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """处理反馈：`resolve` 只标记已处理；`remove` 同时下架整个上传批次。"""
    require_admin(request, authorization)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")
    action = str(body.get("action") or "").strip()
    note = str(body.get("note") or "").strip()[:500]
    if action not in ("resolve", "remove", "dismiss"):
        fail(400, "invalid_action", "操作只能是 resolve / remove / dismiss")
    stamp = security.iso()
    with db(request) as conn:
        report = conn.execute(
            "SELECT * FROM resource_reports WHERE id = ?", (report_id,)
        ).fetchone()
        if report is None:
            fail(404, "report_not_found", "反馈记录不存在")
        if action == "remove":
            conn.execute(
                """UPDATE resource_uploads
                   SET status = 'removed', reviewed_at = ?, review_note = ?
                   WHERE id = ?""",
                (stamp, note or "用户反馈后下架", report["upload_id"]),
            )
            conn.execute(
                """UPDATE resource_reports SET status = 'resolved',
                   handled_at = ?, handle_note = ?
                   WHERE upload_id = ? AND status = 'pending'""",
                (stamp, note, report["upload_id"]),
            )
        else:
            target = "resolved" if action == "resolve" else "dismissed"
            conn.execute(
                """UPDATE resource_reports
                   SET status = ?, handled_at = ?, handle_note = ?
                   WHERE id = ?""",
                (target, stamp, note, report_id),
            )
        audit(conn, f"resource_report_{action}",
              f"report_id={report_id} upload_id={report['upload_id']}")
        job_id = 0
        if action == "remove":
            job_id = resource_jobs.enqueue_delete(conn, report["upload_id"])
    return {"ok": True, "action": action, "jobId": job_id}


@router.get("/admin/resources/queue")
def admin_resource_queue(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """资源发布 outbox 的队列/失败状态，供控制台和巡检查看。"""
    require_admin(request, authorization)
    with db(request) as conn:
        counts = conn.execute(
            """SELECT status, COUNT(*) AS n FROM resource_publish_jobs
               GROUP BY status"""
        ).fetchall()
        recent = conn.execute(
            """SELECT id, upload_id, action, status, attempts, last_error,
                      created_at, updated_at, next_run_at
               FROM resource_publish_jobs
               ORDER BY id DESC LIMIT 30"""
        ).fetchall()
    count_map = {row["status"]: row["n"] for row in counts}
    return {
        "counts": count_map,
        "recent": [dict(row) for row in recent],
        "pending": count_map.get("pending", 0),
        "failed": count_map.get("failed", 0),
    }


@router.get("/admin/resources/metrics")
def admin_resource_metrics(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    authorization: str | None = Header(default=None),
) -> dict:
    """资源中心按天指标：下载/预览/307 来源/额度拦截/发布失败等。"""
    require_admin(request, authorization)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT day, metric, count FROM resource_daily_metrics
               WHERE day >= date('now', ?)
               ORDER BY day DESC, metric""",
            (f"-{days - 1} days",),
        ).fetchall()
        events = conn.execute(
            """SELECT substr(created_at, 1, 10) AS day,
                      COUNT(*) AS requests,
                      COALESCE(SUM(CASE WHEN mode = 'download' THEN 1 ELSE 0 END), 0) AS downloads,
                      COALESCE(SUM(CASE WHEN mode = 'preview' THEN 1 ELSE 0 END), 0) AS previews,
                      COALESCE(SUM(bytes), 0) AS bytes
               FROM download_events
               WHERE created_at >= date('now', ?)
               GROUP BY substr(created_at, 1, 10)
               ORDER BY day DESC""",
            (f"-{days - 1} days",),
        ).fetchall()
    return {"days": days, "metrics": [dict(r) for r in rows],
            "events": [dict(r) for r in events]}


@router.get("/admin/resources/integrity")
def admin_resource_integrity(
    request: Request,
    limit: int = Query(default=30, ge=1, le=500),
    remote: bool = Query(default=False),
    authorization: str | None = Header(default=None),
) -> dict:
    """抽样/全量对账：本地文件、EdgeOne 镜像、COS 对象与 SHA-256。

    默认只查前 30 个，避免管理页面一次扫 1.3GB；全量用 limit=500
    或直接跑 tools/check_resources.py。
    """
    require_admin(request, authorization)
    cfg: Config = config(request)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT f.id, f.upload_id, f.name, f.stored_name, f.size,
                      f.sha256, f.object_key, u.status
               FROM resource_files f
               JOIN resource_uploads u ON u.id = f.upload_id
               WHERE u.status = 'approved'
               ORDER BY f.id LIMIT ?""",
            (limit,),
        ).fetchall()
    items = []
    store = storage_for(cfg) if remote and cfg.cos_enabled else None
    try:
        for row in rows:
            local = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
            edge = (Path(cfg.edge_public_dir) / str(row["upload_id"])
                    / row["stored_name"]) if _edge_enabled(cfg) else None
            item = {
                "id": row["id"], "uploadId": row["upload_id"],
                "name": row["name"], "size": row["size"],
                "sha256": row["sha256"] or "",
                "local": local.exists() and local.stat().st_size == row["size"],
                "edge": bool(edge is not None and edge.exists()
                             and edge.stat().st_size == row["size"]),
                "cos": bool(row["object_key"]),
            }
            if local.exists() and (row["sha256"] or "").strip():
                item["sha256Match"] = (
                    _sha256_file(local) == row["sha256"]
                )
            if store is not None and row["object_key"]:
                try:
                    item["cos"] = store.object_exists(row["object_key"])
                except CosError as exc:
                    item["cosError"] = str(exc)[:200]
            items.append(item)
    finally:
        if store is not None:
            store.close()
    return {
        "checked": len(items),
        "localMissing": sum(1 for i in items if not i["local"]),
        "edgeMissing": sum(1 for i in items if not i["edge"]),
        "cosMissing": sum(1 for i in items if not i["cos"]),
        "sha256Mismatch": sum(1 for i in items if i.get("sha256Match") is False),
        "items": items,
    }


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
