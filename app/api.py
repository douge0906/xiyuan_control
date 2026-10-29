"""公开 API：设备登记、会话、内容下发、遥测。

三条硬规矩：
1. 不碰教务凭据 —— 只接收客户端 OA 登录后提交的学号与伪名，不读 Cookie/Ticket
2. 任何接口失败都返回统一结构 {error:{code,message}, request_id}
3. 设备与会话令牌在库里只存 HMAC
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from . import content, security
from .config import Config
from .db import DB

router = APIRouter()


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
            if row["status"] != "active":
                fail(403, "device_banned", "设备已被封禁")
            if row["public_key"] != public_key:
                fail(409, "device_key_conflict", "同一安装标识换了密钥，请重装应用")
            conn.execute(
                "UPDATE devices SET platform = ?, app_version = ?, last_seen = ? WHERE id = ?",
                (platform or row["platform"], app_version or row["app_version"], stamp, row["id"]),
            )
            device = row

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
    alias = str(body.get("student_alias") or "")
    display_name = str(body.get("display_name") or "")[:32]
    asserted_at = str(body.get("asserted_at") or "")
    signature = str(body.get("signature") or "")

    if not student_id or len(student_id) > 32:
        fail(400, "invalid_student_id", "学号无效")
    if not signature:
        fail(400, "missing_signature", "缺少签名")

    asserted = security.parse_iso(asserted_at)
    if asserted is None or abs((security.now() - asserted).total_seconds()) > cfg.challenge_ttl:
        fail(400, "assertion_expired", "断言时间无效或已过期")

    expected_alias = security.student_alias(student_id)
    if alias and alias != expected_alias:
        fail(400, "invalid_alias", "伪名与学号不匹配")

    payload = security.login_payload(
        challenge_id, challenge, device["device_serial"], student_id,
        expected_alias, display_name, asserted_at,
    )
    if not security.verify_signature(device["public_key"], payload, signature):
        fail(400, "invalid_signature", "登录断言验签失败")

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

        s_hash = security.student_hash(student_id)
        user = conn.execute("SELECT * FROM users WHERE student_hash = ?", (s_hash,)).fetchone()
        if user is None:
            conn.execute(
                """INSERT INTO users (student_hash, student_alias, display_name,
                                      status, created_at, last_login_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (s_hash, expected_alias, display_name, stamp, stamp),
            )
            user = conn.execute(
                "SELECT * FROM users WHERE student_hash = ?", (s_hash,)
            ).fetchone()
        else:
            if user["status"] != "active":
                fail(403, "user_banned", "账号已被停用")
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
        "user": {"alias": expected_alias, "display_name": display_name},
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
        "downloadUrl": data.get("downloadUrl", ""),
        "notes": data.get("notes", ""),
    }


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
            "downloadUrl": release.get("downloadUrl", ""),
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
    return {"ok": True, "id": feedback_id, "time": stamp}


# ---------------------------------------------------------------- 资源上传

_RESOURCE_AREAS = {"textbook", "exam", "exam_paper", "answer"}
_RESOURCE_EXT = {
    "textbook": {".pdf"},
    "exam_paper": {".pdf", ".doc", ".docx"},
    "answer": {".pdf", ".doc", ".docx"},
}
_MAX_FILE_BYTES = 80 * 1024 * 1024
_MAX_FILES = 10
_MAX_ANSWER_FILES = 10


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
    if not course or len(course) > 256 or "\n" in course or "\r" in course:
        fail(400, "invalid_course", "课程名称无效")
    if len(book) > 256 or "\n" in book or "\r" in book:
        fail(400, "invalid_book", "教材名称无效")
    if area not in _RESOURCE_AREAS:
        fail(400, "invalid_area", "上传区域无效")
    if len(note) > 500:
        fail(400, "note_too_long", "备注最多 500 字")

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

    stamp = security.iso()
    with db(request) as conn:
        cur = conn.execute(
            """INSERT INTO resource_uploads
               (device_id, course_name, book_name, area, note, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (device["id"], course, book, area, note, stamp),
        )
        upload_id = cur.lastrowid

    folder = cfg.upload_dir / str(upload_id)
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[tuple[str, str, int, str]] = []
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
                with dest.open("wb") as out:
                    while True:
                        chunk = await item.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _MAX_FILE_BYTES:
                            raise ValueError("文件超过 80 MB")
                        out.write(chunk)
                if size <= 0:
                    raise ValueError("文件为空")
                saved.append((original, stored, size, kind))
                await item.close()

        if not saved:
            raise ValueError("没有可保存的文件")

        with db(request) as conn:
            for original, stored, size, kind in saved:
                conn.execute(
                    """INSERT INTO resource_files
                       (upload_id, name, stored_name, size, kind)
                       VALUES (?, ?, ?, ?, ?)""",
                    (upload_id, original, stored, size, kind),
                )
    except HTTPException:
        _cleanup_resource_upload(request, folder, upload_id)
        raise
    except Exception:
        _cleanup_resource_upload(request, folder, upload_id)
        fail(400, "upload_failed", "文件保存失败，请检查大小与格式")

    return {"ok": True, "id": upload_id, "status": "pending", "time": stamp}


@router.get("/api/v1/resources")
def list_resources(request: Request) -> dict[str, Any]:
    """审核通过后的共享资源，按课程 + 教材分组。"""
    with db(request) as conn:
        rows = conn.execute(
            """SELECT u.id AS upload_id, u.course_name, u.book_name,
                      u.reviewed_at, u.created_at,
                      f.id AS file_id, f.name, f.size, f.kind, f.downloads
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
                "name": row["name"],
                "area": row["kind"],
                "size": row["size"],
                "downloads": row["downloads"],
                "createdAt": stamp,
                "downloadUrl": f"/api/v1/resources/files/{row['file_id']}",
            }
        )
    return {"courses": list(courses.values())}


@router.get("/api/v1/resources/files/{file_id}")
def download_resource(request: Request, file_id: int):
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
    if not path.exists():
        fail(404, "resource_missing", "资源文件不存在")
    with db(request) as conn:
        conn.execute(
            "UPDATE resource_files SET downloads = downloads + 1 WHERE id = ?",
            (file_id,),
        )
    return FileResponse(path, filename=row["name"])


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
                            "area": f["kind"],
                            "downloads": f["downloads"],
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
