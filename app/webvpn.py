"""WebVPN（深澜 Srun）网关封装：主机名加密、URL 包装、换票登录、逐跳请求。

## 为什么单独一个模块

学校 WebVPN 网关（https://webvpn.cwxu.edu.cn）是校外访问内网 10.3.100.110 的
**唯一出口**：把目标主机名用 AES-128-CFB 加密成 hex 拼进 URL 路径段，网关解密后
代发内网请求。安卓端 `lib/services/campus/webvpn.dart` 已经跑通同一条链路，服务端
必须**逐字节复现**它的加密与包装规则，否则网关解不开地址。

## 三条必须守住的规矩（全部来自安卓端 `campus_session.dart` 的注释）

1. **原始地址与实际请求地址分离**：相对 `Location` 只能相对**未包装**的原始地址
   解析，否则 `/http/<hex>` 前缀会被吃掉、退化成网关根路径（VPN 换票链断在这一跳）。
2. **302 之后退化为 GET**：表单 / 正文只挂第一跳。
3. **每跳都要捕获 `Set-Cookie`**：CAS / 网关 / 平台的会话 cookie 都藏在 302 里。

## 票据判定看响应体、不看状态码

实测无效票据时网关返回 **HTTP 200 + 一段错误 HTML**（"无法解析SSO相应的XML内容"），
所以「请求成功」不等于「登录成功」；反过来网关票据过期时返回 302 → `/login`。
两种情况都要单独判。

## 与既有模块的边界

- 只**只读**复用 `campus_proxy` 的 cookie 工具与 UA，不改它的任何行为。
- 票据 / token 一律不进日志（本模块的异常消息已经过 `redact()` 脱敏）。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from .campus_proxy import _UA, _capture_cookies, _cookie_header

# cryptography >= 49 把 CFB 挪到了 decrepit；旧版本还在 primitives 下。
try:  # pragma: no cover - 取决于 cryptography 版本
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.modes import CFB
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

# ---------------------------------------------------------------- 常量

#: WebVPN 网关（公网 218.90.181.98），校外访问内网的唯一出口。
GATEWAY = "https://webvpn.cwxu.edu.cn"
GATEWAY_HOST = "webvpn.cwxu.edu.cn"

#: 深澜默认密钥：key 与 IV 相同（安卓端 webvpn.dart:26）。
SECRET = b"wrdvpnisthebest!"

#: 学校 CAS（公网），TGT / ST 都在这里。
CAS_BASE = "https://wxcas.cwxu.edu.cn"

#: CAS 给 WebVPN 注册的 service，必须字节级一致（安卓 webvpn.dart:33）。
WEBVPN_LOGIN_SERVICE = f"{GATEWAY}/login?cas_login=true"

#: 内网平台（慧新 E 校）地址。服务端本身**不可达**，只能经网关。
PLATFORM_BASE = "http://10.3.100.110"

#: 内网免鉴权探测接口：登录态是否有效看它回 JSON 还是回登录页 HTML。
PLATFORM_PROBE_URL = f"{PLATFORM_BASE}/berserker-app/frontInfo?synAccessSource=mobile"

#: 逐跳上限，与安卓端 `campus_session.dart` 的 12 跳一致。
MAX_HOPS = 12

#: 平台票逐跳查找的跳数上限（安卓 dorm_service.locatePlatformTicket 同口径）。
MAX_TICKET_HOPS = 6


class WebVpnError(Exception):
    """网关换票 / 登录失败（消息已脱敏，可直接进日志）。"""


class CasError(Exception):
    """CAS 换票失败（消息已脱敏，可直接进日志）。"""


# ---------------------------------------------------------------- 脱敏

_REDACT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"TGT-[A-Za-z0-9_\-]+"), "TGT-***"),
    (re.compile(r"ST-[A-Za-z0-9_\-]+"), "ST-***"),
    (re.compile(r"(?i)(ticket=)[^&\s\"'<>]+"), r"\1***"),
    (re.compile(r"(?i)(token=)[^&\s\"'<>]+"), r"\1***"),
    (re.compile(r"(?i)(wengine_vpn_ticket[^=]*=)[^;\s]+"), r"\1***"),
    (re.compile(r"(?i)(access[_-]?token[\"'\s:=]+)[A-Za-z0-9_\-\.]+"), r"\1***"),
)


def redact(text: str) -> str:
    """把 TGT / ST / ticket / token 换成 ``***``。

    R7：票据进了日志就等于泄漏（URL 里的 token 会出现在浏览器历史 / Referer /
    服务端日志里）。本模块所有异常消息在抛出前都过一遍这里。
    """
    out = text or ""
    for pattern, repl in _REDACT_RULES:
        out = pattern.sub(repl, out)
    return out


# ---------------------------------------------------------------- 主机名加密与 URL 包装


def encrypt_host(host: str) -> str:
    """主机名 → ``hex(key) + hex(CFB128 密文)``。

    AES-128-CFB128（流式、无 padding），key 与 IV 都是深澜默认密钥。实现照抄规格
    §3.3，与安卓端 `WebVpn.encryptHost` 逐字节一致（6 条 golden vector 可回归）。
    """
    enc = Cipher(algorithms.AES(SECRET), CFB(SECRET)).encryptor()
    ct = enc.update(host.encode()) + enc.finalize()
    return SECRET.hex() + ct.hex()


def needs_wrap(url: str) -> bool:
    """是否**内网地址**（只有内网才需要走网关，公网地址直连）。

    规则与安卓端 `WebVpn.needsWrap` 一致：必须是 4 段 IPv4，且落在
    10.x / 192.168.x / 172.16-31.x；网关自身不再包装。
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host or host == GATEWAY_HOST:
        return False
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        a = int(parts[0])
        b = int(parts[1])
    except ValueError:
        return False
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


def wrap_url(url: str) -> str:
    """把内网 URL 包成网关 URL（**幂等**）。

    结果形如 ``https://webvpn.cwxu.edu.cn/http/<hexHost>/path?query``；
    端口非默认时段名带 ``-port``（如 ``http-8080``）；根路径补 ``/``。
    已经是网关地址（或公网地址）时原样返回 —— 每跳重定向都会再调一次，幂等是刚需。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.netloc == urlsplit(GATEWAY).netloc:
        return url  # 幂等：已经是网关地址
    if not needs_wrap(url):
        return url  # 公网地址直连，不走网关
    scheme = parts.scheme or "http"
    try:
        port = parts.port
    except ValueError:
        port = None
    default_port = 443 if scheme == "https" else 80
    seg = f"{scheme}-{port}" if port is not None and port != default_port else scheme
    host = parts.hostname or ""
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    frag = f"#{parts.fragment}" if parts.fragment else ""
    return f"{GATEWAY}/{seg}/{encrypt_host(host)}{path}{query}{frag}"


# ---------------------------------------------------------------- CAS 换票


async def cas_st(client: Any, tgt: str, service: str) -> str:
    """用 CAS 的 TGT 换 ST。

    等价于安卓端 `cas_client.dart:205-219` / `webvpn.dart:142-143` 的 POST 段。
    响应体就是票据本身（``ST-...``），个别部署会回 JSON，两种都收。
    """
    try:
        resp = await client.post(
            f"{CAS_BASE}/lyuapServer/v1/tickets/{tgt}",
            data={"service": service},
            headers={"Referer": f"{CAS_BASE}/"},
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成 CasError
        raise CasError(f"ST 铸造失败：{type(exc).__name__}") from exc
    body = (resp.text or "").strip()
    if body.startswith("ST-"):
        return body
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {}
    st = ""
    if isinstance(data, dict):
        st = str(data.get("ticket") or "")
        if not st and isinstance(data.get("data"), dict):
            st = str(data["data"].get("ticket") or "")
    if st.startswith("ST-"):
        return st
    # 500 的响应体里通常有"TGT 已失效"之类的说明，但含 TGT，必须脱敏。
    raise CasError(f"ST 铸造失败：HTTP {resp.status_code} {redact(body[:120])}")


# ---------------------------------------------------------------- 网关登录


def _login_failed(body: str) -> bool:
    """网关登录页的错误文本（无效票据时 HTTP 200 也会带这段话）。"""
    if not body:
        return False
    return "无法解析SSO相应的XML内容" in body or "登录失败" in body


def _is_gateway_login(location: str) -> bool:
    """Location 是否指向网关自己的 ``/login``（= 票据过期）。"""
    try:
        parts = urlsplit(urljoin(GATEWAY + "/", location))
    except ValueError:
        return False
    return (parts.hostname or "").lower() == GATEWAY_HOST and parts.path.startswith("/login")


async def _follow_login_chain(client: Any, jar: dict[str, str], first: Any) -> None:
    """网关回跳后的 302 链：只跟网关自己的跳转，逐跳把 cookie 收进罐。

    为什么还要跟：网关可能在后续 302 里才下发 / 覆盖 ``wengine_vpn_ticket…``。
    只跟网关域内的跳转，避免把 CAS 站点的 cookie 混进网关罐。
    """
    location = first.headers.get("location")
    seen: set[str] = set()
    hops = 0
    while location and hops < 5:
        target = urljoin(GATEWAY + "/", location)
        if target in seen:
            break
        seen.add(target)
        try:
            parts = urlsplit(target)
        except ValueError:
            break
        if (parts.hostname or "").lower() != GATEWAY_HOST:
            break
        resp = await client.get(target, follow_redirects=False)
        _capture_cookies(resp.headers, jar)
        if _login_failed(resp.text or ""):
            raise WebVpnError("VPN换票失败")
        if resp.status_code < 300 or resp.status_code >= 400:
            break
        location = resp.headers.get("location")
        hops += 1


async def webvpn_login(client: Any, jar: dict[str, str], tgt: str) -> None:
    """用 TGT 登录网关（种 cookie → 换 ST → 回跳 → 校验落点）。

    失败抛 :class:`WebVpnError`（消息已脱敏）。成功时网关票据在 ``jar`` 里。
    """
    # ① 先访问网关根，让网关下发初始票据 cookie（回跳必须带上）。
    try:
        g0 = await client.get(GATEWAY + "/", follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        raise WebVpnError(f"VPN不可达：{type(exc).__name__}") from exc
    _capture_cookies(g0.headers, jar)

    # ② TGT 换 WebVPN 的 ST（service 必须是网关注册的原值）。
    st = await cas_st(client, tgt, WEBVPN_LOGIN_SERVICE)

    # ③ 带 ST 回跳网关：登录态靠同名 cookie 覆盖。
    url = f"{GATEWAY}/login?cas_login=true&ticket={quote(st, safe='')}"
    try:
        resp = await client.get(url, headers={"Referer": f"{CAS_BASE}/"}, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001
        raise WebVpnError(f"VPN回跳异常：{type(exc).__name__}") from exc
    _capture_cookies(resp.headers, jar)
    if _login_failed(resp.text or ""):
        raise WebVpnError("VPN换票失败")
    await _follow_login_chain(client, jar, resp)

    # ④ 真实验证：内网免鉴权接口必须回 JSON，而不是登录页 HTML。
    status, _url, _headers, content = await vpn_fetch(client, jar, PLATFORM_PROBE_URL)
    text = content.decode("utf-8", "replace")
    if status >= 500:
        raise WebVpnError(f"VPN验证失败：HTTP {status}")
    if text.lstrip().startswith("<"):
        raise WebVpnError("VPN未登录")


# ---------------------------------------------------------------- 逐跳请求


def _merge_headers(base: dict[str, str], extra: dict[str, str] | None) -> dict[str, str]:
    """合并请求头：``extra`` 覆盖 ``base``，大小写不敏感。

    为什么要去重：httpx 的 ``Headers`` 允许同时存在 ``Cookie`` 与 ``cookie``，
    两份都发出去会让服务端拿到两个 Cookie 头。合并前先把 ``base`` 里同名
    （忽略大小写）的键删掉。
    """
    merged = dict(base)
    for key, value in (extra or {}).items():
        for old in list(merged):
            if old.lower() == key.lower():
                merged.pop(old)
        merged[key] = value
    return merged


async def vpn_fetch(
    client: Any,
    jar: dict[str, str],
    raw_url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    body: bytes | None = None,
):
    """经网关请求一个**未包装**的内网地址。

    ``jar`` 是本会话的 cookie 罐：既用来发 ``Cookie`` 头，也用来收 ``Set-Cookie``
    （每跳都收）。

    返回 ``(status, url, headers, body_bytes)``。其中：

    - ``url`` 是**实际请求**的地址（包装后），不是原始地址；
    - 网关票据过期时**不**继续往登录页跑，而是把 302 原样返回，交给调用方
      （调用方重铸票据后重试一次）。

    三条硬规矩：每跳重新包装 URL；相对 Location 相对**未包装**的原始地址解析；
    302 之后退化为 GET、表单 / 正文只挂第一跳。
    """
    raw = raw_url
    hop_method = method
    for hop in range(MAX_HOPS):
        target = wrap_url(raw)
        # 先铺默认头，再让调用方的头覆盖（调用方可以合并网关罐与平台罐的
        # cookie 一起发）。**Cookie 键永远存在**（哪怕空串）——
        # webproxy.py:293-300 的教训。
        h = _merge_headers(
            {
                "User-Agent": _UA,
                "Referer": GATEWAY + "/",
                "Cookie": _cookie_header(jar),
            },
            headers,
        )
        resp = await client.request(
            hop_method,
            target,
            headers=h,
            data=form if hop == 0 else None,
            content=body if hop == 0 else None,
            follow_redirects=False,
        )
        _capture_cookies(resp.headers, jar)
        location = resp.headers.get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            if _is_gateway_login(location):
                # 网关票据过期：交回调用方去重铸，别把登录页 HTML 当结果。
                return resp.status_code, str(resp.url), resp.headers, resp.content
            # 关键：相对**原始（未包装）**地址解析，否则 /http/<hex> 前缀被吃掉。
            raw = urljoin(raw, location)
            hop_method = "GET"
            continue
        return resp.status_code, str(resp.url), resp.headers, resp.content
    raise TimeoutError("重定向跳数超限")


def ticket_param(url: str) -> str:
    """取 URL 的 ``ticket`` 查询参数（只解一次百分号编码）。"""
    if not url:
        return ""
    try:
        query = urlsplit(url).query
    except ValueError:
        return ""
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == "ticket":
            return value
    return ""


async def locate_platform_ticket(
    client: Any,
    jar: dict[str, str],
    start_url: str,
    max_hops: int = MAX_TICKET_HOPS,
) -> str:
    """逐跳找**平台自己**的票（票只出现在中间那一跳）。

    与安卓端 `DormService.locatePlatformTicket` 同口径：票从当前（未包装）URL 的
    ``ticket`` 参数取，**忽略以 ``ST-`` 开头**的票（那是 CAS 的 ST，不是平台票）。
    找不到返回空串（调用方按安卓端的做法退回用 ST 换 token）。
    """
    raw = start_url
    for _hop in range(max_hops):
        ticket = ticket_param(raw)
        target = wrap_url(raw)
        h = {"User-Agent": _UA, "Referer": GATEWAY + "/", "Cookie": _cookie_header(jar)}
        resp = await client.request("GET", target, headers=h, follow_redirects=False)
        _capture_cookies(resp.headers, jar)
        if not ticket:
            ticket = ticket_param(str(resp.url))
        if ticket and not ticket.startswith("ST-"):
            return ticket
        location = resp.headers.get("location")
        if not location:
            break
        raw = urljoin(raw, location)
        ticket = ticket_param(raw)
        if ticket and not ticket.startswith("ST-"):
            return ticket
    return ""
