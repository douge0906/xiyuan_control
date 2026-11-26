"""公开 API：设备登记、会话、内容下发、遥测。

三条硬规矩：
1. 不碰教务凭据 —— 只接收客户端 OA 登录后提交的学号与伪名，不读 Cookie/Ticket
2. 任何接口失败都返回统一结构 {error:{code,message}, request_id}
3. 设备与会话令牌在库里只存 HMAC
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import hashlib
import hmac
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import (APIRouter, File, Form, Header,
                     HTTPException, Query, Request, UploadFile)
from fastapi.responses import FileResponse, RedirectResponse

from . import content, edge_api, notify, resource_jobs, security
from .config import Config
from .db import DB
from .object_storage import CosError, storage_for
from .resource_preview import docx_text

router = APIRouter()
logger = logging.getLogger("xiyuan_control.api")


# ---------------------------------------------------------------- 错误

def fail(status: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


# ---------------------------------------------------------------- 依赖

def config(request: Request) -> Config:
    return request.app.state.config


def db(request: Request) -> DB:
    return request.app.state.db


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2:
        return None
    return parts[1].strip()


def touch_device_day(conn, device_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO device_days (device_id, day) VALUES (?, ?)",
        (device_id, security.today()),
    )


def require_device(request: Request, authorization: str | None) -> dict[str, Any]:
    """校验 `Authorization: Device <token>`，返回设备行。"""
    cfg: Config = config(request)
    token = _bearer(authorization)
    if not token:
        fail(401, "missing_device_token", "缺少设备令牌")
    hashed = security.token_hash(token, cfg.token_secret)
    with db(request) as conn:
        row = conn.execute(
            """SELECT d.* FROM device_tokens t
               JOIN devices d ON d.id = t.device_id
               WHERE t.token_hash = ? AND t.revoked_at IS NULL""",
            (hashed,),
        ).fetchone()
        if row is None:
            fail(401, "invalid_device_token", "设备令牌无效")
        if row["status"] != "active":
            fail(403, "device_banned", "设备已被封禁")
        conn.execute("UPDATE device_tokens SET last_used = ? WHERE token_hash = ?",
                     (security.iso(), hashed))
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?",
                     (security.iso(), row["id"]))
        touch_device_day(conn, row["id"])
    return dict(row)


def optional_device(request: Request, authorization: str | None) -> dict[str, Any] | None:
    if not authorization:
        return None
    return require_device(request, authorization)


def require_session(request: Request, authorization: str | None) -> dict[str, Any]:
    """校验 `Authorization: Bearer <access>`，返回 {session, user}。"""
    cfg: Config = config(request)
    token = _bearer(authorization)
    if not token:
        fail(401, "missing_access_token", "缺少访问令牌")
    hashed = security.token_hash(token, cfg.token_secret)
    with db(request) as conn:
        row = conn.execute(
            """SELECT s.*, u.student_alias, u.display_name, u.status AS user_status
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.access_hash = ? AND s.revoked_at IS NULL""",
            (hashed,),
        ).fetchone()
        if row is None:
            fail(401, "invalid_access_token", "访问令牌无效")
        if row["access_exp"] < security.iso():
            fail(401, "access_expired", "访问令牌已过期")
        if row["user_status"] != "active":
            fail(403, "user_banned", "账号已被停用")
        conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?",
                     (security.iso(), row["id"]))
        touch_device_day(conn, row["device_id"])
    return dict(row)


# ---------------------------------------------------------------- 设备

# 设备登记允许的平台标识。
#
# `web` 是网页版（浏览器里跑的 PWA），`android` / `ios` 是两个客户端；
# `probe` / `server` / `uploader` 是库里已有的历史值（早期探针与脚本写入），
# 保留它们是为了老设备重新登记时不会被这次收紧挡下。
#
# platform 参与设备签名（见 security.device_payload），
# 所以这里只做校验、绝不改写客户端上报的值：改写会让签名校验失败。
DEVICE_PLATFORMS = frozenset({
    "android", "ios", "web", "probe", "server", "uploader",
})


@router.post("/api/v1/devices/register")
async def register_device(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")

    installation_id = str(body.get("installation_id") or "").strip()
    public_key = str(body.get("public_key") or "").strip()
    platform = str(body.get("platform") or "")[:32]
    app_version = str(body.get("app_version") or "")[:32]
    created_at = str(body.get("created_at") or "")
    signature = str(body.get("signature") or "")

    if not installation_id or len(installation_id) > 128:
        fail(400, "invalid_installation_id", "安装标识无效")
    if not public_key or not signature:
        fail(400, "missing_signature", "缺少公钥或签名")
    if platform and platform.strip().lower() not in DEVICE_PLATFORMS:
        fail(400, "invalid_platform", "平台标识无效")

    asserted = security.parse_iso(created_at)
    if asserted is None:
        fail(400, "invalid_timestamp", "时间戳格式错误")
    delta = abs((security.now() - asserted).total_seconds())
    if delta > cfg.device_sign_window:
        fail(400, "timestamp_out_of_window", "签名时间超出允许范围，请校准设备时间")

    payload = security.device_payload(
        installation_id, public_key, platform, app_version, created_at
    )
    if not security.verify_signature(public_key, payload, signature):
        fail(400, "invalid_signature", "设备签名校验未通过")

    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE installation_id = ?", (installation_id,)
        ).fetchone()
        if row is None:
            serial = security.b64url_encode(secrets.token_bytes(9))
            conn.execute(
                """INSERT INTO devices
                   (installation_id, public_key, platform, app_version,
                    device_serial, status, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (installation_id, public_key, platform, app_version, serial, stamp, stamp),
            )
            device = conn.execute(
                "SELECT * FROM devices WHERE installation_id = ?", (installation_id,)
            ).fetchone()
        else:
            # 2026-11-01 设备去重配套改动：
            #   installation_id 改为 ANDROID_ID 派生后，重装会带着**同一个** id
            #   和**新的**密钥对回来。旧逻辑在这里 fail(409, device_key_conflict)，
            #   等于把卸载重装的用户永久挡在门外，所以改为接受轮换：
            #   换公钥 + 撤销该设备的旧令牌（旧令牌对应的私钥已不在那台机器上）。
            if row["status"] == "banned":
                fail(403, "device_banned", "设备已被封禁")
            if row["public_key"] != public_key:
                conn.execute(
                    "UPDATE devices SET public_key = ? WHERE id = ?",
                    (public_key, row["id"]),
                )
                conn.execute(
                    """UPDATE device_tokens SET revoked_at = ?
                       WHERE device_id = ? AND revoked_at IS NULL""",
                    (stamp, row["id"]),
                )
            if row["status"] == "removed":
                # 归档过的设备装了回来 —— 复活，不要让人卡在「已归档」状态。
                conn.execute(
                    "UPDATE devices SET status = 'active', merged_into = 0 WHERE id = ?",
                    (row["id"],),
                )
            conn.execute(
                "UPDATE devices SET platform = ?, app_version = ?, last_seen = ? WHERE id = ?",
                (platform or row["platform"], app_version or row["app_version"], stamp, row["id"]),
            )
            device = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (row["id"],)
            ).fetchone()

        token = security.new_token()
        conn.execute(
            """INSERT INTO device_tokens (device_id, token_hash, created_at)
               VALUES (?, ?, ?)""",
            (device["id"], security.token_hash(token, cfg.token_secret), stamp),
        )
        touch_device_day(conn, device["id"])

    return {
        "token": token,
        "device_serial": device["device_serial"],
        "created": row is None,
        "time": stamp,
    }


# ---------------------------------------------------------------- 会话

@router.post("/api/v1/auth/challenges")
def create_challenge(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = require_device(request, authorization)
    cfg: Config = config(request)
    challenge_id = security.b64url_encode(secrets.token_bytes(12))
    nonce = security.b64url_encode(secrets.token_bytes(24))
    with db(request) as conn:
        conn.execute(
            "INSERT INTO challenges (id, device_id, nonce, created_at) VALUES (?, ?, ?, ?)",
            (challenge_id, device["id"], nonce, security.iso()),
        )
    return {
        "challenge_id": challenge_id,
        "challenge": nonce,
        "expires_in": cfg.challenge_ttl,
    }


@router.post("/api/v1/auth/assertions")
async def create_assertion(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    device = require_device(request, authorization)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")

    challenge_id = str(body.get("challenge_id") or "")
    challenge = str(body.get("challenge") or "")
    student_id = str(body.get("student_id") or "").strip()
    alias = str(body.get("student_alias") or "").strip()
    display_name = str(body.get("display_name") or "")[:32]
    asserted_at = str(body.get("asserted_at") or "")
    signature = str(body.get("signature") or "")
    try:
        assertion_version = int(body.get("assertion_version") or 1)
    except (TypeError, ValueError):
        fail(400, "invalid_version", "断言版本无效")
    if assertion_version not in (1, 2):
        fail(400, "invalid_version", "断言版本无效")

    if not signature:
        fail(400, "missing_signature", "缺少签名")

    asserted = security.parse_iso(asserted_at)
    if asserted is None or abs((security.now() - asserted).total_seconds()) > cfg.challenge_ttl:
        fail(400, "assertion_expired", "断言时间无效或已过期")

    legacy_hash = ""
    if assertion_version == 2:
        # v2：客户端只发本地算出的伪名，原始学号不进入请求与签名。
        if not security.is_alias(alias):
            fail(400, "invalid_alias", "伪名格式无效")
        payload = security.login_payload_v2(
            challenge_id, challenge, device["device_serial"],
            alias, display_name, asserted_at,
        )
        if not security.verify_signature(device["public_key"], payload, signature):
            fail(400, "invalid_signature", "登录断言验签失败")
    else:
        # v1：兼容 1.0.9 及更早客户端；原始学号仍会到达，但只用于校验，
        # 服务端不再把它写进任何可枚举字段。
        if not student_id or len(student_id) > 32:
            fail(400, "invalid_student_id", "学号无效")
        expected_alias = security.student_alias(student_id)
        if alias and alias != expected_alias:
            fail(400, "invalid_alias", "伪名与学号不匹配")
        alias = expected_alias
        payload = security.login_payload(
            challenge_id, challenge, device["device_serial"], student_id,
            alias, display_name, asserted_at,
        )
        if not security.verify_signature(device["public_key"], payload, signature):
            fail(400, "invalid_signature", "登录断言验签失败")
        legacy_hash = security.student_hash(student_id)

    account_key = security.account_key(cfg.account_key_secret, alias)
    alias_tag = security.alias_tag(account_key)

    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM challenges WHERE id = ? AND device_id = ?",
            (challenge_id, device["id"]),
        ).fetchone()
        if row is None:
            fail(400, "unknown_challenge", "挑战不存在")
        if row["used_at"]:
            fail(400, "challenge_used", "挑战已被使用")
        if row["nonce"] != challenge:
            fail(400, "challenge_mismatch", "挑战内容不匹配")

        # 风控：10 分钟内同一设备频繁登录
        recent = conn.execute(
            "SELECT COUNT(*) c FROM challenges WHERE device_id = ? AND created_at >= ?",
            (device["id"], security.iso(security.now() - timedelta(minutes=10))),
        ).fetchone()["c"]
        if recent > 5:
            _risk(conn, "login_burst", device_id=device["id"],
                  detail=f"10 分钟内 {recent} 次挑战")

        conn.execute("UPDATE challenges SET used_at = ? WHERE id = ?", (stamp, challenge_id))

        user = conn.execute(
            "SELECT * FROM users WHERE account_key = ?", (account_key,)
        ).fetchone()
        if user is None and legacy_hash:
            # 兼容尚未迁移的存量账号：先按旧学号哈希找到行，再就地改键。
            user = conn.execute(
                "SELECT * FROM users WHERE student_hash = ?", (legacy_hash,)
            ).fetchone()
            if user is not None and not user["account_key"]:
                conn.execute(
                    """UPDATE users
                       SET account_key = ?, student_alias = ?, student_hash = ?
                       WHERE id = ?""",
                    (account_key, alias_tag, "v2:" + account_key, user["id"]),
                )
                user = conn.execute(
                    "SELECT * FROM users WHERE id = ?", (user["id"],)
                ).fetchone()
        if user is None:
            conn.execute(
                """INSERT INTO users (student_hash, student_alias, account_key,
                                      display_name, status, created_at, last_login_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                ("v2:" + account_key, alias_tag, account_key,
                 display_name, stamp, stamp),
            )
            user = conn.execute(
                "SELECT * FROM users WHERE account_key = ?", (account_key,)
            ).fetchone()
        else:
            if user["status"] != "active":
                fail(403, "user_banned", "账号已被停用")
            # 迁移未覆盖到的行顺手补齐展示标签。
            if user["account_key"] == account_key and len(user["student_alias"]) != 8:
                conn.execute(
                    "UPDATE users SET student_alias = ?, student_hash = ? WHERE id = ?",
                    (alias_tag, "v2:" + account_key, user["id"]),
                )
            conn.execute(
                "UPDATE users SET last_login_at = ?, display_name = COALESCE(NULLIF(?, ''), display_name) WHERE id = ?",
                (stamp, display_name, user["id"]),
            )

        # 风控：单账号绑定设备过多（近 30 天有会话的设备）
        device_count = conn.execute(
            """SELECT COUNT(DISTINCT device_id) c FROM sessions
               WHERE user_id = ? AND created_at >= ?""",
            (user["id"], security.iso(security.now() - timedelta(days=30))),
        ).fetchone()["c"]
        if device_count >= 3:
            _risk(conn, "too_many_devices", user_id=user["id"], device_id=device["id"],
                  detail=f"近 30 天设备数 {device_count + 1}")

        access = security.new_token()
        refresh = security.new_token()
        access_exp = security.iso(security.now() + timedelta(seconds=cfg.access_ttl))
        refresh_exp = security.iso(security.now() + timedelta(seconds=cfg.refresh_ttl))
        conn.execute(
            """INSERT INTO sessions
               (user_id, device_id, access_hash, access_exp, refresh_hash,
                refresh_exp, created_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], device["id"],
             security.token_hash(access, cfg.token_secret), access_exp,
             security.token_hash(refresh, cfg.token_secret), refresh_exp,
             stamp, stamp),
        )

    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": cfg.access_ttl,
        "user": {"alias": alias_tag, "display_name": display_name},
    }


@router.post("/api/v1/auth/refresh")
async def refresh_session(request: Request) -> dict[str, Any]:
    cfg: Config = config(request)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")
    token = str(body.get("refresh_token") or "")
    if not token:
        fail(400, "missing_refresh_token", "缺少刷新令牌")

    hashed = security.token_hash(token, cfg.token_secret)
    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE refresh_hash = ? AND revoked_at IS NULL", (hashed,)
        ).fetchone()
        if row is None:
            fail(401, "invalid_refresh_token", "刷新令牌无效")
        if row["refresh_exp"] < stamp:
            fail(401, "refresh_expired", "刷新令牌已过期，请重新登录")

        access = security.new_token()
        refresh = security.new_token()
        conn.execute(
            """UPDATE sessions SET access_hash = ?, access_exp = ?, refresh_hash = ?,
                   refresh_exp = ?, last_seen = ?, revoked_at = NULL
               WHERE id = ?""",
            (security.token_hash(access, cfg.token_secret),
             security.iso(security.now() + timedelta(seconds=cfg.access_ttl)),
             security.token_hash(refresh, cfg.token_secret),
             security.iso(security.now() + timedelta(seconds=cfg.refresh_ttl)),
             stamp, row["id"]),
        )
        touch_device_day(conn, row["device_id"])

    return {"access_token": access, "refresh_token": refresh, "expires_in": cfg.access_ttl}


@router.post("/api/v1/auth/revoke")
def revoke(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = require_device(request, authorization)
    with db(request) as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
            (security.iso(), device["id"]),
        )
    return {"ok": True}


# ---------------------------------------------------------------- 内容

@router.get("/api/v1/app/release")
def app_release(request: Request) -> dict[str, Any]:
    cfg: Config = config(request)
    data = content.load(cfg.content_dir, "app_release") or {}
    return {
        "latestVersion": data.get("latestVersion", "0"),
        "downloadUrl": _app_download_url(
            request, str(data.get("downloadUrl", "")),
        ),
        "notes": data.get("notes", ""),
    }


@router.get("/api/v1/app/download")
def app_download(request: Request) -> RedirectResponse:
    """版本更新 APK 的稳定入口：点击时现签 EdgeOne URL。

    清单里只放这个控制端地址，避免 EdgeOne Type D 签名过期；
    仍保留白名单校验，只允许跳转到控制端配置的 http(s) 地址。
    """
    cfg: Config = config(request)
    data = content.load(cfg.content_dir, "app_release") or {}
    target = str(data.get("downloadUrl") or "").strip()
    if not target.startswith(("https://", "http://")):
        fail(404, "release_missing", "安装包地址未配置")
    if urlsplit(target).path.rstrip("/") == "/api/v1/app/download":
        fail(500, "release_loop", "安装包地址不能指向自身")
    return RedirectResponse(
        _edge_sign_url(cfg, target),
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


@router.head("/api/v1/app/download")
def app_download_head(request: Request) -> RedirectResponse:
    """部分下载器会先发 HEAD 探测大小，跟随同一签名跳转。"""
    return app_download(request)


@router.get("/api/v1/app/download/{filename}")
def app_download_named(request: Request, filename: str) -> RedirectResponse:
    """带文件名的下载入口，供总站「只认 .apk 结尾」的校验使用。

    总站 lib/release.ts 的 pickDownloadUrl 仅在 downloadUrl 以 .apk 结尾时
    才原样采用，否则回退到 CDN 裸链 —— 那条没有 EdgeOne Type D 签名，必 403。
    2026-11-22 起 /api/v1/app/download 以 .apk 收尾的版本走这里，仍由控制端
    点击时现签，总站因此不必重新构建。

    只认清单里那个文件的名字，不做任意跳转，避免变成开放重定向。
    """
    cfg: Config = config(request)
    data = content.load(cfg.content_dir, "app_release") or {}
    target = str(data.get("downloadUrl") or "").strip()
    if not target.startswith(("https://", "http://")):
        fail(404, "release_missing", "安装包地址未配置")
    if Path(urlsplit(target).path).name != filename:
        fail(404, "release_file_mismatch", "安装包文件名不匹配")
    return RedirectResponse(
        _edge_sign_url(cfg, target),
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


@router.head("/api/v1/app/download/{filename}")
def app_download_named_head(
    request: Request, filename: str,
) -> RedirectResponse:
    """HEAD 版本，语义与 GET 一致。"""
    return app_download_named(request, filename)


@router.get("/api/v1/config/versions")
def config_versions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    optional_device(request, authorization)
    release = content.load(cfg.content_dir, "app_release") or {}
    return {
        "appRelease": {
            "latestVersion": release.get("latestVersion", "0"),
            "downloadUrl": _app_download_url(
                request, str(release.get("downloadUrl", "")),
            ),
        },
        "schoolCalendar": content.version(cfg.content_dir, "school_calendar"),
        "messages": content.version(cfg.content_dir, "messages"),
        "examPapers": content.version(cfg.content_dir, "exam_papers"),
        "serverTime": security.iso(),
    }


@router.get("/api/v1/messages")
def messages(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    optional_device(request, authorization)
    return {
        "version": content.version(cfg.content_dir, "messages"),
        "items": content.load(cfg.content_dir, "messages") or [],
    }


@router.get("/api/v1/school-calendar")
def school_calendar(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    optional_device(request, authorization)
    data = content.load(cfg.content_dir, "school_calendar")
    if data is None:
        fail(404, "calendar_not_configured", "校历尚未配置")
    return {"version": content.version(cfg.content_dir, "school_calendar"), "calendar": data}


@router.get("/api/v1/exam-papers/folders")
def exam_papers(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    optional_device(request, authorization)
    return {
        "version": content.version(cfg.content_dir, "exam_papers"),
        "items": content.load(cfg.content_dir, "exam_papers") or [],
    }


# ---------------------------------------------------------------- 遥测

@router.post("/api/v1/telemetry/events")
async def telemetry(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = config(request)
    device = optional_device(request, authorization)
    if device is None:
        fail(401, "missing_device_token", "缺少设备令牌")
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")

    items = body if isinstance(body, list) else [body]
    accepted = 0
    stamp = security.iso()
    with db(request) as conn:
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id") or "")[:64]
            name = str(item.get("name") or "")[:48]
            if not event_id or not name:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO events (device_id, event_id, name, ts)
                   VALUES (?, ?, ?, ?)""",
                (device["id"], event_id, name, str(item.get("ts") or stamp)),
            )
            accepted += cur.rowcount
        # 顺带清理过期事件（每天一次足够，用随机概率触发）
        if secrets.randbelow(50) == 0:
            cutoff = security.iso(security.now() - timedelta(days=cfg.event_retention_days))
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    return {"ok": True, "accepted": accepted}


# ---------------------------------------------------------------- 反馈

@router.post("/api/v1/feedback")
async def submit_feedback(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """匿名反馈：带设备令牌时记录设备，未登录也可提交。"""
    device = optional_device(request, authorization)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")

    text = str(body.get("content") or "").strip()
    contact = str(body.get("contact") or "").strip()[:120]
    app_version = str(body.get("app_version") or "").strip()[:32]
    if not text:
        fail(400, "empty_feedback", "反馈内容不能为空")
    if len(text) > 2000:
        fail(400, "feedback_too_long", "反馈内容最多 2000 字")

    stamp = security.iso()
    with db(request) as conn:
        cur = conn.execute(
            """INSERT INTO feedback (device_id, content, contact, app_version, status, created_at)
               VALUES (?, ?, ?, ?, 'new', ?)""",
            (device["id"] if device else None, text, contact, app_version, stamp),
        )
        feedback_id = cur.lastrowid

    # 落到库里之后立刻推微信。后台线程，失败只记日志 —— 推送不能影响用户提交。
    notify.push_feedback_async(
        config(request).feedback_showdoc_token,
        {
            "id": feedback_id,
            "content": text,
            "contact": contact,
            "app_version": app_version,
            "created_at": stamp,
        },
    )
    return {"ok": True, "id": feedback_id, "time": stamp}


# ---------------------------------------------------------------- 资源上传

_RESOURCE_AREAS = {"textbook", "exam", "exam_paper", "answer"}
_RESOURCE_EXT = {
    "textbook": {".pdf"},
    "exam_paper": {".pdf", ".doc", ".docx"},
    "answer": {".pdf", ".doc", ".docx"},
}
_MAX_FILE_BYTES = 80 * 1024 * 1024
_MAX_TOTAL_BYTES = 80 * 1024 * 1024
_MAX_FILES = 10
_MAX_ANSWER_FILES = 10
# 兼容旧代码/旧测试的默认值；实际额度以 Config.resource_daily_download_limit 为准。
_DAILY_DOWNLOAD_LIMIT = 5
_REPORT_REASONS = {"broken", "mismatch", "duplicate", "copyright", "other"}


def _device_account_id(conn: Any, device_id: int) -> int | None:
    """解析设备当前所属账号（只用于额度口径）。

    设备令牌本身不含用户；优先取该设备最近一条未撤销登录会话，
    未登录时回退到该设备创建的邀请码所属账号。这样用户换机登录后
    额度仍跟着账号走，退出登录也不会立刻丢额度。
    """
    session = conn.execute(
        """SELECT user_id FROM sessions
           WHERE device_id = ? AND revoked_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (device_id,),
    ).fetchone()
    if session is not None:
        return int(session["user_id"])
    code = conn.execute(
        """SELECT user_id FROM invite_codes
           WHERE device_id = ? AND status = 'active'
           ORDER BY created_at DESC LIMIT 1""",
        (device_id,),
    ).fetchone()
    if code is not None:
        return int(code["user_id"])
    return None


def _invite_bonus_for_user(
    conn: Any, cfg: Config, user_id: int,
) -> tuple[int, int]:
    """按指定账号的已激活邀请数计算下载加成。"""
    invited = int(conn.execute(
        """SELECT COUNT(*) AS n FROM invite_relations
           WHERE inviter_user_id = ? AND status = 'activated'""",
        (user_id,),
    ).fetchone()["n"])
    per_invite = max(0, int(getattr(
        cfg, "resource_invite_bonus_per_invite", 10
    )))
    cap = max(0, int(getattr(cfg, "resource_invite_bonus_limit", 1000)))
    bonus = invited * per_invite
    if cap > 0 and bonus > cap:
        bonus = cap
    return bonus, invited


def _invite_download_bonus(
    conn: Any, cfg: Config, device_id: int,
) -> tuple[int, int]:
    """按当前设备所属账号的已激活邀请数计算下载加成。"""
    user_id = _device_account_id(conn, device_id)
    if user_id is None:
        return 0, 0
    return _invite_bonus_for_user(conn, cfg, user_id)


def _quota_limits(
    conn: Any,
    cfg: Config,
    device: dict[str, Any],
    account_id: int | None = None,
) -> tuple[int, int, int]:
    """返回 (基础额度, 邀请加成, 已激活邀请人数)。"""
    base = max(1, int(getattr(cfg, "resource_daily_download_limit", 5)))
    if account_id is None:
        account_id = _device_account_id(conn, int(device["id"]))
    if account_id is None:
        return base, 0, 0
    bonus, invited = _invite_bonus_for_user(conn, cfg, account_id)
    return base, bonus, invited


def _download_used(
    conn: Any,
    device_id: int,
    account_id: int | None,
    today: str,
) -> int:
    """今日已用下载次数。

    登录账号：跨设备累计；为兼容旧数据，仍把本设备上 user_id 为空
    的历史记录计入当前账号。
    未登录：按设备累计全部记录，避免退出登录后额度重置。
    """
    if account_id is None:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM download_events
               WHERE device_id = ? AND mode = 'download'
                 AND substr(created_at, 1, 10) = ?""",
            (device_id, today),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM download_events
               WHERE mode = 'download'
                 AND substr(created_at, 1, 10) = ?
                 AND (user_id = ? OR (user_id IS NULL AND device_id = ?))""",
            (today, account_id, device_id),
        ).fetchone()
    return int(row["n"])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _bump_metric(conn: Any, metric: str, amount: int = 1) -> None:
    stamp = security.iso()[:10]
    conn.execute(
        """INSERT INTO resource_daily_metrics (day, metric, count)
           VALUES (?, ?, ?)
           ON CONFLICT(day, metric) DO UPDATE SET count = count + excluded.count""",
        (stamp, metric, max(0, int(amount))),
    )


def _edge_base_url(cfg: Config, upload_id: int, stored_name: str) -> str:
    """EdgeOne 公开路径。签名参数在返回 307 时按每次请求生成。"""
    return f"{cfg.edge_resource_base}/{upload_id}/{stored_name}"


def _edge_sign_url(cfg: Config, url: str) -> str:
    """按 EdgeOne Type D 规则给任意静态 URL 追加短时签名。

    算法与预热/刷新共用 edge_api.sign_url，避免两处实现漂移。
    """
    return edge_api.sign_url(cfg, url)


def _edge_signed_url(cfg: Config, upload_id: int, stored_name: str) -> str:
    """资源中心文件路径的 EdgeOne 短时 URL。"""
    return _edge_sign_url(cfg, _edge_base_url(cfg, upload_id, stored_name))


def _app_download_url(request: Request, raw_url: str) -> str:
    """把静态托管的 APK 地址换成控制端签名跳转地址。

    客户端更新下载由系统浏览器打开，最终仍走 EdgeOne；换成后端入口后，
    每次点击时再生成 Type D 签名，避免把 15 分钟后就失效的签名写进清单。

    2026-11-22：入口补上 APK 文件名，让地址以 .apk 结尾。总站
    lib/release.ts 的 pickDownloadUrl 只认 .apk 收尾的 downloadUrl，
    否则会回退到无签名的 CDN 裸链并 403；补文件名后总站原样采用，
    不必重新构建 Next.js。
    """
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    if urlsplit(raw).netloc.lower() == "static.wxxydouge.icu":
        name = Path(urlsplit(raw).path).name or "xiyuan-pocket.apk"
        base = f"{str(request.base_url).rstrip('/')}/api/v1/app/download"
        return f"{base}/{name}"
    return raw


def _edge_enabled(cfg: Config) -> bool:
    return bool(cfg.edge_resource_base and cfg.edge_public_dir)


def _mirror_resource_files(cfg: Config, upload_id: int, rows: list[Any]) -> None:
    """把已审核文件镜像到 Nginx/EdgeOne 静态目录，供 307 直链取字节。

    只镜像 approved 批次；不用软链整个 uploads，避免审核前文件被直链访问。
    优先硬链接，跨文件系统时退回复制。失败只记日志，不阻断审核。
    """
    if not _edge_enabled(cfg):
        return
    dest_dir = Path(cfg.edge_public_dir) / str(upload_id)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_dir.chmod(0o755)
    except OSError as exc:
        logger.warning("EdgeOne 镜像目录创建失败 upload=%s: %s", upload_id, exc)
        return
    for row in rows:
        local = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
        if not local.exists():
            continue
        dest = dest_dir / row["stored_name"]
        if dest.exists():
            continue
        try:
            os.link(local, dest)
        except OSError:
            try:
                shutil.copy2(local, dest)
            except OSError as exc:
                logger.warning("EdgeOne 镜像复制失败 upload=%s: %s", upload_id, exc)
                continue
        try:
            dest.chmod(0o644)
        except OSError:
            pass
    logger.info("EdgeOne 镜像完成 upload=%s files=%s", upload_id, len(rows))


def _remove_edge_resource_dir(cfg: Config, upload_id: int) -> None:
    if not _edge_enabled(cfg):
        return
    target = Path(cfg.edge_public_dir) / str(upload_id)
    shutil.rmtree(target, ignore_errors=True)


def backfill_all_resources(app: Any) -> None:
    """启动时补传/镜像历史已审核文件。

    线上已有资源不会走「上传 → 审核」流程，若只在上传/审核时发布，
    历史文件会永远留在本地。启动时后台执行一次；COS 已存在对象会跳过，
    EdgeOne 已镜像文件会跳过，单个失败不阻断其余文件。
    """
    cfg: Config = app.state.config
    if not cfg.cos_enabled and not _edge_enabled(cfg):
        return
    with app.state.db as conn:
        ids = [
            row["upload_id"]
            for row in conn.execute(
                """SELECT DISTINCT f.upload_id FROM resource_files f
                   JOIN resource_uploads u ON u.id = f.upload_id
                   WHERE u.status = 'approved'
                   ORDER BY f.upload_id"""
            ).fetchall()
        ]
    logger.info("资源发布历史补传开始，批次数=%s", len(ids))
    for upload_id in ids:
        try:
            _publish_resource_files(app, upload_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("资源发布历史补传失败 upload=%s: %s", upload_id, exc)
    logger.info("资源发布历史补传结束，批次数=%s", len(ids))


def _publish_resource_files(app: Any, upload_id: int) -> None:
    """把某个已审核批次的文件发布到 EdgeOne 静态缓存和/或 COS。

    上传接口先返回、审核通过后再在后台执行；用户不用等上传往返。
    审核/下载时如果对象还没发布完，下载接口会自然回退到本地文件。
    失败只记日志，不影响审核。未审核批次不发布，避免审核前直链泄漏。
    """
    cfg: Config = app.state.config
    with app.state.db as conn:
        status_row = conn.execute(
            "SELECT status FROM resource_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if status_row is None or status_row["status"] != "approved":
            return
        rows = conn.execute(
            """SELECT id, upload_id, stored_name, object_key, sha256
               FROM resource_files WHERE upload_id = ? ORDER BY id""",
            (upload_id,),
        ).fetchall()
    _mirror_resource_files(cfg, upload_id, rows)
    store = storage_for(cfg)
    if not store.enabled:
        # 即使 COS 未启用，也把历史文件的 SHA-256 补齐，供对账使用。
        _fill_sha256(cfg, app.state.db, rows)
        try:
            edge_api.purge_upload(cfg, app.state.db, upload_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EdgeOne 刷新失败 upload=%s: %s", upload_id, exc)
        _prefetch_upload_quietly(cfg, app.state.db, upload_id)
        return
    try:
        for row in rows:
            key = store.object_key(upload_id, row["stored_name"])
            local = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
            sha256 = (row["sha256"] or "").strip()
            if not sha256 and local.exists():
                sha256 = _sha256_file(local)
                with app.state.db as conn:
                    conn.execute(
                        "UPDATE resource_files SET sha256 = ? WHERE id = ?",
                        (sha256, row["id"]),
                    )
            if row["object_key"]:
                try:
                    if store.object_exists(row["object_key"]):
                        continue
                except CosError as exc:
                    logger.warning("COS HEAD 失败 upload=%s: %s", upload_id, exc)
            if not local.exists():
                logger.warning("COS 上传跳过：本地文件缺失 %s", local)
                continue
            try:
                store.upload_file(key, local)
            except Exception as exc:  # noqa: BLE001 - 后台任务不能让接口失败
                logger.warning("COS 上传失败 upload=%s key=%s: %s",
                               upload_id, key, exc)
                continue
            with app.state.db as conn:
                conn.execute(
                    """UPDATE resource_files
                       SET object_key = ?, sha256 = COALESCE(NULLIF(sha256, ''), ?)
                       WHERE id = ?""",
                    (key, sha256, row["id"]),
                )
            logger.info("COS 上传完成 upload=%s key=%s", upload_id, key)
    finally:
        store.close()
    # 发布完成后刷新 EdgeOne 旧缓存，避免 30 天 TTL 内继续返回旧文件。
    try:
        edge_api.purge_upload(cfg, app.state.db, upload_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EdgeOne 刷新失败 upload=%s: %s", upload_id, exc)
    _prefetch_upload_quietly(cfg, app.state.db, upload_id)


def _prefetch_upload_quietly(cfg: Config, dbx: DB, upload_id: int) -> None:
    """发布后预热本批资源，让第一位下载者直接命中边缘缓存。

    刷新（purge）是异步任务，若与预热同时提交，可能出现"预热刚完成又被
    清掉"的竞态；这里等 3 秒再提交预热。失败只记日志，不影响审核结果。
    """
    if not _edge_enabled(cfg) or not cfg.edge_zone_id:
        return
    try:
        time.sleep(3)
        result = edge_api.prefetch_upload(cfg, dbx, upload_id)
        logger.info(
            "EdgeOne 预热完成 upload=%s urls=%s failed=%s",
            upload_id, result.get("urls"), result.get("failedCount"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("EdgeOne 预热失败 upload=%s: %s", upload_id, exc)


def _fill_sha256(cfg: Config, dbx: DB, rows: list[Any]) -> None:
    """为历史文件补齐 SHA-256；单个文件读取失败不影响其他文件。"""
    for row in rows:
        if (row["sha256"] or "").strip():
            continue
        local = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
        if not local.exists():
            continue
        try:
            digest = _sha256_file(local)
        except OSError as exc:
            logger.warning("SHA-256 计算失败 %s: %s", local, exc)
            continue
        with dbx as conn:
            conn.execute(
                "UPDATE resource_files SET sha256 = ? WHERE id = ?",
                (digest, row["id"]),
            )


def _delete_resource_objects(app: Any, upload_id: int) -> None:
    """下架/拒绝后尽力删除 COS 对象和 EdgeOne 镜像，避免直链继续可访问。"""
    cfg: Config = app.state.config
    _remove_edge_resource_dir(cfg, upload_id)
    store = storage_for(cfg)
    if not store.enabled:
        try:
            edge_api.purge_upload(cfg, app.state.db, upload_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EdgeOne 下架刷新失败 upload=%s: %s", upload_id, exc)
        return
    try:
        with app.state.db as conn:
            rows = conn.execute(
                """SELECT id, upload_id, stored_name, object_key
                   FROM resource_files WHERE upload_id = ? ORDER BY id""",
                (upload_id,),
            ).fetchall()
        for row in rows:
            key = row["object_key"] or store.object_key(upload_id,
                                                         row["stored_name"])
            try:
                store.delete_object(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("COS 删除失败 upload=%s key=%s: %s",
                               upload_id, key, exc)
                continue
            with app.state.db as conn:
                conn.execute(
                    "UPDATE resource_files SET object_key = '' WHERE id = ?",
                    (row["id"],),
                )
    finally:
        store.close()
    # 下架后不清缓存，已提交到 EdgeOne 的旧文件仍会在 TTL 内被访问。
    try:
        edge_api.purge_upload(cfg, app.state.db, upload_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EdgeOne 下架刷新失败 upload=%s: %s", upload_id, exc)


def _cleanup_resource_upload(request: Request, folder: Path, upload_id: int) -> None:
    for f in folder.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass
    with db(request) as conn:
        conn.execute("DELETE FROM resource_files WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM resource_uploads WHERE id = ?", (upload_id,))


@router.post("/api/v1/resource-uploads")
async def create_resource_upload(
    request: Request,
    authorization: str | None = Header(default=None),
    course_name: str = Form(...),
    book_name: str = Form(""),
    area: str = Form(...),
    note: str = Form(""),
    contributor: str = Form(""),
    files: list[UploadFile] = File(...),
    answer_files: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    """提交资源上传，进入控制台待审队列。

    `area=exam` 时 `files` 是试卷、`answer_files` 是随卷答案，一次提交绑定。
    """
    device = require_device(request, authorization)
    cfg: Config = config(request)

    course = course_name.strip()
    book = book_name.strip()
    area = area.strip()
    note = note.strip()
    contributor = contributor.strip()
    if not course or len(course) > 256 or "\n" in course or "\r" in course:
        fail(400, "invalid_course", "课程名称无效")
    if len(book) > 256 or "\n" in book or "\r" in book:
        fail(400, "invalid_book", "教材名称无效")
    if area not in _RESOURCE_AREAS:
        fail(400, "invalid_area", "上传区域无效")
    if len(note) > 500:
        fail(400, "note_too_long", "备注最多 500 字")
    if len(contributor) > 32 or "\n" in contributor or "\r" in contributor:
        fail(400, "invalid_contributor", "署名无效（最多 32 字，不能换行）")

    if area == "textbook":
        main_kind = "textbook"
        answers: list[UploadFile] = []
    elif area == "exam":
        main_kind = "exam_paper"
        answers = list(answer_files or [])
    else:
        main_kind = area
        answers = []
    if area == "textbook" and answer_files:
        fail(400, "invalid_answers", "教材 PDF 不接受答案文件")
    if not files:
        fail(400, "missing_files", "请选择文件")
    if len(files) > _MAX_FILES:
        fail(400, "too_many_files", "试卷/教材一次最多 10 个文件")
    if len(answers) > _MAX_ANSWER_FILES:
        fail(400, "too_many_answers", "答案一次最多 10 个文件")
    if area == "exam" and len(files) > 1:
        fail(400, "too_many_papers", "一份试卷只能提交一个主文件")

    stamp = security.iso()
    with db(request) as conn:
        cur = conn.execute(
            """INSERT INTO resource_uploads
               (device_id, course_name, book_name, area, note, contributor,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (device["id"], course, book, area, note, contributor, stamp),
        )
        upload_id = cur.lastrowid

    folder = cfg.upload_dir / str(upload_id)
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[tuple[str, str, int, str, str]] = []
    try:
        for kind, items in ((main_kind, files), ("answer", answers)):
            for item in items:
                original = Path(item.filename or "").name.strip()[:200]
                if not original:
                    fail(400, "invalid_filename", "文件名无效")
                suffix = Path(original).suffix.lower()
                if suffix not in _RESOURCE_EXT[kind]:
                    fail(400, "invalid_file_type", f"文件格式不支持：{original}")
                stored = f"{uuid.uuid4().hex}{suffix}"
                dest = folder / stored
                size = 0
                digest = hashlib.sha256()
                with dest.open("wb") as out:
                    while True:
                        chunk = await item.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _MAX_FILE_BYTES:
                            fail(400, "file_too_large",
                                 f"{original} 超过 80MB")
                        out.write(chunk)
                        digest.update(chunk)
                if size <= 0:
                    fail(400, "empty_file", f"{original} 是空文件")
                saved.append((original, stored, size, kind,
                              digest.hexdigest()))
                await item.close()

        if not saved:
            fail(400, "missing_files", "没有可保存的文件")
        if sum(item[2] for item in saved) > _MAX_TOTAL_BYTES:
            fail(400, "total_too_large", "文件总大小超过 80MB")

        with db(request) as conn:
            first_main_id = 0
            for original, stored, size, kind, sha256 in saved:
                if kind == "answer":
                    continue
                cur = conn.execute(
                    """INSERT INTO resource_files
                       (upload_id, name, stored_name, size, kind, parent_id,
                        sha256)
                       VALUES (?, ?, ?, ?, ?, 0, ?)""",
                    (upload_id, original, stored, size, kind, sha256),
                )
                if first_main_id == 0:
                    first_main_id = cur.lastrowid
            for original, stored, size, kind, sha256 in saved:
                if kind != "answer":
                    continue
                conn.execute(
                    """INSERT INTO resource_files
                       (upload_id, name, stored_name, size, kind, parent_id,
                        sha256)
                       VALUES (?, ?, ?, ?, 'answer', ?, ?)""",
                    (upload_id, original, stored, size, first_main_id, sha256),
                )
    except HTTPException:
        _cleanup_resource_upload(request, folder, upload_id)
        raise
    except Exception:
        _cleanup_resource_upload(request, folder, upload_id)
        fail(400, "upload_failed", "文件保存失败，请检查大小与格式")

    # 审核通过前不发布；审核动作会把 publish job 写入 outbox，由独立 worker 消费。
    return {"ok": True, "id": upload_id, "status": "pending", "time": stamp}


@router.get("/api/v1/resources")
def list_resources(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """审核通过后的共享资源，按课程 + 教材分组；需要设备身份。"""
    require_device(request, authorization)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT u.id AS upload_id, u.course_name, u.book_name,
                      u.contributor, u.reviewed_at, u.created_at,
                      f.id AS file_id, f.name, f.size, f.kind,
                      f.downloads, f.parent_id, f.sha256
               FROM resource_uploads u
               JOIN resource_files f ON f.upload_id = u.id
               WHERE u.status = 'approved'
               ORDER BY u.course_name, u.book_name, u.id, f.id"""
        ).fetchall()

    courses: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["course_name"], row["book_name"])
        course = courses.setdefault(
            key,
            {
                "courseName": row["course_name"],
                "bookName": row["book_name"],
                "updatedAt": row["reviewed_at"] or row["created_at"],
                "files": [],
            },
        )
        stamp = row["reviewed_at"] or row["created_at"]
        if stamp > course["updatedAt"]:
            course["updatedAt"] = stamp
        course["files"].append(
            {
                "id": row["file_id"],
                "uploadId": row["upload_id"],
                "name": row["name"],
                "area": row["kind"],
                "size": row["size"],
                "downloads": row["downloads"],
                "parentId": row["parent_id"],
                "sha256": row["sha256"] or "",
                "contributor": row["contributor"] or "",
                "createdAt": stamp,
                "downloadUrl": f"/api/v1/resources/files/{row['file_id']}",
            }
        )
    return {"courses": list(courses.values())}


@router.get("/api/v1/resources/quota")
def resource_quota(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """当前设备/账号今日下载额度，供详情页提前展示剩余次数。"""
    device = require_device(request, authorization)
    today = security.iso()[:10]
    cfg = config(request)
    with db(request) as conn:
        account_id = _device_account_id(conn, int(device["id"]))
        used = _download_used(conn, int(device["id"]), account_id, today)
        base_limit, bonus_limit, invited_count = _quota_limits(
            conn, cfg, device, account_id=account_id,
        )
    limit = base_limit + bonus_limit
    scope = "account" if account_id is not None else "device"
    mode = "base_only" if bonus_limit == 0 else "base_plus_bonus"
    per_invite = max(0, int(getattr(
        cfg, "resource_invite_bonus_per_invite", 10
    )))
    scope_note = "按账号" if scope == "account" else "未登录，按设备"
    note = (
        f"当前{scope_note}每日基础额度 {base_limit} 次；"
        f"邀请 1 人每日追加 {per_invite} 次。"
        if bonus_limit == 0
        else f"当前{scope_note}每日基础额度 {base_limit} 次，"
             f"已邀请 {invited_count} 人，加成 {bonus_limit} 次。"
    )
    return {
        "limit": limit,
        "baseLimit": base_limit,
        "bonusLimit": bonus_limit,
        "bonusPerInvite": per_invite,
        "invitedCount": invited_count,
        "used": used,
        "remaining": max(0, limit - used),
        "date": today,
        "scope": scope,
        "accountBound": account_id is not None,
        "policy": {"mode": mode, "note": note},
    }


@router.post("/api/v1/resources/reports")
async def create_resource_report(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """用户反馈：文件失效、内容不符、重复或侵权。"""
    device = require_device(request, authorization)
    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        fail(400, "invalid_json", "请求体必须是 JSON 对象")
    try:
        upload_id = int(body.get("uploadId") or 0)
        file_id = int(body.get("fileId") or 0)
    except (TypeError, ValueError):
        fail(400, "invalid_resource", "资源编号无效")
    reason = str(body.get("reason") or "").strip()
    detail = str(body.get("detail") or "").strip()
    if reason not in _REPORT_REASONS:
        fail(400, "invalid_reason", "反馈类型无效")
    if len(detail) > 500:
        fail(400, "detail_too_long", "补充说明最多 500 字")
    if upload_id <= 0:
        fail(400, "invalid_resource", "上传批次编号无效")

    with db(request) as conn:
        upload = conn.execute(
            "SELECT id, status FROM resource_uploads WHERE id = ?",
            (upload_id,),
        ).fetchone()
        if upload is None:
            fail(404, "resource_not_found", "资源不存在")
        if upload["status"] != "approved":
            fail(409, "resource_unavailable", "该资源已不在公开列表")
        if file_id > 0:
            owner = conn.execute(
                "SELECT upload_id FROM resource_files WHERE id = ?",
                (file_id,),
            ).fetchone()
            if owner is None or owner["upload_id"] != upload_id:
                fail(400, "invalid_resource", "文件与上传批次不匹配")
        duplicate = conn.execute(
            """SELECT id FROM resource_reports
               WHERE device_id = ? AND upload_id = ? AND file_id = ?
                 AND status = 'pending'""",
            (device["id"], upload_id, file_id),
        ).fetchone()
        if duplicate is not None:
            return {"ok": True, "id": duplicate["id"], "duplicate": True}
        cur = conn.execute(
            """INSERT INTO resource_reports
               (upload_id, file_id, device_id, reason, detail, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (upload_id, file_id, device["id"], reason, detail, security.iso()),
        )
        report_id = cur.lastrowid
    return {"ok": True, "id": report_id, "duplicate": False}


@router.get("/api/v1/resources/files/{file_id}/text")
def preview_resource_text(
    request: Request,
    file_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """DOCX 文本预览；不占下载额度、不记录下载事件。

    `.doc` 是二进制格式，服务端没有 LibreOffice，无法可靠提取；
    返回 415 让客户端明确提示“下载后用 Word 打开”。
    """
    require_device(request, authorization)
    cfg: Config = config(request)
    with db(request) as conn:
        row = conn.execute(
            """SELECT f.*, u.status FROM resource_files f
               JOIN resource_uploads u ON u.id = f.upload_id
               WHERE f.id = ?""",
            (file_id,),
        ).fetchone()
    if row is None or row["status"] != "approved":
        fail(404, "resource_missing", "资源不存在")
    path = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
    if path.suffix.lower() != ".docx":
        fail(415, "preview_unsupported", "暂不支持该格式在线预览，请下载后打开")

    tmp_path: Path | None = None
    store = None
    try:
        if not path.exists():
            cos_key = (row["object_key"] or "").strip() if cfg.cos_enabled else ""
            if not cos_key:
                fail(404, "resource_missing", "资源文件不存在")
            fd, tmp_name = tempfile.mkstemp(prefix="resource_", suffix=".docx")
            os.close(fd)
            tmp_path = Path(tmp_name)
            store = storage_for(cfg)
            try:
                store.download_file(cos_key, tmp_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("COS 解析下载失败 key=%s: %s", cos_key, exc)
                fail(404, "resource_missing", "资源文件不存在")
            path = tmp_path
        try:
            text, truncated = docx_text(path)
        except ValueError:
            fail(422, "preview_failed", "文档解析失败，请下载后用 Word 打开")
    finally:
        if store is not None:
            store.close()
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return {
        "name": row["name"],
        "format": "docx",
        "text": text,
        "truncated": truncated,
    }


@router.get("/api/v1/resources/files/{file_id}")
def download_resource(
    request: Request,
    file_id: int,
    authorization: str | None = Header(default=None),
    mode: str = Query(default="download"),
    dev: str | None = Query(default=None),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
):
    if mode not in {"download", "preview"}:
        fail(400, "invalid_mode", "下载模式无效")
    device = require_device(request, authorization)
    cfg: Config = config(request)
    # 🔴 文件存在性必须在**事务内、写计数之前**校验（2026-11-01 修）：
    #    原来 path.exists() 写在 `with db()` 之外，而 download_events 插入与
    #    downloads+1 在事务里提交 —— 文件被清理掉之后，用户拿到的是 404，
    #    但当日配额已经扣了、下载数也虚增了一次。
    #    现在把「查行 + 拼路径 + 校验存在」放在事务最前面，
    #    校验不通过时 fail() 抛异常，DB.__exit__ 走 rollback，一个计数都不落。
    cos_key = ""
    edge_path: Path | None = None
    delivery = "local"
    with db(request) as conn:
        row = conn.execute(
            """SELECT f.*, u.status FROM resource_files f
               JOIN resource_uploads u ON u.id = f.upload_id
               WHERE f.id = ?""",
            (file_id,),
        ).fetchone()
        if row is None or row["status"] != "approved":
            fail(404, "resource_missing", "资源不存在")

        path = cfg.upload_dir / str(row["upload_id"]) / row["stored_name"]
        cos_key = (row["object_key"] or "").strip() if cfg.cos_enabled else ""
        if _edge_enabled(cfg):
            edge_path = (Path(cfg.edge_public_dir) / str(row["upload_id"])
                         / row["stored_name"])
        edge_ready = bool(edge_path is not None and edge_path.exists())
        size_bytes = int(row["size"] or 0)
        cos_threshold = int(getattr(cfg, "resource_cos_direct_threshold", 0) or 0)
        cos_direct = bool(
            cos_key and cos_threshold > 0 and size_bytes >= cos_threshold
        )
        # 404 判断必须把 EdgeOne 镜像和 COS 对象都纳入：本地磁盘被清理后，
        # 只要边缘镜像或 COS 对象还在，下载仍然应该成功。COS 不能只看
        # object_key，否则对象被删后会先 307 到预签名地址、再由 COS 返回 404，
        # 后台却记了一次下载；这里在两者都不可用时做一次 HEAD 兜底。
        # 大文件准备直连 COS 时也必须先 HEAD，不能把缺失对象 307 给客户端。
        cos_ready = bool(cos_key)
        if cos_ready and (cos_direct or (not path.exists() and not edge_ready)):
            store_probe = storage_for(cfg)
            try:
                cos_ready = store_probe.object_exists(cos_key)
            except CosError as exc:
                logger.warning("COS HEAD 404 判断失败 key=%s: %s", cos_key, exc)
                cos_ready = False
            finally:
                store_probe.close()
        if not path.exists() and not edge_ready and not cos_ready:
            fail(404, "resource_missing", "资源文件不存在")
        if cos_direct and cos_ready:
            delivery = "cos"
        else:
            delivery = ("edge" if edge_ready
                        else ("cos" if cos_ready else "local"))

        account_id: int | None = None
        if mode == "download":
            today = security.iso()[:10]
            account_id = _device_account_id(conn, int(device["id"]))
            base_limit, bonus_limit, invited_count = _quota_limits(
                conn, cfg, device, account_id=account_id,
            )
            limit = base_limit + bonus_limit
            used = _download_used(conn, int(device["id"]), account_id, today)
            if used >= limit:
                logger.info("下载额度用尽 device=%s account=%s used=%s limit=%s",
                            device["id"], account_id, used, limit)
                scope_note = "账号" if account_id is not None else "设备"
                quota_note = (
                    f"{scope_note}基础额度 {base_limit} 次/天；邀请 1 人每日 +"
                    f"{getattr(cfg, 'resource_invite_bonus_per_invite', 10)} 次"
                    if bonus_limit == 0
                    else f"{scope_note}基础额度 {base_limit} 次/天，已邀请 "
                         f"{invited_count} 人，加成 {bonus_limit} 次"
                )
                fail(
                    429,
                    "download_quota",
                    f"今日下载次数已用完（{quota_note}）",
                )

        conn.execute(
            """INSERT INTO download_events
               (device_id, user_id, file_id, bytes, mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (device["id"], account_id, file_id, row["size"], mode,
             security.iso()),
        )
        if mode == "download":
            conn.execute(
                "UPDATE resource_files SET downloads = downloads + 1 WHERE id = ?",
                (file_id,),
            )
        _bump_metric(conn, "preview" if mode == "preview" else "download")
        _bump_metric(conn, f"delivery_{delivery}")
    if delivery == "edge" and edge_path is not None:
        # EdgeOne Token 鉴权：每次 307 现签一条短时 URL，旧直链不再永久公开。
        edge_url = _edge_signed_url(cfg, row["upload_id"], row["stored_name"])
        return RedirectResponse(edge_url, status_code=307)
    if delivery == "cos" and cos_key:
        store = storage_for(cfg)
        try:
            # COS 兜底：对象桶已设为私有，只下发短时预签名 URL，不再回公开直链。
            return RedirectResponse(
                store.presigned_url(cos_key, expire=cfg.edge_token_ttl),
                status_code=307,
            )
        finally:
            store.close()
    headers = {}
    if (row["sha256"] or "").strip():
        headers["X-Content-SHA256"] = row["sha256"]
    return FileResponse(
        path,
        filename=row["name"],
        headers=headers,
        # 预览（网页版看 PDF）要能在浏览器里直接渲染，下载才用附件。
        content_disposition_type="inline" if mode == "preview" else "attachment",
    )


@router.get("/api/v1/resource-uploads/mine")
def my_resource_uploads(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """当前设备的上传记录与贡献/下载统计。"""
    device = require_device(request, authorization)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT * FROM resource_uploads
               WHERE device_id = ? ORDER BY id DESC LIMIT 200""",
            (device["id"],),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            files = conn.execute(
                """SELECT id, name, size, kind, downloads, parent_id
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
                            "area": f["kind"],
                            "downloads": f["downloads"],
                            "parentId": f["parent_id"],
                        }
                        for f in files
                    ],
                }
            )
        stats = conn.execute(
            """SELECT COUNT(f.id) AS contributions,
                      COALESCE(SUM(f.downloads), 0) AS downloads
               FROM resource_uploads u
               JOIN resource_files f ON f.upload_id = u.id
               WHERE u.device_id = ? AND u.status = 'approved'""",
            (device["id"],),
        ).fetchone()
    return {
        "stats": {
            "contributions": stats["contributions"] if stats else 0,
            "downloads": stats["downloads"] if stats else 0,
        },
        "items": items,
    }


# ---------------------------------------------------------------- 内部

def _risk(conn, kind: str, *, device_id: int | None = None,
          user_id: int | None = None, detail: str = "") -> None:
    conn.execute(
        """INSERT INTO risk_events (kind, device_id, user_id, detail, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (kind, device_id, user_id, detail, security.iso()),
    )


# ---------------------------------------------------------------- 邀请拉新

_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_INVITE_CODE_LEN = 8
_INVITE_SOURCES = {"share", "manual_code", "link"}
_INVITE_ACTIONS = {"course_import", "grade_query"}
_INVITE_BIND_WINDOW = 3600
_INVITE_BIND_MAX = 8
# 「新用户」的圈定窗口：账号在控制端首次登记后多少天内仍算新同学。
# 3 天（2026-11-26 同学确认）：只留给「刚装完就填码」这一种，
# 判据要严 —— 邀请奖励是给真·新同学的，不是给存量用户补领的。
_INVITE_NEW_USER_DAYS = 3
_INVITE_BIND_ATTEMPTS: dict[int, list[float]] = {}
_INVITE_BIND_LOCK = threading.Lock()


def _invite_bind_allowed(user_id: int) -> bool:
    """简单的进程内限流：同一用户每小时最多 8 次填码尝试。

    单进程部署足够；多进程时每个进程各算一份，仍能挡住脚本连续试码。
    """
    now = time.monotonic()
    with _INVITE_BIND_LOCK:
        attempts = [t for t in _INVITE_BIND_ATTEMPTS.get(user_id, [])
                    if now - t < _INVITE_BIND_WINDOW]
        if len(attempts) >= _INVITE_BIND_MAX:
            _INVITE_BIND_ATTEMPTS[user_id] = attempts
            return False
        attempts.append(now)
        _INVITE_BIND_ATTEMPTS[user_id] = attempts
        return True


def _new_invite_code(conn) -> str:
    """生成 8 位去混淆邀请码；撞库重试几次后用 token 兜底。"""
    for _ in range(5):
        code = "".join(
            secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_CODE_LEN)
        )
        if conn.execute(
            "SELECT 1 FROM invite_codes WHERE code = ?", (code,)
        ).fetchone() is None:
            return code
    # 兜底也必须落在去混淆字母表内，不能再从 base64 取字符带出 I/L/O/0/1。
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_CODE_LEN))


@router.get("/api/v1/invite/config")
def invite_config(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """活动/彩蛋开关；设备鉴权可选，客户端在拉取失败时按保守默认处理。"""
    cfg: Config = config(request)
    return {
        "active": cfg.invite_active,
        "egg_enabled": cfg.egg_enabled,
        "actions": sorted(_INVITE_ACTIONS),
    }


@router.get("/api/v1/invite/me")
def invite_me(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """我的邀请码、统计与脱敏名单。按用户幂等发放邀请码。"""
    sess = require_session(request, authorization)
    user_id = sess["user_id"]
    stamp = security.iso()
    cfg = config(request)

    with db(request) as conn:
        code_row = conn.execute(
            "SELECT * FROM invite_codes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if code_row is None:
            inserted = False
            for _ in range(5):
                code = _new_invite_code(conn)
                try:
                    conn.execute(
                        """INSERT INTO invite_codes
                           (code, user_id, device_id, status, used_count, created_at)
                           VALUES (?, ?, ?, 'active', 0, ?)""",
                        (code, user_id, sess.get("device_id"), stamp),
                    )
                    inserted = True
                    break
                except sqlite3.IntegrityError:
                    # 主键撞码：换一个码重试；user_id 唯一冲突说明并发已建码：
                    # 回退重查该用户的码，不能把未写入的 code 直接返回。
                    exist = conn.execute(
                        "SELECT * FROM invite_codes WHERE user_id = ?", (user_id,)
                    ).fetchone()
                    if exist is not None:
                        code = exist["code"]
                        code_row = exist
                        inserted = True
                        break
            if not inserted:
                fail(500, "invite_code_failed", "邀请码生成失败，请稍后重试")
        elif code_row["status"] != "active":
            # 历史被移除的码复活复用，避免 user_id 唯一约束冲突。
            conn.execute(
                "UPDATE invite_codes SET status = 'active', device_id = ? WHERE user_id = ?",
                (sess.get("device_id"), user_id),
            )
            code = code_row["code"]
        else:
            code = code_row["code"]

        rel_rows = conn.execute(
            """SELECT r.id, r.status, r.bound_at, r.activated_at
               FROM invite_relations r
               WHERE r.inviter_user_id = ?
               ORDER BY r.bound_at DESC, r.id DESC""",
            (user_id,),
        ).fetchall()

        total = sum(1 for r in rel_rows if r["status"] in ("pending", "activated"))
        activated = sum(1 for r in rel_rows if r["status"] == "activated")

        # 名单只给稳定别名，不回伪名/学号/姓名等任何身份信息。
        # 用关系 id 生成别名：新增邀请不会让旧同学的「同学 N」全部重排。
        relations: list[dict[str, Any]] = []
        for r in rel_rows:
            alias = f"同学 {(int(r['id']) * 73) % 997 + 1}"
            relations.append({
                "id": r["id"],
                "status": r["status"],
                "masked_label": alias,
                "activated_at": r["activated_at"],
            })

    base = str(request.base_url).rstrip("/")
    per_invite = max(0, int(getattr(
        cfg, "resource_invite_bonus_per_invite", 10
    )))
    # 与下载额度共用同一个封顶值，避免邀请页展示的奖励大于实际可用额度。
    bonus_cap = max(0, int(getattr(cfg, "resource_invite_bonus_limit", 1000)))
    download_bonus = activated * per_invite
    if bonus_cap > 0 and download_bonus > bonus_cap:
        download_bonus = bonus_cap
    return {
        "available": True,
        "code": code,
        "share_url": f"{base}/invite?c={code}",
        "total_invited": total,
        "activated_count": activated,
        "download_bonus": download_bonus,
        "download_bonus_per_invite": per_invite,
        "relations": relations,
    }


@router.post("/api/v1/invite/bind")
async def invite_bind(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """新用户回填邀请码：校验通过即建立并激活关系（填码直接生效）。"""
    sess = require_session(request, authorization)
    invitee_id = sess["user_id"]
    invitee_device = sess.get("device_id")

    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")

    code = str(body.get("code") or "").strip().upper()
    source = str(body.get("source") or "manual_code").strip()[:24]
    app_version = str(body.get("app_version") or "").strip()[:32]
    platform = str(body.get("platform") or "").strip()[:16]
    if source not in _INVITE_SOURCES:
        source = "manual_code"

    def reject(reason: str, message: str) -> dict[str, Any]:
        return {"ok": False, "status": "invalid", "reason": reason, "message": message}

    if not _invite_bind_allowed(invitee_id):
        return reject("rate_limited", "操作过于频繁，请稍后再试")

    code = code.strip()
    if (not code or len(code) != _INVITE_CODE_LEN
            or any(ch not in _INVITE_ALPHABET for ch in code)):
        return reject("invalid_code", "邀请码无效，请核对后重试")

    stamp = security.iso()
    with db(request) as conn:
        existing = conn.execute(
            "SELECT id, status FROM invite_relations WHERE invitee_user_id = ?",
            (invitee_id,),
        ).fetchone()
        if existing is not None:
            _risk(conn, "invite_duplicate", user_id=invitee_id, detail="重复绑定邀请关系")
            return reject("duplicate", "该账号已经绑定过邀请关系")

        code_row = conn.execute(
            "SELECT * FROM invite_codes WHERE code = ? AND status = 'active'",
            (code,),
        ).fetchone()
        if code_row is None:
            _risk(conn, "invite_bad_code", user_id=invitee_id,
                  detail=f"无效邀请码 {code[:4]}***")
            return reject("invalid_code", "邀请码无效，请核对后重试")

        # 过期码不再可用。`expires_at` 为空表示长期有效。
        expires_at = code_row["expires_at"]
        if expires_at:
            try:
                if security.now() > security.parse_iso(expires_at):
                    return reject("invalid_code", "邀请码已过期，请向同学重新索取")
            except ValueError:
                # 坏数据按过期处理，宁可拒绝也不放行。
                return reject("invalid_code", "邀请码已失效，请向同学重新索取")

        inviter_id = code_row["user_id"]
        if inviter_id == invitee_id:
            _risk(conn, "invite_self", user_id=invitee_id, detail="填写自己的邀请码")
            return reject("self_invite", "不能填写自己的邀请码")

        # 邀请人账号必须有效：封禁用户的码不应再带来新关系。
        inviter = conn.execute(
            "SELECT status FROM users WHERE id = ?", (inviter_id,)
        ).fetchone()
        if inviter is None or inviter["status"] != "active":
            return reject("invalid_code", "邀请码无效，请核对后重试")

        # 「新用户」怎么圈定（2026-11-26 调整）
        #
        # 判据 = 账号在控制端**首次登记**的时间在 _INVITE_NEW_USER_DAYS 天内，
        #        且不是**同一台设备**上的另一个号。
        #
        # 窗口最终定在 3 天（2026-11-26 同学确认改回短窗口）：
        # 邀请奖励只给「刚装完就填码」的真·新同学，存量用户补填不算。
        # 中途试过 7 天 / 30 天，结论是「宁可少算，也不要把老账号算成新用户」。
        #
        # 防刷不靠把窗口压小（那只会误伤真用户），靠四道闸门：
        #   ① 每个被邀请账号终身只能有一条关系（uq_invite_rel_invitee 唯一索引）；
        #   ② 不能填自己的码（inviter_id == invitee_id）；
        #   ③ 同一台设备上的两个号不能互邀（同机双开自导自演）；
        #   ④ 每小时最多 8 次填码尝试（_invite_bind_allowed）。
        user_row = conn.execute(
            "SELECT created_at FROM users WHERE id = ?", (invitee_id,)
        ).fetchone()
        session_n = conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE user_id = ? AND revoked_at IS NULL",
            (invitee_id,),
        ).fetchone()["c"]
        created = security.parse_iso(user_row["created_at"]) if user_row else None
        window_days = _INVITE_NEW_USER_DAYS
        age_days = None
        if created is not None:
            age_days = (security.now() - created).total_seconds() / 86400.0
        is_old = age_days is not None and age_days > window_days
        if is_old:
            _risk(conn, "invite_old_user", user_id=invitee_id,
                  detail=f"账号登记 {age_days:.0f} 天，超过 {window_days} 天窗口"
                         f"（会话 {session_n}）")
            return reject(
                "old_user",
                f"这个账号注册已经超过 {window_days} 天了，只能算老用户。"
                f"邀请奖励只给刚安装就填码的新同学。",
            )

        # 同一台设备上的两个号不能互相刷邀请（双开 / 手机分身场景）。
        inviter_device = code_row["device_id"]
        if invitee_device and inviter_device:
            try:
                same_device = int(invitee_device) == int(inviter_device)
            except (TypeError, ValueError):
                same_device = False
            if same_device:
                _risk(conn, "invite_same_device", user_id=invitee_id,
                      detail=f"邀请人与被邀请人同设备（{invitee_device}）")
                return reject(
                    "same_device",
                    "同一台手机上切换账号不算邀请，换台设备再试。",
                )

        try:
            cur = conn.execute(
                """INSERT INTO invite_relations
                   (code, inviter_user_id, invitee_user_id, invitee_device_id, source,
                    status, bound_at, activated_at, reward_state, created_at)
                   VALUES (?, ?, ?, ?, ?, 'activated', ?, ?, 'none', ?)""",
                (code, inviter_id, invitee_id, invitee_device, source,
                 stamp, stamp, stamp),
            )
        except sqlite3.IntegrityError:
            # 并发：另一路已为该账号建立关系（invitee 唯一索引），按重复绑定处理。
            _risk(conn, "invite_duplicate", user_id=invitee_id, detail="并发重复绑定邀请关系")
            return reject("duplicate", "该账号已经绑定过邀请关系")
        relation_id = cur.lastrowid
        conn.execute(
            "UPDATE invite_codes SET used_count = used_count + 1 WHERE code = ?",
            (code,),
        )
        conn.execute(
            """INSERT INTO invite_events (relation_id, event_type, meta, created_at)
               VALUES (?, 'bound', ?, ?)""",
            (relation_id, json.dumps(
                {"source": source, "app_version": app_version, "platform": platform},
                ensure_ascii=False), stamp),
        )
        # 填码即激活：单独记一条 activated 事件，便于区分「绑定动作」与「生效」。
        conn.execute(
            """INSERT INTO invite_events (relation_id, event_type, meta, created_at)
               VALUES (?, 'activated', ?, ?)""",
            (relation_id, json.dumps({"action": "bind"}, ensure_ascii=False), stamp),
        )

    return {"ok": True, "status": "activated", "message": "邀请码已生效，已计入邀请"}


@router.post("/api/v1/invite/activate")
async def invite_activate(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """被邀请人完成课表导入 / 成绩查询后幂等上报激活；无关系则静默跳过。"""
    sess = require_session(request, authorization)
    invitee_id = sess["user_id"]

    try:
        body = await request.json()
    except Exception:
        body = {}
    action = str(body.get("action") or "").strip()[:24]
    if action not in _INVITE_ACTIONS:
        action = "course_import"

    stamp = security.iso()
    with db(request) as conn:
        rel = conn.execute(
            """SELECT * FROM invite_relations
               WHERE invitee_user_id = ? AND status IN ('pending', 'activated')
               ORDER BY id DESC LIMIT 1""",
            (invitee_id,),
        ).fetchone()
        if rel is None:
            return {"ok": True, "activated": False}
        if rel["status"] == "activated":
            return {"ok": True, "activated": True, "already": True}

        conn.execute(
            "UPDATE invite_relations SET status = 'activated', activated_at = ? WHERE id = ?",
            (stamp, rel["id"]),
        )
        conn.execute(
            """INSERT INTO invite_events (relation_id, event_type, meta, created_at)
               VALUES (?, 'activated', ?, ?)""",
            (rel["id"], json.dumps({"action": action}, ensure_ascii=False), stamp),
        )

    return {"ok": True, "activated": True}


@router.post("/api/v1/app/rating")
async def submit_app_rating(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """课表页五星体验评分：1-5 分，按用户+版本幂等写入。

    只把内部 user_id/device_id 用于去重与幂等；管理端聚合只输出版本/平台/
    平均分/分布，不展示学号、姓名。
    """
    sess = require_session(request, authorization)

    try:
        body = await request.json()
    except Exception:
        fail(400, "invalid_json", "请求体不是合法 JSON")

    try:
        rating = int(body.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if rating < 1 or rating > 5:
        fail(400, "invalid_rating", "评分需在 1-5 之间")

    app_version = str(body.get("app_version") or "").strip()[:32]
    platform = str(body.get("platform") or "").strip()[:16]
    stamp = security.iso()
    with db(request) as conn:
        # 同一用户 + 同一版本 + 同一平台只保留最后一次评分：
        # 重复提交（重试、旧客户端补报）不会把平均分刷高。
        conn.execute(
            """INSERT INTO app_ratings
               (user_id, device_id, rating, app_version, platform, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, app_version, platform) DO UPDATE SET
                 rating = excluded.rating,
                 device_id = excluded.device_id,
                 created_at = excluded.created_at""",
            (sess["user_id"], sess.get("device_id"), rating,
             app_version, platform, stamp),
        )

    return {"ok": True}
