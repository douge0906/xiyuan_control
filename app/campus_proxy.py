"""Web 端有状态 CAS 会话代理（一期）。

## 为什么需要它

浏览器（Flutter Web / PWA）里 `dart:io HttpClient` 不可用，而安卓端的教务会话
依赖逐跳跟 302、回传 `Set-Cookie`、按学号持有 cookie 罐。无状态的 `webproxy.py`
每请求清空 cookie，串不起 CAS 登录链。本模块在**服务端内存**里按学号持有一套
CAS/教务 cookie 罐，浏览器交账密、服务端代跑 CAS 登录与查询，解析后的数据回传。

## 与 `webproxy.py` 的边界（硬规矩）

1. **独立模块、独立前缀** `/api/v1/campus`，不 import 改写 webproxy 的任何行为；
   白名单 / 防内网 / 超时 / 响应体上限 / 限流等安全约束直接复用其只读工具函数。
2. **会话按学号隔离**：每个会话绑定归属人，cookie 只发给本会话；不跨用户串台。
3. **凭据不落盘、不进日志**：密码仅在「待提交验证码」阶段留在内存，登录结束后
   不再保存；TGT / cookie 只存内存，进程重启即失效。
4. **一期独立会话，不与 App 设备私钥 token 打通**：会话凭证是本模块自签的随机
   `session_id`，不复用、不互认 App 的设备令牌。
5. **验证码人工回填**：服务端不做本地验证码识别；`/session/start` 取图回传浏览器，
   用户识别后由 `/session/submit` 回填。

## 未覆盖（一期）

- 融合门户（一卡通余额）、实验课 sygl 与教务是独立子系统，cookie 不通用；本模块
  一期先打通教务会话（课表 / 成绩 / 考试 / 学业）。门户 / sygl 会话另立。
- 会话仅存内存，多实例部署不共享；一期单实例足够。
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urljoin

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import webproxy

# httpx 可选依赖：缺失时应用照常启动，仅本模块端点返回 503（同 webproxy 策略）。
try:  # noqa: SIM105
    import httpx  # type: ignore
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore[assignment]

router = APIRouter()

# ---------------------------------------------------------------- 常量

CAS_BASE = "https://wxcas.cwxu.edu.cn"
JWGL_BASE = "https://jwgl.cwxu.edu.cn"
JWGL_SERVICE = f"{JWGL_BASE}/sso/lyiotlogin"
PORTAL_BASE = "https://my.cwxu.edu.cn"
PORTAL_SERVICE = f"{PORTAL_BASE}/shiro-cas"

# CAS RSA 公钥（与安卓 cas_client.dart 硬编码一致；无填充加密）。
_RSA_MODULUS_HEX = (
    "00b5eeb166e069920e80bebd1fea4829d3d1f3216f2aabe79b6c47a3c18dcee5"
    "fd22c2e7ac519cab59198ece036dcf289ea8201e2a0b9ded307f8fb704136eaeb"
    "670286f5ad44e691005ba9ea5af04ada5367cd724b5a26fdb5120cc95b6431604"
    "bd219c6b7d83a6f8f24b43918ea988a76f93c333aa5a20991493d4eb1117e7b1"
)
_RSA_EXPONENT = 65537

#: 会话有效期（秒）。对齐安卓本地会话缓存的 20 分钟保守口径。
SESSION_TTL = 20 * 60
#: 待验证码阶段的有效期（未提交即丢弃凭据）。
PENDING_TTL = 5 * 60
#: 单会话并发上限：同一学号只保留最新一条会话。
MAX_PER_USER = 1

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 会话存储


@dataclass
class _Session:
    sid: str
    username: str
    service: str
    stage: str  # "pending" | "ready"
    cookies: dict[str, str] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)
    last: float = field(default_factory=time.monotonic)
    # 仅 pending 阶段持有：已加密密码、验证码 uid。
    encrypted_password: str = ""
    captcha_uid: str = ""


_sessions: dict[str, _Session] = {}
_by_user: dict[str, str] = {}
_lock = asyncio.Lock()


def _now() -> float:
    return time.monotonic()


def _capture_cookies(headers: Any, jar: dict[str, str]) -> None:
    """从 Set-Cookie 逐值捕获首段 key=value（复刻安卓 CampusSession 逻辑）。"""
    for raw in headers.get_list("set-cookie"):
        first = raw.split(";")[0].strip()
        eq = first.find("=")
        if eq <= 0:
            continue
        k = first[:eq].strip()
        v = first[eq + 1 :].strip()
        if not v or v.lower() == "deleted":
            jar.pop(k, None)
        else:
            jar[k] = v


def _cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _purge_expired() -> None:
    now = _now()
    stale = [
        sid
        for sid, s in _sessions.items()
        if now - s.last > (PENDING_TTL if s.stage == "pending" else SESSION_TTL)
    ]
    for sid in stale:
        s = _sessions.pop(sid, None)
        if s and _by_user.get(s.username) == sid:
            _by_user.pop(s.username, None)


def _rsa_encrypt(password: str) -> str:
    """RSA 无填充加密（小端拼整数，与安卓 CasClient.rawRsaEncrypt 一致）。"""
    n = int(_RSA_MODULUS_HEX, 16)
    m = int.from_bytes(password.encode("utf-8"), "little")
    return format(pow(m, _RSA_EXPONENT, n), "x").lstrip("0")


def _new_client() -> httpx.AsyncClient:
    # follow_redirects=False：CAS 会话藏在中间 302 的 Set-Cookie 里，必须逐跳可见。
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(webproxy.REQUEST_TIMEOUT, connect=10.0),
        verify=True,
        http2=False,
        headers={"User-Agent": _UA},
    )


# ---------------------------------------------------------------- 内部流程


async def _fetch_captcha(client: httpx.AsyncClient) -> tuple[str, str]:
    """取验证码，返回 (uid, 图片 base64)。失败抛 ValueError。"""
    resp = await client.get(
        f"{CAS_BASE}/lyuapServer/kaptcha",
        headers={"Referer": f"{CAS_BASE}/lyuapServer/login", "Origin": CAS_BASE},
    )
    data = resp.json()
    uid = str(data.get("uid") or "")
    content = str(data.get("content") or "")
    if not uid or not content:
        raise ValueError("验证码接口返回异常")
    b64 = content.split(",")[-1] if "," in content else content
    return uid, b64


async def _submit_ticket(
    client: httpx.AsyncClient,
    s: _Session,
    code: str,
) -> dict[str, Any]:
    resp = await client.post(
        f"{CAS_BASE}/lyuapServer/v1/tickets",
        data={
            "username": s.username,
            "password": s.encrypted_password,
            "service": s.service,
            "loginType": "",
            "id": s.captcha_uid,
            "code": code,
            "otpcode": "",
        },
        headers={
            "Referer": f"{CAS_BASE}/lyuapServer/login",
            "Origin": CAS_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


async def _sso_to(client: httpx.AsyncClient, s: _Session, tgt: str) -> bool:
    """用 TGT 换 ST 并访问 service 建立子系统会话；成功返回 True。"""
    st_resp = await client.post(
        f"{CAS_BASE}/lyuapServer/v1/tickets/{tgt}",
        data={"service": s.service},
        headers={"Referer": f"{CAS_BASE}/"},
    )
    st = st_resp.text.strip()
    try:
        j = st_resp.json()
        if isinstance(j, dict) and isinstance(j.get("ticket"), str):
            st = j["ticket"]
    except Exception:  # noqa: BLE001
        pass
    if not st:
        return False

    sep = "&" if "?" in s.service else "?"
    url = f"{s.service}{sep}ticket={st}"
    # 逐跳跟随落地跳转（最多 6 跳），每跳捕获 cookie。
    for _ in range(6):
        resp = await client.get(url, headers={"Referer": f"{CAS_BASE}/"})
        _capture_cookies(resp.headers, s.cookies)
        location = resp.headers.get("location")
        if resp.status_code >= 300 and resp.status_code < 400 and location:
            url = urljoin(url, location)
            continue
        # 落地页又被弹回登录页 → 会话没建起来。
        if not resp.is_success or "login" in str(resp.url).lower():
            return False
        return True
    return False


# ---------------------------------------------------------------- 端点


@router.get("/api/v1/campus/ping")
async def campus_ping() -> dict[str, Any]:
    """探活：供前端确认代理可用。"""
    return {
        "ok": httpx is not None,
        "suffixes": list(webproxy.ALLOWED_SUFFIXES),
        "httpx": getattr(httpx, "__version__", None) if httpx else None,
    }


@router.post("/api/v1/campus/session/start")
async def session_start(request: Request) -> Any:
    """第一步：校验入参、加密密码、取验证码图，建立 pending 会话。

    请求体：``{"username": "...", "password": "...", "service": "可选"}``
    响应：``{"session_id": "...", "captcha_b64": "..."}``
    """
    if httpx is None:
        return webproxy._fail(503, "proxy_unavailable", "服务器缺少 httpx，网页版教务暂不可用")

    ip = request.client.host if request.client else "-"
    if not await webproxy._rate_ok(ip):
        return webproxy._fail(429, "rate_limited", "请求过于频繁，请稍后重试")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return webproxy._fail(400, "bad_json", "请求体不是合法 JSON")
    if not isinstance(payload, dict):
        return webproxy._fail(400, "bad_json", "请求体必须是 JSON 对象")

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    service = str(payload.get("service") or JWGL_SERVICE).strip()
    if not username or not password:
        return webproxy._fail(400, "missing_credential", "缺少学号或密码")

    host = webproxy._host_of(service)
    if host is None or not webproxy._host_allowed(host):
        return webproxy._fail(403, "host_not_allowed", "不允许的子系统入口")

    encrypted = _rsa_encrypt(password)
    sid = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    sess = _Session(
        sid=sid,
        username=username,
        service=service,
        stage="pending",
        encrypted_password=encrypted,
    )

    client = _new_client()
    try:
        uid, captcha_b64 = await _fetch_captcha(client)
    except httpx.TimeoutException:
        return webproxy._fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
    except Exception:  # noqa: BLE001
        return webproxy._fail(502, "upstream_error", "获取验证码失败，请稍后重试")
    finally:
        await client.aclose()

    sess.captcha_uid = uid
    async with _lock:
        _purge_expired()
        # 同一学号挤掉旧会话（含未完成的 pending）。
        old = _by_user.get(username)
        if old:
            _sessions.pop(old, None)
        _sessions[sid] = sess
        _by_user[username] = sid

    return {"session_id": sid, "captcha_b64": captcha_b64}


@router.post("/api/v1/campus/session/submit")
async def session_submit(request: Request) -> Any:
    """第二步：回填验证码，完成 CAS 登录并建立教务会话。

    请求体：``{"session_id": "...", "code": "..."}``
    """
    if httpx is None:
        return webproxy._fail(503, "proxy_unavailable", "服务器缺少 httpx，网页版教务暂不可用")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return webproxy._fail(400, "bad_json", "请求体不是合法 JSON")
    if not isinstance(payload, dict):
        return webproxy._fail(400, "bad_json", "请求体必须是 JSON 对象")

    sid = str(payload.get("session_id") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not sid or not code:
        return webproxy._fail(400, "missing_field", "缺少 session_id 或验证码")

    async with _lock:
        _purge_expired()
        sess = _sessions.get(sid)
        if sess is None or sess.stage != "pending":
            return webproxy._fail(401, "session_expired", "会话已过期，请重新登录")

    client = _new_client()
    try:
        data = await _submit_ticket(client, sess, code)
    except httpx.TimeoutException:
        return webproxy._fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
    except Exception:  # noqa: BLE001
        return webproxy._fail(502, "upstream_error", "提交登录失败，请稍后重试")

    tgt = data.get("tgt")
    if isinstance(tgt, str) and tgt:
        try:
            ready = await _sso_to(client, sess, tgt)
        except Exception:  # noqa: BLE001
            ready = False
        if not ready:
            return webproxy._fail(502, "sso_failed", "换取教务会话失败，请稍后重试")
        sess.stage = "ready"
        sess.last = _now()
        # 登录成功后不再保留加密密码。
        sess.encrypted_password = ""
        sess.captcha_uid = ""
        return {"ok": True, "note": "登录成功"}

    data_block = data.get("data")
    code_str = data_block.get("code", "") if isinstance(data_block, dict) else ""
    msg = (
        data_block.get("msg") or data_block.get("message") or ""
        if isinstance(data_block, dict)
        else ""
    )
    if code_str == "PASSERROR":
        return webproxy._fail(401, "bad_credential", "学号或密码不正确")
    if _looks_locked(f"{code_str} {msg}"):
        return webproxy._fail(423, "locked", "账号已被临时锁定，请稍后再试")
    return webproxy._fail(400, "captcha_failed", "验证码错误，请换一张重试")


@router.post("/api/v1/campus/session/fetch")
async def session_fetch(request: Request) -> Any:
    """带本会话 cookie 代取教务资源（无状态隧道之上的有状态代抓）。

    请求体：``{"session_id": "...", "url": "...", "method": "GET",
    "headers": {...}, "body_b64": null}``
    响应结构对齐 webproxy：``status / url / headers / body_b64 / truncated``。
    """
    if httpx is None:
        return webproxy._fail(503, "proxy_unavailable", "服务器缺少 httpx，网页版教务暂不可用")

    ip = request.client.host if request.client else "-"
    if not await webproxy._rate_ok(ip):
        return webproxy._fail(429, "rate_limited", "请求过于频繁，请稍后重试")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return webproxy._fail(400, "bad_json", "请求体不是合法 JSON")
    if not isinstance(payload, dict):
        return webproxy._fail(400, "bad_json", "请求体必须是 JSON 对象")

    sid = str(payload.get("session_id") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not sid or not url:
        return webproxy._fail(400, "missing_field", "缺少 session_id 或 url")

    async with _lock:
        _purge_expired()
        sess = _sessions.get(sid)
        if sess is None or sess.stage != "ready":
            return webproxy._fail(401, "session_expired", "登录已过期，请重新登录")
        sess.last = _now()
        jar = dict(sess.cookies)

    host = webproxy._host_of(url)
    if host is None:
        return webproxy._fail(400, "bad_url", "只支持 http/https 地址")
    if not webproxy._host_allowed(host):
        return webproxy._fail(403, "host_not_allowed", f"不允许代理该站点：{host}")
    if await webproxy._resolves_to_private(host):
        return webproxy._fail(403, "private_address", f"该域名解析到内网地址，已拒绝：{host}")

    method = str(payload.get("method") or "GET").upper()
    if method not in ("GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"):
        return webproxy._fail(400, "bad_method", f"不支持的方法：{method}")

    raw_headers = payload.get("headers")
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            name = str(key).lower().strip()
            if name in webproxy.ALLOWED_REQUEST_HEADERS and value is not None:
                headers[name] = str(value)
    if jar:
        headers["cookie"] = _cookie_header(jar)

    body: bytes | None = None
    body_b64 = payload.get("body_b64")
    if isinstance(body_b64, str) and body_b64:
        try:
            body = base64.b64decode(body_b64, validate=False)
        except Exception:  # noqa: BLE001
            return webproxy._fail(400, "bad_body", "body_b64 不是合法 base64")

    client = _new_client()
    try:
        # 逐跳跟随 302（对齐安卓 CampusSession：会话 cookie 藏在中间跳转里），
        # 每跳捕获 Set-Cookie 回写本地 jar，落地后再写回服务端会话罐。
        hop_url = url
        hop_method = method
        hop_body = body
        resp = None
        for hop in range(6):
            hop_headers = dict(headers)
            if jar:
                hop_headers["cookie"] = _cookie_header(jar)
            resp = await client.request(
                hop_method,
                hop_url,
                headers=hop_headers,
                content=hop_body,
            )
            _capture_cookies(resp.headers, jar)
            location = resp.headers.get("location")
            if (
                hop < 5
                and resp.status_code >= 300
                and resp.status_code < 400
                and location
            ):
                hop_url = urljoin(hop_url, location)
                hop_method = "GET"
                hop_body = None
                continue
            break

        assert resp is not None
        raw = await resp.aread()
        truncated = len(raw) > webproxy.MAX_BODY_BYTES
        body_out = raw[: webproxy.MAX_BODY_BYTES]

        out_headers: dict[str, list[str]] = {}
        for name in webproxy.ALLOWED_RESPONSE_HEADERS:
            if name == "set-cookie":
                values = resp.headers.get_list("set-cookie")
            else:
                values = resp.headers.get_list(name)
            if values:
                out_headers[name] = list(values)

        # 落地后把本轮捕获到的新 cookie 写回服务端会话（会话续期 / 刷新）。
        async with _lock:
            target = _sessions.get(sid)
            if target is not None and target.stage == "ready":
                target.cookies.update(jar)
                target.last = _now()

        return {
            "status": resp.status_code,
            "url": str(resp.url),
            "headers": out_headers,
            "body_b64": base64.b64encode(body_out).decode("ascii"),
            "truncated": truncated,
        }
    except httpx.TimeoutException:
        return webproxy._fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
    except httpx.HTTPError as exc:
        return webproxy._fail(502, "upstream_error", f"访问学校系统失败：{type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        return webproxy._fail(502, "upstream_error", f"代理失败：{type(exc).__name__}")
    finally:
        await client.aclose()


@router.post("/api/v1/campus/session/logout")
async def session_logout(request: Request) -> Any:
    """注销本端会话（不调学校 CAS 登出，只清服务端内存罐）。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    sid = str(payload.get("session_id") or "").strip() if isinstance(payload, dict) else ""
    async with _lock:
        sess = _sessions.pop(sid, None)
        if sess and _by_user.get(sess.username) == sid:
            _by_user.pop(sess.username, None)
    return {"ok": True}


def _looks_locked(text: str) -> bool:
    s = text.lower()
    return (
        "lock" in s
        or "锁定" in text
        or "次数过多" in text
        or "尝试次数" in text
        or "频繁" in text
    )
