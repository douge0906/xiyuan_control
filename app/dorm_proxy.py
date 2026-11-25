"""掌上锡院 Web 端「电费查询 / 充值」控制端代理（一期）。

## 为什么需要它

浏览器（Flutter Web / PWA）开不了 TCP 到内网 `10.3.100.110`（慧新 E 校平台），
安卓端那条「CAS 登录 → TGT → WebVPN 换票 → 平台 ST → 平台票 → oauth token →
`getThirdData`」的链路只能由服务端代跑。本模块把这条链路搬到控制服务器上，
**以 WebVPN 网关为唯一出口**（网关在公网，能反向访问内网），对浏览器只暴露
8 个 JSON 端点（`/api/v1/campus/dorm/*`）。

## 与 `campus_proxy.py` / `webproxy.py` 的边界（硬规矩）

1. **独立模块、独立前缀** `/api/v1/campus/dorm`，不改这两个模块的任何行为，
   只**只读**复用它们的工具函数（cookie 捕获 / 拼装 / RSA 加密 / 建客户端 /
   验证码识别 / 限流 / 错误信封）。
2. **per-user 隔离**：每个会话独立的 `httpx.AsyncClient` + **独立的 cookie 罐**
   （网关罐与平台罐分开）。**绝不**复用 `webproxy._http()` 那个进程级共享
   client —— 2026-11-10 已经因此把上一个用户的 cookie 串给下一个人。
3. **凭据不落盘、不进日志**：密码只在「待提交验证码」阶段留在内存；TGT /
   ST / 平台票 / access_token 只存内存，登出即丢；日志一律脱敏。
4. **网关票据是短命的**：每次内网请求都要能识别「被弹回 `/login`」并自动
   重铸（最多重试一次）。

## 平台侧的三条硬约束（踩过的坑）

- `synAccessSource` **必须是 `app`**：`h5` / `pc` 会被平台以 4030「服务大厅未授权」
  拒掉（2026-10-26 实测）。query 与同名头**两处都带**。
- **平台票那一步不可省**：只用 CAS 的 ST 去换 token 会拿到低权限 token，同样
  4030。必须先访问 `lyCas?ticket=<ST>`，从中间那一跳的 URL 上取平台自己的票。
- 平台票只出现在**中间那一跳**的 URL 上（最终落点会丢），必须逐跳捕获。
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import webproxy
from . import webvpn
from .campus_proxy import (
    _UA,
    _capture_cookies,
    _close_session_client,
    _cookie_header,
    _fetch_captcha,
    _looks_locked,
    _new_client,
    _rsa_encrypt,
    _submit_ticket,
)
from .captcha_solver import solve as _captcha_solve

# httpx 可选依赖：缺失时应用照常启动，仅本模块端点返回 503（同 webproxy 策略）。
try:  # noqa: SIM105
    import httpx  # type: ignore
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore[assignment]

router = APIRouter()

# ---------------------------------------------------------------- 常量

#: 内网平台（慧新 E 校）根地址。服务端不可达，只能经 WebVPN 网关访问。
PLATFORM_BASE = webvpn.PLATFORM_BASE

#: 平台注册给 CAS 的 service（内网地址！CAS 只校验它登记过，不校验可达性）。
PLATFORM_CAS_SERVICE = (
    f"{PLATFORM_BASE}/berserker-auth/cas/login/lyCas"
    "?targetUrl=http%3A%2F%2F10.3.100.110%2Fplat%2F%3Fname%3DloginTransit"
)

#: 平台 oauth 换 token 的端点。
PLATFORM_TOKEN_URL = f"{PLATFORM_BASE}/berserker-auth/oauth/token"

#: 电费查询接口（POST form）。
PLATFORM_QUERY_URL = f"{PLATFORM_BASE}/charge/feeitem/getThirdData"

#: 电费充值 SPA（充值页 URL 在服务端拼好后交给浏览器打开）。
PLATFORM_CHARGE_APP = f"{PLATFORM_BASE}/charge-app"

#: 一卡通充值 SPA 与平台会话预热入口。
PLATFORM_CARD_RECHARGE = (
    f"{PLATFORM_BASE}/campus-card/cardRecharge?appId=27&loginFrom=h5&synAccessSource=h5"
)
PLATFORM_CARD_LOGIN = f"{PLATFORM_BASE}/plat/?name=loginTransit"

#: 平台 oauth 用的 Basic 头（= mobile_service_platform:mobile_service_platform_secret）。
PLATFORM_BASIC = (
    "Basic bW9iaWxlX3NlcnZpY2VfcGxhdGZvcm06bW9iaWxlX3NlcnZpY2VfcGxhdGZvcm1fc2VjcmV0"
)

#: 电费接口用的 Basic 头（= charge:charge_secret）。
CHARGE_BASIC = "Basic Y2hhcmdlOmNoYXJnZV9zZWNyZXQ="

#: 平台来源参数：必须是 `app`（h5/pc ⇒ 4030「服务大厅未授权」）。
SYN_ACCESS_SOURCE = "app"

#: 平台票 → access_token 用的固定参数（安卓端同款）。
PLATFORM_GRANT = {
    "grant_type": "password",
    "scope": "all",
    "loginFrom": "app",
    "logintype": "sso",
    "device_token": "h5",
}

#: 会话有效期（秒）：滑动续期，与 `campus_proxy.SESSION_TTL` 同口径。
SESSION_TTL = 12 * 60 * 60

#: 待验证码阶段的有效期（超时即丢弃凭据）。
PENDING_TTL = 5 * 60

#: 单账号并发会话上限：同一学号可同时保留最近 N 条会话。
#:
#: ⚠️ 旧值是 1（只留最新一条），于是「手机上登一次、电脑上再登一次」会把
#: 前一个会话踢掉，前一个设备随后所有请求都收到 401，用户体感就是
#: 「登录状态不在了」。现在保留最近 N 条，多设备互不干扰。
MAX_PER_USER = 3

#: 同一账号两次 CAS 登录之间的最小间隔（秒）。学校按「登录失败次数」计锁号，
#: 放宽这个常量等于把学生账号往锁上推（安卓端 cas_client.dart 同为 8 秒）。
LOGIN_THROTTLE_SECONDS = 8

#: 自动识别验证码最多试 3 张（同 campus_proxy）。
CAPTCHA_ATTEMPTS = 3

#: 充值 URL 的有效期（秒）——仅作提示，真实有效期由平台/网关票据决定。
RECHARGE_URL_TTL = 300

#: 网关 cookie 的名字特征（深澜 WebVPN 自家发的）。其余 cookie 归平台。
_GATEWAY_COOKIE_HINTS = ("wengine_vpn", "route", "ssoready")

# ---------------------------------------------------------------- 浏览器桥接

#: 控制端自己的域名（桥页由它提供，再经网关代理加载 ⇒ 浏览器 origin 变成网关，
#: 与校内网平台同源，于是浏览器能直接同源 fetch 包装后的内网地址）。
CONTROL_HOST = "con.wxxydouge.icu"

#: 桥页在内网平台上的主机（hex 形式由 `webvpn.encrypt_host` 算）。
PLATFORM_HOST = "10.3.100.110"

#: 桥页 URL 路径（经网关代理后是 `/https/<hex(控制域名)>/api/v1/campus/dorm/bridge`）。
BRIDGE_PATH = "/api/v1/campus/dorm/bridge"

#: 桥页 HTML（内联 JS，无外部依赖）。参数一律从 `location.hash` 读，
#: 这样票据不会出现在 URL 的 query 里，也就不会进 nginx 访问日志。
BRIDGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<title>掌上锡院 · 校园网桥</title>
</head>
<body>
<pre id="log">bridge loading…</pre>
<script>
// 掌上锡院 · WebVPN 桥页（生产版原型）
//
// 本页由 con.wxxydouge.icu 提供，但**通过 WebVPN 网关加载**：
//   https://webvpn.cwxu.edu.cn/https/<hex(con.wxxydouge.icu)>/api/v1/campus/dorm/bridge#...
// 所以本页的 origin 是 https://webvpn.cwxu.edu.cn —— 与校内网平台同源，
// 可以直接同源 fetch 包装后的内网地址，绕开「服务器出口 IP 被网关 403 拒绝」。
//
// 前提：网关票据 cookie 已在**前一步**种好（客户端先开一次网关登录页）。
// 若平台接口回的不是 JSON 而是 HTML（= 被弹回登录页），说明票据没了，
// 这时用 st_gw2（备用的一次性票）自举一次再重试。
(function () {
  var el = document.getElementById('log');
  function log(m) { if (el) el.textContent += '\\n' + m; }
  function finish(payload) {
    window.__bridgeResult = payload;
    try { if (window.opener) window.opener.postMessage(
      {source: 'xiyuan-bridge', type: 'dorm-result', payload: payload}, '*'); } catch (e) {}
    // 充值类结果**不能关窗**：客户端要复用这个窗口导航到充值页，
    // 关掉再开就是「没有用户手势的新窗口」→ 会被弹窗拦截器拦下。
    var keep = !!(payload && payload.ok && payload.kind);
    if (!keep) { try { window.close(); } catch (e) {} }
  }

  var GW = '__GATEWAY__';
  var PLATFORM_BASIC = '__PLATFORM_BASIC__';
  var CHARGE_BASIC = '__CHARGE_BASIC__';

  function jfetch(url, method, headers, body) {
    var h = {};
    if (headers) { for (var k in headers) { if (Object.prototype.hasOwnProperty.call(headers, k)) h[k] = headers[k]; } }
    if (body && !h['Content-Type']) h['Content-Type'] = 'application/x-www-form-urlencoded';
    return fetch(url, {method: method || 'GET', headers: h, body: body || undefined, credentials: 'include'})
      .then(function (r) {
        return r.text().then(function (t) {
          var j = null; try { j = JSON.parse(t); } catch (e) {}
          return {status: r.status, url: r.url, json: j, text: j ? '' : t.slice(0, 300)};
        });
      });
  }

  function parseHash() {
    var out = {};
    var raw = (location.hash || '').replace(/^#/, '');
    if (!raw) return out;
    raw.split('&').forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf('=');
      var k = i < 0 ? kv : kv.slice(0, i);
      var v = i < 0 ? '' : kv.slice(i + 1);
      try { out[decodeURIComponent(k)] = decodeURIComponent(v); } catch (e) { out[k] = v; }
    });
    return out;
  }

  function getPlatformTicket(P, q) {
    var casTarget = encodeURIComponent('__PLAT_TARGET__');
    return jfetch(P + '/berserker-auth/cas/login/lyCas?targetUrl=' + casTarget
                  + '&ticket=' + encodeURIComponent(q.st_plat), 'GET')
      .then(function (r1) {
        var plat = '';
        try {
          var all = new URL(r1.url).searchParams.getAll('ticket');
          for (var i = 0; i < all.length; i++) {
            if (all[i].indexOf('ST-') !== 0) { plat = all[i]; break; }
          }
        } catch (e) {}
        log('platform ticket len ' + plat.length);
        if (!plat) throw new Error('平台票据获取失败');
        return plat;
      });
  }

  function getToken(P, plat) {
    var grant = 'grant_type=password&scope=all&loginFrom=app&logintype=sso&device_token=h5';
    var form = grant + '&username=' + encodeURIComponent(plat) + '&password=' + encodeURIComponent(plat);
    return jfetch(P + '/berserker-auth/oauth/token', 'POST', {'Authorization': PLATFORM_BASIC}, form)
      .then(function (r2) {
        var token = (r2.json && r2.json.access_token) || '';
        log('access_token len ' + token.length);
        if (!token) throw new Error('校园网认证失败');
        return token;
      });
  }

  function queryElec(P, token, q) {
    var body = 'feeitemid=' + encodeURIComponent(q.feeitemid || '448')
      + '&type=IEC&level=3'
      + '&campus=' + encodeURIComponent(q.campus || '1')
      + '&building=' + encodeURIComponent(q.building || '')
      + '&room=' + encodeURIComponent(q.room || '');
    return jfetch(P + '/charge/feeitem/getThirdData', 'POST', {
      'Authorization': CHARGE_BASIC,
      'synjones-auth': 'bearer ' + token,
      'synAccessSource': 'app'
    }, body);
  }

  // 充值页地址只能在这里拼：它要带平台 access_token，而平台只有浏览器进得去
  // （服务端出口 IP 被网关 403）。路径前缀直接用网关的 `/http/<hex>` 形式，
  // 所以本页不需要自己算 AES。
  function buildElecRecharge(q, token) {
    return GW + '/http/' + q.hex + '__CHARGE_APP_PATH__'
      + '?name=pays&id=' + encodeURIComponent(q.feeitemid || '448')
      + '&paymentUrl=' + encodeURIComponent('__PAYMENT_TARGET__')
      + '&token=' + encodeURIComponent(token);
  }

  function buildCardRecharge(q, plat) {
    var base = GW + '/http/' + q.hex;
    var target = encodeURIComponent('__CARD_RECHARGE_PATH__');
    return {
      warmup_url: base + '__CARD_LOGIN_PATH__'
        + '&ticket=' + encodeURIComponent(plat) + '&targetUrl=' + target,
      url: base + '__CARD_RECHARGE_PATH__'
    };
  }

  function bootstrapGateway(q) {
    if (!q.st_gw2) return Promise.resolve(false);
    log('re-bootstrapping gateway ticket');
    return jfetch(GW + '/login?cas_login=true&ticket=' + encodeURIComponent(q.st_gw2), 'GET')
      .then(function () { return true; });
  }

  var Q = {};

  function acquire(P) {
    return getPlatformTicket(P, Q).then(function (plat) {
      return getToken(P, plat).then(function (token) {
        return {plat: plat, token: token};
      });
    });
  }

  function run() {
    Q = parseHash();
    var P = GW + '/http/' + Q.hex;
    var action = Q.action || 'query';

    // 一卡通充值：只需要一张**还没被消费**的平台票据去预热登录态。
    // 千万不能走 acquire()：oauth 换 token 会把这票据用掉（一次性），
    // 再用它加热登录页只会落到平台登录页。
    if (action === 'card') {
      return getPlatformTicket(P, Q)
        .then(function (plat) {
          var card = buildCardRecharge(Q, plat);
          finish({ok: true, kind: 'card', url: card.url, warmup_url: card.warmup_url});
        })
        .catch(function (e) { finish({ok: false, error: String((e && e.message) || e)}); });
    }

    return acquire(P)
      .then(function (ctx) {
        if (action === 'recharge') {
          finish({ok: true, kind: 'recharge', url: buildElecRecharge(Q, ctx.token)});
          return null;
        }
        return queryElec(P, ctx.token, Q).then(function (r3) {
          if (r3.json && r3.json.code === 200) return r3;
          // 没票据 → 自举一次再试
          log('retry after bootstrap');
          return bootstrapGateway(Q).then(function () {
            return acquire(P);
          }).then(function (ctx2) {
            return queryElec(P, ctx2.token, Q);
          });
        }).then(function (r3) {
          var j = r3.json || {};
          if (j.code !== 200) { finish({ok: false, error: '电费查询失败（' + j.code + '）'}); return; }
          finish({ok: true, code: j.code, map: j.map});
        });
      })
      .catch(function (e) { finish({ok: false, error: String((e && e.message) || e)}); });
  }

  window.__bridgeReady = true;
  run();
})();
</script>
</body>
</html>
"""


def _render_bridge_html() -> str:
    """把桥页模板里的占位符换成真实常量。

    为什么不直接写成字面量：网关地址与两个 Basic 头都是模块常量，改了配置
    桥页要跟着变；写成占位符只有一处要改。
    """
    return (
        BRIDGE_HTML.replace("__GATEWAY__", webvpn.GATEWAY)
        .replace("__PLATFORM_BASIC__", PLATFORM_BASIC)
        .replace("__CHARGE_BASIC__", CHARGE_BASIC)
        .replace("__PLAT_TARGET__", f"{PLATFORM_BASE}/plat/?name=loginTransit")
        .replace("__CHARGE_APP_PATH__", "/charge-app")
        .replace("__PAYMENT_TARGET__", f"{PLATFORM_BASE}/plat")
        .replace("__CARD_RECHARGE_PATH__", PLATFORM_CARD_RECHARGE.replace(PLATFORM_BASE, ""))
        .replace("__CARD_LOGIN_PATH__", PLATFORM_CARD_LOGIN.replace(PLATFORM_BASE, ""))
    )


class DormError(Exception):
    """业务错误：带 code / message，直接转成 ``{"error": {...}}`` 信封。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 会话存储


@dataclass
class _DormSession:
    """一个用户的电费会话（全部状态只在内存里）。"""

    sid: str
    username: str
    # "pending" = 等人工验证码；"cas" = 只完成了 CAS 登录（桥接模式：有 TGT，
    # 能现铸 ST，但服务端自己跑不动内网）；"ready" = 网关 + 平台都通了。
    stage: str
    # 登录模式："server" = 服务端代跑内网（默认）；"bridge" = 浏览器自己去读内网。
    mode: str = "server"
    # CAS 登录用的 service：安卓端用的就是平台的 lyCas service。
    service: str = ""
    created: float = field(default_factory=time.monotonic)
    last: float = field(default_factory=time.monotonic)
    # 仅 pending 阶段持有：已加密密码、验证码 uid。登录成功后立刻抹掉。
    encrypted_password: str = ""
    captcha_uid: str = ""
    # CAS 票据授予票据（TGT）：留着它就能免验证码重铸 ST / 平台票 / token。
    tgt: str = ""
    # 平台 access_token（电费查询与充值 URL 都要用）。
    platform_token: str = ""
    # 网关 cookie 罐与平台 cookie 罐**分开**：两者生命周期不同，混在一起
    # 会把网关票据当成平台会话丢掉（反之亦然）。
    gateway_cookies: dict[str, str] = field(default_factory=dict)
    platform_cookies: dict[str, str] = field(default_factory=dict)
    # 会话级 keep-alive 客户端（per-session，绝不复用全局 client）。
    client: Any = None
    # 同一会话的请求串行：手动 cookie 罐的读写不是原子的。
    request_lock: Any = field(default_factory=asyncio.Lock)


_sessions: dict[str, _DormSession] = {}
#: 同一学号保留的会话 sid 列表（最新在前）。旧实现只存一条，导致多设备
#: 登录互相踢下线；这里保留最近 [MAX_PER_USER] 条。
_by_user: dict[str, list[str]] = {}
_lock = asyncio.Lock()

#: 每账号的 CAS 登录节流：username -> 上次发起登录的时间戳。
_login_throttle: dict[str, float] = {}


def _user_sids(username: str) -> list[str]:
    return _by_user.setdefault(username, [])


def _remember_user_session(username: str, sid: str) -> list[_DormSession]:
    """登记新会话并淘汰同账号的旧会话。

    返回被淘汰的会话（调用方在锁外关闭它们的 HTTP 客户端）。
    """
    sids = _user_sids(username)
    if sid in sids:
        sids.remove(sid)
    sids.insert(0, sid)
    evicted: list[_DormSession] = []
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


def _new_sid() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _session_client(sess: _DormSession) -> Any:
    """懒创建并复用会话级客户端（**每个会话独占一个**）。

    绝不复用 `webproxy._http()`：那是进程级共享 client，cookie 罐也是进程级的，
    2026-11-10 已经因此把上一个用户的 cookie 串给下一个人。
    """
    if sess.client is None or getattr(sess.client, "is_closed", True):
        sess.client = _new_client()
    return sess.client


def _combined_cookie(sess: _DormSession) -> str:
    """网关罐 + 平台罐合并成一条 Cookie 头（同名键以平台罐为准）。"""
    merged = dict(sess.gateway_cookies)
    merged.update(sess.platform_cookies)
    return _cookie_header(merged)


def _split_cookies(captured: dict[str, str], sess: _DormSession) -> None:
    """把一次请求收回来的 cookie 分门别类：网关的进网关罐，平台的进平台罐。"""
    for key, value in captured.items():
        if key.lower().startswith(_GATEWAY_COOKIE_HINTS):
            sess.gateway_cookies[key] = value
        else:
            sess.platform_cookies[key] = value


async def _purge_expired() -> None:
    now = _now()
    stale = [
        sid
        for sid, s in _sessions.items()
        if now - s.last > (PENDING_TTL if s.stage == "pending" else SESSION_TTL)
    ]
    for sid in stale:
        s = _sessions.pop(sid, None)
        if s is not None:
            await _close_session_client(s)
            _forget_user_session(s.username, sid)


def _throttle_remaining(username: str) -> float:
    """同一账号距离上次 CAS 登录还差几秒（0 表示可以发起）。"""
    last = _login_throttle.get(username)
    if last is None:
        return 0.0
    elapsed = time.monotonic() - last
    if elapsed >= LOGIN_THROTTLE_SECONDS:
        return 0.0
    return LOGIN_THROTTLE_SECONDS - elapsed


def _mark_login(username: str) -> None:
    _login_throttle[username] = time.monotonic()


# ---------------------------------------------------------------- 上游调用


async def _gateway_fetch(
    sess: _DormSession,
    raw_url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    body: bytes | None = None,
    retry: bool = True,
):
    """经网关发一次请求；网关票据过期时自动重铸并重试一次。

    返回 ``(status, url, headers, body_bytes)``。
    """
    client = _session_client(sess)
    captured: dict[str, str] = {}
    merged = dict(headers or {})
    merged["Cookie"] = _combined_cookie(sess)
    status, url, resp_headers, content = await webvpn.vpn_fetch(
        client,
        captured,
        raw_url,
        method=method,
        headers=merged,
        form=form,
        body=body,
    )
    _split_cookies(captured, sess)

    # 302 → /login = 网关票据过期（实测：带旧票据访问包装 URL 仍被弹回登录页）。
    location = resp_headers.get("location") if resp_headers else None
    if status in (301, 302, 303, 307, 308) and location and webvpn._is_gateway_login(
        location
    ):
        if not retry or not sess.tgt:
            raise DormError(401, "unauthorized", "网关票据已失效，请重新登录")
        await webvpn.webvpn_login(client, sess.gateway_cookies, sess.tgt)
        captured = {}
        merged = dict(headers or {})
        merged["Cookie"] = _combined_cookie(sess)
        status, url, resp_headers, content = await webvpn.vpn_fetch(
            client,
            captured,
            raw_url,
            method=method,
            headers=merged,
            form=form,
            body=body,
        )
        _split_cookies(captured, sess)
    return status, url, resp_headers, content


async def _platform_login(sess: _DormSession) -> str:
    """平台登录：ST → 平台票 → oauth access_token。

    顺序不可换、不可省任何一步：省掉「平台票」那一步会拿到低权限 token，
    后续所有电费接口都会回 4030「服务大厅未授权」。
    """
    client = _session_client(sess)
    st = await webvpn.cas_st(client, sess.tgt, PLATFORM_CAS_SERVICE)

    # 平台票只出现在中间那一跳：逐跳找（相对 Location 相对未包装地址解析）。
    start = f"{PLATFORM_CAS_SERVICE}&ticket={quote(st, safe='')}"
    platform_ticket = ""
    try:
        platform_ticket = await webvpn.locate_platform_ticket(
            client, sess.platform_cookies, start
        )
    except Exception:  # noqa: BLE001 - 拿不到就退回用 ST（安卓端同款回退）
        platform_ticket = ""
    ticket_for_token = platform_ticket or st

    form = dict(PLATFORM_GRANT)
    form["username"] = ticket_for_token
    form["password"] = ticket_for_token
    status, _url, _headers, content = await _gateway_fetch(
        sess,
        PLATFORM_TOKEN_URL,
        method="POST",
        headers={
            "Authorization": PLATFORM_BASIC,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        form=form,
    )
    if status >= 500:
        raise DormError(502, "upstream_error", f"平台换票失败：HTTP {status}")
    try:
        data = json.loads(content.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        raise DormError(502, "upstream_error", "平台换票返回非 JSON") from None
    token = ""
    if isinstance(data, dict):
        token = str(data.get("access_token") or "")
    if not token:
        raise DormError(401, "unauthorized", "平台换票未返回 access_token")
    sess.platform_token = token
    return token


async def _ensure_token(sess: _DormSession) -> str:
    """确保手上有可用的平台 token；没有就用 TGT 静默换一张。"""
    if sess.platform_token:
        return sess.platform_token
    if not sess.tgt:
        raise DormError(401, "unauthorized", "登录已过期，请重新登录")
    return await _platform_login(sess)


async def _query_third_data(
    sess: _DormSession,
    token: str,
    feeitemid: str,
    campus: str,
    building: str,
    room: str,
) -> dict[str, Any]:
    """查电费：POST ``/charge/feeitem/getThirdData``，返回上游的 ``map`` 原样。

    上游 ``map`` **不做任何解析**（余额字段在客户端解），服务端只负责鉴权、
    转发与错误分类。
    """
    form = {
        "feeitemid": str(feeitemid),
        "type": "IEC",
        "level": "3",
        "campus": campus,
        "building": building,
        "room": room,
    }
    headers = {
        "Authorization": CHARGE_BASIC,
        "synjones-auth": f"bearer {token}",
        "synAccessSource": SYN_ACCESS_SOURCE,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    status, _url, _headers, content = await _gateway_fetch(
        sess, PLATFORM_QUERY_URL, method="POST", headers=headers, form=form
    )
    text = content.decode("utf-8", "replace")
    if status >= 500:
        raise DormError(502, "upstream_error", f"电费查询失败：HTTP {status}")
    if text.lstrip().startswith("<"):
        # 网关登录页 / 平台登录页 HTML：票据掉了。
        raise DormError(401, "unauthorized", "会话已失效，请重新登录")
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        raise DormError(502, "upstream_error", "电费查询返回非 JSON") from None
    if not isinstance(data, dict):
        raise DormError(502, "upstream_error", "电费查询返回格式异常")
    code = str(data.get("code") or "")
    if code != "200":
        if code in ("4030", "401", "403"):
            raise DormError(401, "unauthorized", "平台未授权，请重新登录")
        raise DormError(502, "upstream_error", f"电费查询失败：code={code or '空'}")
    payload = data.get("map")
    if not isinstance(payload, dict):
        raise DormError(502, "upstream_error", "电费查询缺少 map 数据")
    return payload


async def _fresh_platform_ticket(sess: _DormSession) -> str:
    """现铸一张平台票（给浏览器的卡包预热 URL 用）。"""
    client = _session_client(sess)
    st = await webvpn.cas_st(client, sess.tgt, PLATFORM_CAS_SERVICE)
    start = f"{PLATFORM_CAS_SERVICE}&ticket={quote(st, safe='')}"
    try:
        ticket = await webvpn.locate_platform_ticket(
            client, sess.platform_cookies, start
        )
    except Exception:  # noqa: BLE001
        ticket = ""
    return ticket


# ---------------------------------------------------------------- 登录流程


async def _dorm_login(
    sess: _DormSession,
    client: Any,
    code: str,
    cas_only: bool = False,
) -> tuple[bool, str]:
    """用验证码提交 CAS 登录；成功则继续走 WebVPN + 平台登录。

    ``cas_only=True``（桥接模式）时**只做 CAS 登录**：拿到 TGT 就收工，
    网关换票与平台登录都不做 —— 因为服务器出口 IP 被网关 403，内网只能由
    用户浏览器自己去读（见 `bridge-ticket` / `bridge` 两个端点）。

    返回 ``(成功, 诊断码)``：诊断码为空串表示成功；``bad_credential`` /
    ``locked`` 表示不该再试；``captcha_failed`` 表示换张验证码继续；
    ``vpn:*`` / ``platform:*`` 表示票拿到了但内网链路没通。
    """
    try:
        data = await _submit_ticket(client, sess, code)
    except Exception:  # noqa: BLE001
        return False, "upstream_error"

    tgt = data.get("tgt") if isinstance(data, dict) else None
    if not (isinstance(tgt, str) and tgt):
        block = data.get("data") if isinstance(data, dict) else None
        code_str = block.get("code", "") if isinstance(block, dict) else ""
        msg = (
            (block.get("msg") or block.get("message") or "")
            if isinstance(block, dict)
            else ""
        )
        if code_str == "PASSERROR":
            return False, "bad_credential"
        if _looks_locked(f"{code_str} {msg}"):
            return False, "locked"
        return False, "captcha_failed"

    sess.tgt = tgt
    sess.stage = "cas" if cas_only else "ready"
    sess.last = _now()
    sess.encrypted_password = ""
    sess.captcha_uid = ""

    if cas_only:
        # 桥接模式：有 TGT 就能现铸 ST，剩下的让浏览器去做。
        return True, ""

    # ② WebVPN 换票：网关票据是后续所有内网请求的前提。
    try:
        await webvpn.webvpn_login(client, sess.gateway_cookies, tgt)
    except webvpn.WebVpnError as exc:
        return False, f"vpn:{webvpn.redact(str(exc))}"
    except Exception as exc:  # noqa: BLE001
        return False, f"vpn:{type(exc).__name__}"

    # ③ 平台 ST → 平台票 → access_token。
    try:
        await _platform_login(sess)
    except DormError as exc:
        return False, f"platform:{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"platform:{type(exc).__name__}"
    return True, ""


# ---------------------------------------------------------------- 端点


def _fail(status: int, code: str, message: str) -> JSONResponse:
    """统一错误信封（与 webproxy / campus_proxy 一致）。"""
    return webproxy._fail(status, code, message)


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return payload if isinstance(payload, dict) else {}


async def _require_session(sid: str) -> _DormSession:
    """取「服务端自己已经把内网跑通」的会话（`query` / 充值类端点用）。

    桥接模式（`stage == cas`）的会话只完成了 CAS 登录，服务端摸不到内网，
    这类端点对它无解 —— 直接给一句明确的文案，别让用户以为是登录过期。
    """
    async with _lock:
        await _purge_expired()
        sess = _sessions.get(sid)
    if sess is None or not sess.tgt:
        raise DormError(401, "session_expired", "登录已过期，请重新登录")
    if sess.stage != "ready":
        raise DormError(
            400,
            "bridge_unsupported",
            "网页版暂不支持该操作（内网需由浏览器直连），请在安卓 App 内使用",
        )
    return sess


async def _require_cas_session(sid: str) -> _DormSession:
    """取一个「至少完成 CAS 登录」的会话（桥接模式用）。

    桥接会话的 stage 是 ``cas``（有 TGT，但没有网关/平台登录态），普通会话
    是 ``ready``。两者都能铸 ST，所以都放行。
    """
    async with _lock:
        await _purge_expired()
        sess = _sessions.get(sid)
    if sess is None or sess.stage not in ("ready", "cas") or not sess.tgt:
        raise DormError(401, "session_expired", "登录已过期，请重新登录")
    return sess


async def _store_session(sess: _DormSession) -> None:
    """落会话，并把同一学号超出 [MAX_PER_USER] 条的旧会话淘汰掉。

    ⚠️ 不再像旧实现那样「只留最新一条」—— 那会让另一台设备立刻掉登录。
    """
    async with _lock:
        await _purge_expired()
        _sessions[sess.sid] = sess
        evicted = _remember_user_session(sess.username, sess.sid)
    for old_sess in evicted:
        if old_sess.sid != sess.sid:
            await _close_session_client(old_sess)


async def _touch(sid: str) -> None:
    async with _lock:
        target = _sessions.get(sid)
        if target is not None:
            target.last = _now()


@router.post("/api/v1/campus/dorm/session/start")
async def session_start(request: Request) -> Any:
    """一步登录：CAS 验证码登录 → WebVPN 换票 → 平台票 → access_token。

    请求体：``{"username": "...", "password": "..."}``
    响应：

    - 成功：``{"session_id": "...", "ok": true}``
    - 需人工验证码：``{"session_id": "...", "captcha_b64": "..."}``
    - 失败：``{"error": {"code": "bad_credential|locked|rate_limited|...",
      "message": "..."}}``

    可选 ``"mode": "bridge"``：只做 CAS 登录（拿 TGT 就收工），跳过网关换票与
    平台登录 —— 服务器出口 IP 被 WebVPN 网关 403，内网只能由用户浏览器自己去读。
    这时成功响应多一个 ``"mode": "bridge"``，后续用 ``bridge-ticket`` 铸票。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，电费查询暂不可用")

    ip = request.client.host if request.client else "-"
    if not await webproxy._rate_ok(ip):
        return _fail(429, "rate_limited", "请求过于频繁，请稍后重试")

    payload = await _body(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return _fail(400, "missing_credential", "缺少学号或密码")

    mode = str(payload.get("mode") or "").strip().lower()
    bridge = mode == "bridge"

    # 同一账号 8 秒内只允许发起一次 CAS 登录（学校按登录失败次数计锁号）。
    if _throttle_remaining(username) > 0:
        return _fail(429, "rate_limited", "登录过于频繁，请稍后再试")

    sess = _DormSession(
        sid=_new_sid(),
        username=username,
        stage="pending",
        mode="bridge" if bridge else "server",
        service=PLATFORM_CAS_SERVICE,
        encrypted_password=_rsa_encrypt(password),
    )
    client = _session_client(sess)

    # 自动识别验证码最多 3 张：识别率 97%~100%，用户基本看不到验证码。
    for _attempt in range(CAPTCHA_ATTEMPTS):
        try:
            uid, captcha_b64 = await _fetch_captcha(client)
        except httpx.TimeoutException:
            await _close_session_client(sess)
            return _fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
        except Exception:  # noqa: BLE001
            await _close_session_client(sess)
            return _fail(502, "upstream_error", "获取验证码失败，请稍后重试")
        sess.captcha_uid = uid

        try:
            answer = _captcha_solve(base64.b64decode(captcha_b64))
        except Exception:  # noqa: BLE001
            answer = None
        if not answer:
            continue

        ok, detail = await _dorm_login(sess, client, answer, cas_only=bridge)
        if ok:
            _mark_login(username)
            await _store_session(sess)
            if bridge:
                return {"session_id": sess.sid, "ok": True, "mode": "bridge"}
            return {"session_id": sess.sid, "ok": True}

        if detail in ("bad_credential", "locked"):
            _mark_login(username)
            await _close_session_client(sess)
            if detail == "bad_credential":
                return _fail(401, "bad_credential", "学号或密码不正确")
            return _fail(423, "locked", "账号已被临时锁定，请稍后再试")
        if detail.startswith("vpn:") or detail.startswith("platform:"):
            # 票拿到了但内网链路没通：这次登录不算数，不留孤儿会话。
            _mark_login(username)
            await _close_session_client(sess)
            return _fail(502, "upstream_error", f"内网通道建立失败：{detail}")
        # 验证码没识别 / 没答对 → 换一张继续。

    # 自动识别没成：给用户一张干净的验证码人工回填。
    try:
        uid, captcha_b64 = await _fetch_captcha(client)
        sess.captcha_uid = uid
    except Exception:  # noqa: BLE001
        await _close_session_client(sess)
        return _fail(502, "upstream_error", "获取验证码失败，请稍后重试")

    _mark_login(username)
    await _store_session(sess)
    if bridge:
        return {"session_id": sess.sid, "captcha_b64": captcha_b64, "mode": "bridge"}
    return {"session_id": sess.sid, "captcha_b64": captcha_b64}


@router.post("/api/v1/campus/dorm/session/submit")
async def session_submit(request: Request) -> Any:
    """回填人工验证码，继续完成登录。

    请求体：``{"session_id": "...", "code": "abcd"}``
    响应：``{"session_id": "...", "ok": true}`` 或错误信封。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，电费查询暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not sid or not code:
        return _fail(400, "missing_field", "缺少 session_id 或验证码")

    async with _lock:
        await _purge_expired()
        sess = _sessions.get(sid)
    if sess is None or sess.stage != "pending":
        return _fail(401, "session_expired", "登录已过期，请重新登录")

    client = _session_client(sess)
    ok, detail = await _dorm_login(
        sess, client, code, cas_only=(sess.mode == "bridge")
    )
    if ok:
        _mark_login(sess.username)
        await _store_session(sess)
        if sess.mode == "bridge":
            return {"session_id": sid, "ok": True, "mode": "bridge"}
        return {"session_id": sid, "ok": True}

    if detail in ("bad_credential", "locked"):
        _mark_login(sess.username)
        async with _lock:
            gone = _sessions.pop(sid, None)
            if gone is not None:
                _forget_user_session(gone.username, sid)
        await _close_session_client(sess)
        if detail == "bad_credential":
            return _fail(401, "bad_credential", "学号或密码不正确")
        return _fail(423, "locked", "账号已被临时锁定，请稍后再试")

    if detail.startswith("vpn:") or detail.startswith("platform:"):
        # 票拿到了但内网链路没通：这次会话没救，别把它留成「就绪」状态。
        _mark_login(sess.username)
        async with _lock:
            gone = _sessions.pop(sid, None)
            if gone is not None:
                _forget_user_session(gone.username, sid)
        await _close_session_client(sess)
        return _fail(502, "upstream_error", f"内网通道建立失败：{detail}")

    # 验证码答错：留在 pending，让用户再试一次。
    return _fail(400, "captcha_failed", "验证码不正确，请重试")


@router.post("/api/v1/campus/dorm/query")
async def query(request: Request) -> Any:
    """查电费（余额 / 剩余电量）。

    请求体：``{"session_id": "...", "feeitemid": 448, "campus": "1",
    "building": "A7&李园贰號A7", "room": "211"}``
    响应：``{"ok": true, "code": "200", "map": {...}}``（``map`` 是上游原样数据，
    解析留给客户端）。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，电费查询暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        return _fail(400, "missing_field", "缺少 session_id")
    feeitemid = payload.get("feeitemid")
    campus = str(payload.get("campus") or "")
    building = str(payload.get("building") or "")
    room = str(payload.get("room") or "")
    if feeitemid in (None, ""):
        return _fail(400, "missing_field", "缺少 feeitemid")

    try:
        sess = await _require_session(sid)
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)

    try:
        async with sess.request_lock:
            token = await _ensure_token(sess)
            result = await _query_third_data(
                sess, token, str(feeitemid), campus, building, room
            )
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)
    except httpx.TimeoutException:
        return _fail(504, "upstream_timeout", "连接学校系统超时，请稍后重试")
    except Exception as exc:  # noqa: BLE001
        return _fail(502, "upstream_error", f"电费查询失败：{type(exc).__name__}")

    await _touch(sid)
    return {"ok": True, "code": "200", "map": result}


@router.post("/api/v1/campus/dorm/recharge-url")
async def recharge_url(request: Request) -> Any:
    """电费充值页地址（浏览器新标签页打开）。

    请求体：``{"session_id": "...", "feeitemid": 448}``
    响应：``{"ok": true, "url": "...", "gateway_login_url": "...",
    "expires_in": 300}``

    浏览器有自己的 cookie 罐，拿不到服务端的网关票据 —— 所以额外给一个
    ``gateway_login_url``：让浏览器先用自己的 ST 在网关登录一次。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，充值暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    feeitemid = payload.get("feeitemid")
    if not sid:
        return _fail(400, "missing_field", "缺少 session_id")
    if feeitemid in (None, ""):
        return _fail(400, "missing_field", "缺少 feeitemid")

    try:
        sess = await _require_session(sid)
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)

    try:
        async with sess.request_lock:
            token = await _ensure_token(sess)
            client = _session_client(sess)
            # 浏览器要在**网关 origin** 下拿到自己的网关票据 → 现铸一份 ST。
            st = await webvpn.cas_st(client, sess.tgt, webvpn.WEBVPN_LOGIN_SERVICE)
            payment = quote(PLATFORM_BASE + "/plat", safe="")
            raw = (
                f"{PLATFORM_CHARGE_APP}?name=pays&id={quote(str(feeitemid), safe='')}"
                f"&paymentUrl={payment}&token={quote(token, safe='')}"
            )
            url = webvpn.wrap_url(raw)
            gateway_login_url = (
                f"{webvpn.GATEWAY}/login?cas_login=true&ticket={quote(st, safe='')}"
            )
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)
    except webvpn.CasError as exc:
        return _fail(401, "unauthorized", webvpn.redact(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _fail(502, "upstream_error", f"生成充值地址失败：{type(exc).__name__}")

    await _touch(sid)
    return {
        "ok": True,
        "url": url,
        "gateway_login_url": gateway_login_url,
        "expires_in": RECHARGE_URL_TTL,
    }


@router.post("/api/v1/campus/dorm/card-recharge-url")
async def card_recharge_url(request: Request) -> Any:
    """一卡通充值地址（两步：先预热建平台会话，再进充值页）。

    请求体：``{"session_id": "..."}``
    响应：``{"ok": true, "warmup_url": "...", "url": "...", "gateway_login_url": "..."}``

    浏览器有自己的 cookie 罐，拿不到服务端的网关票据 —— 所以额外给一个
    ``gateway_login_url``：让浏览器先用自己的 ST 在网关登录一次。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，充值暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        return _fail(400, "missing_field", "缺少 session_id")

    try:
        sess = await _require_session(sid)
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)

    try:
        async with sess.request_lock:
            client = _session_client(sess)
            ticket = await _fresh_platform_ticket(sess)
            if not ticket:
                raise DormError(401, "unauthorized", "平台票据获取失败，请重新登录")
            target = quote(PLATFORM_CARD_RECHARGE, safe="")
            warmup_raw = (
                f"{PLATFORM_CARD_LOGIN}&ticket={quote(ticket, safe='')}"
                f"&targetUrl={target}"
            )
            warmup_url = webvpn.wrap_url(warmup_raw)
            url = webvpn.wrap_url(PLATFORM_CARD_RECHARGE)
            # 浏览器要在**网关 origin** 下拿到自己的网关票据 → 现铸一份 ST。
            st = await webvpn.cas_st(client, sess.tgt, webvpn.WEBVPN_LOGIN_SERVICE)
            gateway_login_url = (
                f"{webvpn.GATEWAY}/login?cas_login=true&ticket={quote(st, safe='')}"
            )
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)
    except webvpn.CasError as exc:
        return _fail(401, "unauthorized", webvpn.redact(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _fail(502, "upstream_error", f"生成充值地址失败：{type(exc).__name__}")

    await _touch(sid)
    return {
        "ok": True,
        "warmup_url": warmup_url,
        "url": url,
        "gateway_login_url": gateway_login_url,
    }


@router.post("/api/v1/campus/dorm/session/refresh")
async def session_refresh(request: Request) -> Any:
    """探活 + 静默续期：免验证码、免账密地维持登录态。

    请求体：``{"session_id": "..."}``
    响应：``{"ok": true, "refreshed": true|false}``
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，电费查询暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        return _fail(400, "missing_field", "缺少 session_id")

    try:
        sess = await _require_cas_session(sid)
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)

    if sess.stage == "cas":
        # 桥接会话：服务端没跑网关/平台，没有「续期」这回事 —— 它只需要 TGT
        # 能继续铸票。TGT 死了会在 bridge-ticket 上以 401 暴露出来。
        await _touch(sid)
        return {"ok": True, "refreshed": False}

    refreshed = False
    try:
        async with sess.request_lock:
            client = _session_client(sess)
            # ① 网关票据还在吗？探一次内网免鉴权接口：回 JSON 才是活的。
            #    走 `_gateway_fetch`：探测途中被弹回 /login 时它会自动重铸一次，
            #    而且收回来的 cookie 会按网关 / 平台分罐存好（临时罐会被丢掉，
            #    重铸出来的新票据就白拿了）。
            alive = False
            if sess.tgt:
                before = dict(sess.gateway_cookies)
                try:
                    status, _u, _h, content = await _gateway_fetch(
                        sess, webvpn.PLATFORM_PROBE_URL
                    )
                    body = content.decode("utf-8", "replace")
                    alive = status < 500 and not body.lstrip().startswith("<")
                except DormError:
                    alive = False
                except Exception:  # noqa: BLE001
                    alive = False
                if sess.gateway_cookies != before:
                    # 票据被换过了（重铸生效）→ 算一次续期。
                    refreshed = True
            if not alive and sess.tgt:
                try:
                    await webvpn.webvpn_login(client, sess.gateway_cookies, sess.tgt)
                    refreshed = True
                except Exception:  # noqa: BLE001
                    pass

            # ② 平台 token 还有效吗？只有「明确未授权」才重铸，其它错误不折腾。
            token_alive = False
            if sess.platform_token:
                try:
                    await _query_third_data(sess, sess.platform_token, "448", "", "", "")
                    token_alive = True
                except DormError as exc:
                    token_alive = exc.code != "unauthorized"
                except Exception:  # noqa: BLE001
                    token_alive = True
            if not token_alive:
                sess.platform_token = ""
                try:
                    await _platform_login(sess)
                    refreshed = True
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    await _touch(sid)
    return {"ok": True, "refreshed": refreshed}


@router.post("/api/v1/campus/dorm/session/ping")
async def session_ping(request: Request) -> Any:
    """探活：校验本地留存的 session_id 是否仍然可用。

    ⚠️ 桥接模式（``mode=bridge``）的会话 stage 是 ``cas``：服务端没跑网关/平台，
    但它**能铸票**，浏览器侧照样能查电费。所以这里放行 ``cas`` 与 ``ready``
    两种 stage —— 否则 Web 端每次查询前探活都会拿到 401，把好会话当成过期清掉。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，电费查询暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        return _fail(400, "missing_field", "缺少 session_id")

    try:
        await _require_cas_session(sid)
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)

    await _touch(sid)
    return {"ok": True}


@router.post("/api/v1/campus/dorm/session/logout")
async def session_logout(request: Request) -> Any:
    """注销本端会话（不调学校 CAS 登出，只清服务端内存状态）。"""
    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    async with _lock:
        sess = _sessions.pop(sid, None)
        if sess is not None:
            _forget_user_session(sess.username, sid)
    if sess is not None:
        sess.platform_token = ""
        sess.tgt = ""
        sess.encrypted_password = ""
        sess.gateway_cookies.clear()
        sess.platform_cookies.clear()
        await _close_session_client(sess)
    return {"ok": True}


# ---------------------------------------------------------------- 桥接模式
#
# 服务器出口 IP（42.192.52.117）被 WebVPN 网关 403，内网慧新平台只有用户
# 自己的浏览器能读到。桥接模式的做法：
#
#   1. 客户端用 ``mode=bridge`` 登录 → 服务端只做 CAS 登录，拿到 TGT；
#   2. 客户端调 ``bridge-ticket`` → 服务端用 TGT 铸两张一次性 ST
#      （网关票 + 平台票），连同内网地址的密文一起交给浏览器；
#   3. 浏览器先顶层打开网关登录页（``{GW}/login?cas_login=true&ticket=st_gw``）
#      拿到网关票据 cookie，再导航到 ``{GW}/https/<hex(控制域名)>/…/bridge#参数``；
#      网关会把控制服务端的页面代理出来，浏览器 origin 变成网关，
#      于是页面里的 fetch 与内网平台同源、带得上 cookie；
#   4. 桥页跑完链路后 ``postMessage`` 回执给 opener。
#
# ⚠️ 网关票据 cookie 的 Set-Cookie **没有 SameSite**（默认 Lax），第三方 iframe
# 带不上，所以必须是顶层命名窗口，不能 iframe。


@router.post("/api/v1/campus/dorm/bridge-ticket")
async def bridge_ticket(request: Request) -> Any:
    """铸桥接用的一次性票据（网关票 + 平台票）。

    请求体：``{"session_id": "..."}``

    响应字段：

    - ``gateway_origin``：网关 origin，例如 ``https://webvpn.cwxu.edu.cn``；
    - ``bridge_path``：**网关上的路径**（不是控制端自己的路径）。客户端要拼成
      ``{gateway_origin}{bridge_path}`` 才能打开桥页，即
      ``https://webvpn.cwxu.edu.cn/https/<hex(控制域名)>/api/v1/campus/dorm/bridge``；
      开头必然是 ``/https/``（网关用「协议 + 密文主机」表示被代理的站点）；
    - ``st_gw``：网关票，客户端先拿它开
      ``{gateway_origin}/login?cas_login=true&ticket=st_gw`` 种网关票据 cookie；
    - ``st_plat``：平台票，一次性，桥页用它换 access_token；
    - ``hex_plat``：内网平台主机（``10.3.100.110``）的密文，桥页用它拼内网地址；
    - ``expires_in``：提示用的有效期（秒），票据本身极短命。

    ⚠️ 票据一律**不进日志、不进异常文案**（用 :func:`webvpn.redact` 脱敏），
    也**不能放进 URL 的 query**（会进 nginx 访问日志）——只能走 fragment。

    票据一次性、极短命，所以每次调用都现铸。
    """
    if httpx is None:
        return _fail(503, "proxy_unavailable", "服务器缺少 httpx，桥接暂不可用")

    payload = await _body(request)
    sid = str(payload.get("session_id") or "").strip()
    if not sid:
        return _fail(400, "missing_field", "缺少 session_id")

    try:
        sess = await _require_cas_session(sid)
    except DormError as exc:
        return _fail(exc.status, exc.code, exc.message)

    client = _session_client(sess)
    try:
        st_gw = await webvpn.cas_st(
            client, sess.tgt, webvpn.WEBVPN_LOGIN_SERVICE
        )
        st_plat = await webvpn.cas_st(client, sess.tgt, PLATFORM_CAS_SERVICE)
    except webvpn.CasError as exc:
        return _fail(401, "unauthorized", webvpn.redact(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _fail(502, "upstream_error", f"铸造票据失败：{type(exc).__name__}")

    if not st_gw or not st_plat:
        return _fail(502, "upstream_error", "铸造票据失败：票据为空")

    await _touch(sid)
    return {
        "ok": True,
        "gateway_origin": webvpn.GATEWAY,
        # 网关上的路径：客户端拼 {gateway_origin}{bridge_path}。
        "bridge_path": f"/https/{webvpn.encrypt_host(CONTROL_HOST)}{BRIDGE_PATH}",
        "st_gw": st_gw,
        "st_plat": st_plat,
        "hex_plat": webvpn.encrypt_host(PLATFORM_HOST),
        "expires_in": RECHARGE_URL_TTL,
    }


@router.get("/api/v1/campus/dorm/bridge")
async def bridge() -> HTMLResponse:
    """桥页：浏览器里的「代理客户端」，替服务端去读内网电费。

    - 页面**不需要登录**（它由网关代理，浏览器 origin 已是网关）；
    - 所有票据、房间号等参数都从 ``location.hash`` 读 —— URL 的 fragment
      不会进 nginx 访问日志；
    - 结果通过 ``window.opener.postMessage`` 回给开窗的页面，然后关窗。
    """
    return HTMLResponse(
        content=_render_bridge_html(),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
