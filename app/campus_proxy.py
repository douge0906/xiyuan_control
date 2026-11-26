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
5. **验证码服务端自动识别**：与安卓端同一套模板匹配（`captcha_tpl.py`），
   一次 `/session/start` 内最多自动试 3 张图，识别成功就直接登录并返回
   `{"ok": true}`，用户看不到验证码；三轮都识别不出来才回退成人工回填
   `captcha_b64`（行为与改造前一致）。

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
from .captcha_solver import solve as _captcha_solve

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

#: 会话有效期（秒）：滑动续期。浏览器端会话凭据只存 sid，罐留在服务端，
#: 因此可以比安卓本地缓存更宽；只要用户在窗口内用过一次就顺延。
#: 20 分钟过短会导致频繁要求重新验证码登录（h5 之所以「不用重登」正是
#: 因为它把罐放在浏览器本地、没有服务端过期一说）。这里取 12 小时，
#: 真实有效性仍以 `session/refresh` 探活上游为准。
SESSION_TTL = 12 * 60 * 60
#: 待验证码阶段的有效期（未提交即丢弃凭据）。
PENDING_TTL = 5 * 60
#: 单账号并发会话上限：同一学号可同时保留最近 N 条会话。
#:
#: ⚠️ 旧值是 1（只留最新一条），于是「手机上登一次、电脑上再登一次」会把
#: 前一个会话踢掉，前一个设备随后所有请求都收到 401，用户体感就是
#: 「登录状态不在了」。现在保留最近 N 条，多设备互不干扰；超出时才淘汰最旧的。
MAX_PER_USER = 3

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
    # 融合门户（一卡通余额）独立 cookie 罐：与教务 cookie 不通用。
    portal_cookies: dict[str, str] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)
    last: float = field(default_factory=time.monotonic)
    # 仅 pending 阶段持有：已加密密码、验证码 uid。
    encrypted_password: str = ""
    captcha_uid: str = ""
    # CAS 票据授予票据（TGT）。留着它就能在教务/门户 cookie 过期后**免验证码**
    # 重新换取 ST 重建会话罐 —— 这是 Web 端「静默续期」的唯一凭据，
    # 不落盘、不进日志、登出即丢。
    tgt: str = ""
    # 会话级复用的 keep-alive HTTP 客户端；懒创建，登出 / 过期时关闭。
    client: Any = None
    # 同一会话的隧道请求串行使用 client：httpx 自身并发安全，但手动 cookie
    # 罐的读写与 302 逐跳 cookie 回写不是原子的，串行能避免两次请求互相覆盖。
    request_lock: Any = field(default_factory=asyncio.Lock)


_sessions: dict[str, _Session] = {}
#: 同一学号保留的会话 sid 列表（最新在前）。旧实现只存一条，导致多设备
#: 登录互相踢下线；这里保留最近 [MAX_PER_USER] 条。
_by_user: dict[str, list[str]] = {}
_lock = asyncio.Lock()


def _user_sids(username: str) -> list[str]:
    return _by_user.setdefault(username, [])


def _remember_user_session(username: str, sid: str) -> list[_Session]:
    """登记新会话并淘汰同账号的旧会话。

    返回被淘汰的会话（调用方在锁外关闭它们的 HTTP 客户端）。
    """
    sids = _user_sids(username)
    if sid in sids:
        sids.remove(sid)
    sids.insert(0, sid)
    evicted: list[_Session] = []
    while len(sids) > MAX_PER_USER:
        old_sid = sids.pop()
        old_sess = _sessions.pop(old_sid, None)
        if old_sess is not None:
            evicted.append(old_sess)
    return evicted


def _forget_user_session(username: str, sid: str) -> None:
    sids = _by_user.get(username)
    if not sids:
        return
    if sid in sids:
        sids.remove(sid)
    if not sids:
        _by_user.pop(username, None)


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


async def _purge_expired() -> None:
    now = _now()
    stale = [
        sid
        for sid, s in _sessions.items()
        if now - s.last > (PENDING_TTL if s.stage == "pending" else SESSION_TTL)
    ]
    for sid in stale:
        s = _sessions.pop(sid, None)
        if s:
            await _close_session_client(s)
            _forget_user_session(s.username, sid)


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


def _session_client(sess: _Session) -> httpx.AsyncClient:
    """懒创建并复用会话级 HTTP 客户端（keep-alive 连接池）。

    单 worker 单事件循环内，同一会话复用同一 AsyncClient，避免每次隧道请求
    都重握 TCP+TLS。cookie 仍由服务端 dict 罐手动管理，不依赖客户端自带 jar。
    """
    if sess.client is None or getattr(sess.client, "is_closed", True):
        sess.client = _new_client()
    return sess.client


async def _close_session_client(sess: _Session) -> None:
    """关闭并丢弃会话持有的复用客户端；忽略关闭期异常。"""
    client = sess.client
    if client is not None:
        sess.client = None
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


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


async def _sso_to(
    client: httpx.AsyncClient,
    tgt: str,
    service: str,
    jar: dict[str, str],
) -> tuple[bool, str]:
    """用 TGT 换 ST 并访问 service 建立子系统会话；返回 (成功, 诊断)。

    ``jar`` 是该子系统独立的 cookie 罐（教务 / 门户互不通用）。
    """
    st_resp = await client.post(
        f"{CAS_BASE}/lyuapServer/v1/tickets/{tgt}",
        data={"service": service},
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
        return False, f"st_empty(换票HTTP {st_resp.status_code})"

    sep = "&" if "?" in service else "?"
    url = f"{service}{sep}ticket={st}"
    hops: list[str] = []
    # 逐跳跟随落地跳转（最多 6 跳），每跳捕获 cookie 并在下一跳回传，
    # 否则 ticketlogin 拿不到上一跳的 JSESSIONID/route，会被弹回登录页。
    for _ in range(6):
        headers: dict[str, str] = {"Referer": f"{CAS_BASE}/"}
        if jar:
            headers["Cookie"] = _cookie_header(jar)
        resp = await client.get(url, headers=headers)
        _capture_cookies(resp.headers, jar)
        location = resp.headers.get("location")
        hops.append(f"{resp.status_code}->{location or '-'}")
        if resp.status_code >= 300 and resp.status_code < 400 and location:
            url = urljoin(url, location)
            # 对齐安卓：同域 http 跳转升级 https（明文 80 端口会被弹回登录）。
            u = urlsplit(url)
            if u.scheme == "http" and u.hostname and u.hostname.endswith(".cwxu.edu.cn"):
                url = u._replace(scheme="https").geturl()
            continue
        # 落地页又被弹回登录页 → 会话没建起来。
        if not resp.is_success or "login" in str(resp.url).lower():
            return False, f"landing {resp.status_code} url={resp.url} hops={hops}"
        return True, f"ok hops={hops}"
    return False, f"redirect_loop hops={hops}"


# ---------------------------------------------------------------- 端点


async def _finish_login(
    sess: _Session, client: Any, tgt: str
) -> tuple[bool, str]:
    """拿到 TGT 后的收尾：建教务罐、best-effort 建门户罐、落会话状态。

    自动识别登录与人工回填登录共用这一条路径，避免两套收尾逻辑走偏。
    """
    ready, detail = await _sso_to(client, tgt, sess.service, sess.cookies)
    if not ready:
        return False, detail
    try:
        await _sso_to(client, tgt, PORTAL_SERVICE, sess.portal_cookies)
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
    sess.stage = "ready"
    sess.last = _now()
    # 留下 TGT：cookie 罐日后过期时靠它免验证码重建会话（静默续期）。
    sess.tgt = tgt
    sess.encrypted_password = ""
    sess.captcha_uid = ""
    return True, "ok"


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
    """一步登录：服务端自动识别验证码并完成 CAS 登录，失败再回退人工。

    请求体：``{"username": "...", "password": "...", "service": "可选"}``
    响应：
      - 自动识别成功：``{"session_id": "...", "ok": true}``（前端不需要弹验证码）
      - 三轮都没识别出来：``{"session_id": "...", "captcha_b64": "..."}``，
        与改造前一致，交人工回填后走 ``/session/submit``
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
    client = _session_client(sess)

    # 自动识别最多试 3 张：单张识别率 97%~100%，三张几乎必成，用户看不到验证码。
    for _attempt in range(3):
        try:
            uid, captcha_b64 = await _fetch_captcha(client)
        except httpx.TimeoutException:
            await _close_session_client(sess)
            return webproxy._fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
        except Exception:  # noqa: BLE001
            await _close_session_client(sess)
            return webproxy._fail(502, "upstream_error", "获取验证码失败，请稍后重试")
        sess.captcha_uid = uid

        try:
            answer = _captcha_solve(base64.b64decode(captcha_b64))
        except Exception:  # noqa: BLE001
            answer = None
        if not answer:
            continue

        try:
            data = await _submit_ticket(client, sess, answer)
        except Exception:  # noqa: BLE001
            continue

        tgt = data.get("tgt") if isinstance(data, dict) else None
        if isinstance(tgt, str) and tgt:
            try:
                ok, _detail = await _finish_login(sess, client, tgt)
            except Exception as e:  # noqa: BLE001
                # 自动登录分支里 `_finish_login` 抛异常时，不能把 pending 会话
                # 留在 `_sessions` 里、也不能继续拿这张票据重试 —— 关掉客户端
                # 后重来一轮验证码，最后一轮仍失败再回退人工。
                import traceback
                traceback.print_exc()
                ok = False
            if ok:
                async with _lock:
                    await _purge_expired()
                    _sessions[sid] = sess
                    evicted = _remember_user_session(username, sid)
                for old_sess in evicted:
                    await _close_session_client(old_sess)
                return {"session_id": sid, "ok": True}
            continue

        # 账密错 / 锁定这类不该反复重试，直接给结论。
        block = data.get("data") if isinstance(data, dict) else None
        code_str = block.get("code", "") if isinstance(block, dict) else ""
        msg = (
            block.get("msg") or block.get("message") or ""
            if isinstance(block, dict)
            else ""
        )
        if code_str == "PASSERROR":
            await _close_session_client(sess)
            return webproxy._fail(401, "bad_credential", "学号或密码不正确")
        if code_str == "NOUSER":
            # 本校 CAS 在「验证码正确 + 学号不存在」时回的是 NOUSER，不是 PASSERROR。
            # 旧逻辑只认 PASSERROR，于是「账号填错」被当成「验证码没过」，
            # 白白烧掉 3 张验证码，最后回退成人工回填 —— 用户体感就是
            # 「每次登录都得手输验证码」。这里直接给终态结论。
            await _close_session_client(sess)
            return webproxy._fail(401, "bad_credential", "学号不存在，请确认学号后重试")
        if _looks_locked(f"{code_str} {msg}"):
            await _close_session_client(sess)
            return webproxy._fail(423, "locked", "账号已被临时锁定，请稍后再试")

    # 自动识别没成：重新取一张干净的验证码，交给用户手填。
    try:
        uid, captcha_b64 = await _fetch_captcha(client)
        sess.captcha_uid = uid
    except Exception:  # noqa: BLE001
        await _close_session_client(sess)
        return webproxy._fail(502, "upstream_error", "获取验证码失败，请稍后重试")

    async with _lock:
        await _purge_expired()
        # 同一学号保留最近 MAX_PER_USER 条会话（多设备互不踢）。
        _sessions[sid] = sess
        evicted = _remember_user_session(username, sid)
    for old_sess in evicted:
        await _close_session_client(old_sess)

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
        await _purge_expired()
        sess = _sessions.get(sid)
        if sess is None or sess.stage != "pending":
            return webproxy._fail(401, "session_expired", "会话已过期，请重新登录")

    client = _session_client(sess)
    try:
        data = await _submit_ticket(client, sess, code)
    except httpx.TimeoutException:
        await _close_session_client(sess)
        return webproxy._fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        await _close_session_client(sess)
        return webproxy._fail(502, "upstream_error", f"提交登录失败：{type(e).__name__}: {e}")

    tgt = data.get("tgt")
    if isinstance(tgt, str) and tgt:
        try:
            ready, sso_detail = await _finish_login(sess, client, tgt)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            ready, sso_detail = False, f"{type(e).__name__}: {e}"
        if not ready:
            await _close_session_client(sess)
            return webproxy._fail(502, "sso_failed", f"换取教务会话失败：{sso_detail}")
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
    if code_str == "NOUSER":
        # 同 session_start：NOUSER 是终态（学号不存在），不该再让用户换验证码。
        return webproxy._fail(401, "bad_credential", "学号不存在，请确认学号后重试")
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

    host = webproxy._host_of(url)
    if host is None:
        return webproxy._fail(400, "bad_url", "只支持 http/https 地址")
    if not webproxy._host_allowed(host):
        return webproxy._fail(403, "host_not_allowed", f"不允许代理该站点：{host}")
    if await webproxy._resolves_to_private(host):
        return webproxy._fail(403, "private_address", f"该域名解析到内网地址，已拒绝：{host}")

    # 门户（my.cwxu.edu.cn）走独立 cookie 罐，其余走教务罐。
    is_portal = host == "my.cwxu.edu.cn" or host.endswith(".my.cwxu.edu.cn")

    async with _lock:
        await _purge_expired()
        sess = _sessions.get(sid)
        if sess is None or sess.stage != "ready":
            return webproxy._fail(401, "session_expired", "登录已过期，请重新登录")
        sess.last = _now()
        jar = dict(sess.portal_cookies if is_portal else sess.cookies)
        client = _session_client(sess)

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
            async with sess.request_lock:
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

        # 落地后把本轮捕获到的新 cookie 写回服务端对应子系统罐（会话续期 / 刷新）。
        async with _lock:
            target = _sessions.get(sid)
            if target is not None and target.stage == "ready":
                (target.portal_cookies if is_portal else target.cookies).update(jar)
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


#: 探活上游教务会话用的轻量页面（成绩查询首页：有会话 200，失效则 302 弹回登录）。
_JWGL_PROBE_URL = (
    f"{JWGL_BASE}/jwglxt/cjcx/cjcx_cxDgXscj.html?gnmkdm=N305005&layout=default"
)


async def _campus_alive(client: httpx.AsyncClient, jar: dict[str, str]) -> bool:
    """用一次 GET 判断教务 cookie 罐是否仍被上游认可。"""
    headers = {"Referer": f"{JWGL_BASE}/jwglxt/xtgl/index_initMenu.html"}
    if jar:
        headers["Cookie"] = _cookie_header(jar)
    try:
        resp = await client.get(_JWGL_PROBE_URL, headers=headers)
    except Exception:  # noqa: BLE001
        return False
    if resp.status_code != 200:
        return False
    return "dl_loginForwa" not in (resp.text or "")


@router.post("/api/v1/campus/session/refresh")
async def session_refresh(request: Request) -> Any:
    """探活 + 静默续期：免验证码、免账密地维持登录态。

    流程：先用一次 GET 探上游教务罐；还活着就只滑动续期（``refreshed=false``），
    死了就用登录时留下的 TGT 重新换 ST 重建 cookie 罐（``refreshed=true``）。
    两者都失败才返回 401，由前端决定是否让用户重新登录。
    """
    if httpx is None:
        return webproxy._fail(503, "proxy_unavailable", "服务器缺少 httpx，网页版教务暂不可用")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    sid = str(payload.get("session_id") or "").strip() if isinstance(payload, dict) else ""
    if not sid:
        return webproxy._fail(400, "missing_field", "缺少 session_id")

    async with _lock:
        await _purge_expired()
        sess = _sessions.get(sid)
        if sess is None or sess.stage != "ready":
            return webproxy._fail(401, "session_expired", "登录已过期，请重新登录")
        client = _session_client(sess)
        jar = dict(sess.cookies)
        tgt = sess.tgt
        service = sess.service

    if await _campus_alive(client, jar):
        async with _lock:
            sess.last = _now()
        return {"ok": True, "refreshed": False}

    if not tgt:
        # 早期会话没有留 TGT，无法静默重建 —— 只能让用户重新登录。
        async with _lock:
            gone = _sessions.pop(sid, None)
            if gone is not None:
                _forget_user_session(gone.username, sid)
        if gone is not None:
            await _close_session_client(gone)
        return webproxy._fail(401, "session_expired", "登录已过期，请重新登录")

    fresh: dict[str, str] = {}
    try:
        ready, detail = await _sso_to(client, tgt, service, fresh)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return webproxy._fail(502, "upstream_error", f"静默续期失败：{type(exc).__name__}: {exc}")

    if not ready:
        # TGT 也失效了（CAS 侧过期或已被挤掉）→ 必须重新走账密 + 验证码。
        async with _lock:
            gone = _sessions.pop(sid, None)
            if gone is not None:
                _forget_user_session(gone.username, sid)
        if gone is not None:
            await _close_session_client(gone)
        return webproxy._fail(401, "session_expired", "登录已过期，请重新登录")

    # 门户（一卡通余额）独立子系统：best-effort 重建，失败不影响教务。
    portal: dict[str, str] = {}
    try:
        await _sso_to(client, tgt, PORTAL_SERVICE, portal)
    except Exception:  # noqa: BLE001
        portal = {}

    async with _lock:
        sess.cookies = fresh
        if portal:
            sess.portal_cookies = portal
        sess.last = _now()

    return {"ok": True, "refreshed": True, "detail": detail}


@router.post("/api/v1/campus/session/ping")
async def session_ping(request: Request) -> Any:
    """探活：校验本地留存的 session_id 是否仍是就绪会话。

    请求体：``{"session_id": "..."}``。
    就绪 → ``{"ok": true}`` 并滑动续期；不存在 / 过期 → 401 ``session_expired``。
    供 Web 端刷新页面后先拿本地 sid 探活，未过期免验证码进入。
    """
    if httpx is None:
        return webproxy._fail(503, "proxy_unavailable", "服务器缺少 httpx，网页版教务暂不可用")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    sid = str(payload.get("session_id") or "").strip() if isinstance(payload, dict) else ""
    if not sid:
        return webproxy._fail(400, "missing_field", "缺少 session_id")

    async with _lock:
        await _purge_expired()
        sess = _sessions.get(sid)
        if sess is None or sess.stage != "ready":
            return webproxy._fail(401, "session_expired", "登录已过期，请重新登录")
        sess.last = _now()

    return {"ok": True}


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
        if sess is not None:
            _forget_user_session(sess.username, sid)
    if sess is not None:
        await _close_session_client(sess)
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
