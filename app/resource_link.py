"""网页版资源直链：给浏览器一条「带签名、短时效」的下载地址。

为什么要单独一个模块（而不是塞进 api.py）：浏览器做顶层导航时**带不了
`Authorization` 头**，网页版也没有本地文件系统可以落盘。所以这里把两件事
拆开——鉴权与计数留在服务端（`/link` 用设备令牌换签名），取字节交给浏览器
自己导航（`/raw` 用签名换文件）。

签名覆盖 `文件 / 模式 / 设备 / 过期时间` 四个维度，10 分钟过期；`/link`
只签名不发字节，也**不扣配额**，配额在 `/raw` 真正下载时才消费。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse

from . import security
from .api import config, db, fail, require_device

router = APIRouter()

#: 直链有效期（秒）。够用户在资源列表里点一下，又短到泄漏了也没多大用。
LINK_TTL_SECONDS = 600

#: 与 `api.download_resource` 保持一致的每日下载上限。
DAILY_DOWNLOAD_LIMIT = 10


# ---------------------------------------------------------------- 签名

def signature(secret: str, file_id: int, mode: str, device_id: int,
              exp: int) -> str:
    """把 file / mode / device / exp 绑进一条 HMAC-SHA256 签名。"""
    message = f"reslink|{file_id}|{mode}|{device_id}|{exp}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _check_signature(cfg: Any, file_id: int, mode: str, dev: str | None,
                     exp: str | None, sig: str | None) -> int:
    """校验直链签名，返回 device_id。任何不规范一律按「链接无效」处理。"""
    if not dev or not exp or not sig:
        fail(401, "missing_device_token", "缺少设备令牌")
    try:
        device_id = int(dev)
        expired_at = int(exp)
    except (TypeError, ValueError):
        fail(403, "link_invalid", "下载链接无效，请重试")
    if expired_at < int(time.time()):
        fail(403, "link_expired", "下载链接已过期，请重新点击下载")
    expect = signature(cfg.token_secret, file_id, mode, device_id, expired_at)
    if not hmac.compare_digest(expect, sig):
        fail(403, "link_invalid", "下载链接无效，请重试")
    return device_id


def _device_by_id(request: Request, device_id: int) -> dict[str, Any]:
    """按 id 取设备行（`require_device` 只认令牌，这里认签名）。"""
    with db(request) as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?",
                           (device_id,)).fetchone()
    if row is None:
        fail(403, "device_banned", "设备已失效，请重新登录")
    if row["status"] != "active":
        fail(403, "device_banned", "设备已被封禁")
    return dict(row)


def _approved_file(conn: Any, file_id: int) -> Any:
    row = conn.execute(
        """SELECT f.*, u.status FROM resource_files f
           JOIN resource_uploads u ON u.id = f.upload_id
           WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if row is None or row["status"] != "approved":
        fail(404, "resource_missing", "资源不存在")
    return row


# ---------------------------------------------------------------- 端点

@router.get("/api/v1/resources/files/{file_id}/link")
def resource_download_link(
    request: Request,
    file_id: int,
    authorization: str | None = Header(default=None),
    mode: str = Query(default="download"),
) -> dict[str, Any]:
    """用设备令牌换一条 10 分钟内有效的直链（不扣配额）。"""
    if mode not in {"download", "preview"}:
        fail(400, "invalid_mode", "下载模式无效")
    device = require_device(request, authorization)
    cfg = config(request)
    with db(request) as conn:
        _approved_file(conn, file_id)
    exp = int(time.time()) + LINK_TTL_SECONDS
    sig = signature(cfg.token_secret, file_id, mode, int(device["id"]), exp)
    return {
        "ok": True,
        "path": (f"/api/v1/resources/files/{file_id}/raw?mode={mode}"
                 f"&dev={device['id']}&exp={exp}&sig={sig}"),
        "expiresIn": LINK_TTL_SECONDS,
    }


@router.get("/api/v1/resources/files/{file_id}/raw")
def resource_file_raw(
    request: Request,
    file_id: int,
    mode: str = Query(default="download"),
    dev: str | None = Query(default=None),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
):
    """按签名直接取字节；浏览器拿到的一定是附件（preview 才内联）。"""
    if mode not in {"download", "preview"}:
        fail(400, "invalid_mode", "下载模式无效")
    cfg = config(request)
    device_id = _check_signature(cfg, file_id, mode, dev, exp, sig)
    device = _device_by_id(request, device_id)

    # 与 `api.download_resource` 同样的顺序：查行 → 拼路径 → 校验存在 → 才写计数。
    with db(request) as conn:
        row = _approved_file(conn, file_id)
        path = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
        if not path.exists():
            fail(404, "resource_missing", "资源文件不存在")

        if mode == "download":
            used = conn.execute(
                """SELECT COUNT(*) AS n FROM download_events
                   WHERE device_id = ? AND mode = 'download'
                     AND substr(created_at, 1, 10) = ?""",
                (device["id"], security.iso()[:10]),
            ).fetchone()["n"]
            if used >= DAILY_DOWNLOAD_LIMIT:
                fail(429, "download_quota",
                     f"今日下载次数已用完（{DAILY_DOWNLOAD_LIMIT} 次）")

        conn.execute(
            """INSERT INTO download_events
               (device_id, file_id, bytes, mode, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (device["id"], file_id, row["size"], mode, security.iso()),
        )
        if mode == "download":
            conn.execute(
                "UPDATE resource_files SET downloads = downloads + 1 WHERE id = ?",
                (file_id,),
            )
        name = row["name"]
        sha = (row["sha256"] or "").strip()

    headers = {"X-Content-SHA256": sha} if sha else None
    return FileResponse(
        Path(path),
        filename=name,
        headers=headers,
        content_disposition_type="inline" if mode == "preview" else "attachment",
    )
