"""Web 端抓取代理：替浏览器完成对校园系统的一次 HTTP 往返。

## 为什么需要它

浏览器受同源策略约束：任何没有 `Access-Control-Allow-Origin` 头的第三方站点，
JavaScript 一律读不到响应体（Chrome 报 "blocked by CORS policy"，
WebKit 报 "not allowed by Access-Control-Allow-Origin. Status code: 200"
—— 服务器明明回了 200，浏览器直接丢弃）。

学校那批站点（教务 `jwgl` / CAS `wxcas` / 融合门户 `my` / 资讯 `www`·`jwc`·
`xgc`·`tw` / 实验课 `sygl`）**一个 CORS 头都没有**，而且我们是外人，
不可能去改它们的响应头。所以 Web 版要想工作，只能由服务端代发。

## 硬规矩（改之前先想清楚）

1. **无状态**：本模块不保存任何 cookie / 会话 / 凭据。
   客户端带什么 cookie 就转发什么，响应回来原样交回。
   服务端内存里**不残留**任何会话 —— 多用户之间也不可能串台。

   ⚠️ 这条规矩**必须靠代码强制**，不能靠「我没写就算没有」：
   `httpx.AsyncClient` 自带一个进程级 cookie jar，而我们是全进程共用一个 client。
   只要请求里没带 `cookie` 头，httpx 就会把**上一个用户**的 `route` / `JSESSIONID`
   自动塞进去。后果有两层：
   - **串号**：A 的教务会话被发给了 B；
   - **CAS 断链**（2026-11-10 真实事故）：教务站点看到 `route` 已存在就不再下发
     `Set-Cookie: route=...`，而 CAS 的子系统会话正是靠这一个 302 里的 cookie 建立的，
     于是 `ssoTo()` 拿不到 `Set-Cookie` → 落地页 302 回登录页 → 用户看到「账密登录全失败」。
     实测：`cookie=''` 时服务器回 `route=800b8c57...`；不给 cookie 头时 jar 塞进旧值，
     服务器回 `set-cookie: []`。
   所以 `webproxy_fetch` 里**永远**给 `headers` 补一个 `cookie` 键（空串也算），
   并且每请求清空 `client.cookies` —— 双保险，见该函数内的注释。

2. **白名单**：只放行 `*.cwxu.edu.cn`，其余一律 403。
   没有白名单的话，这个端点就是一台公开的 SSRF 跳板 ——
   任何人都能用它打内网、打云元数据服务（169.254.169.254）。

3. **防内网**：白名单域名解析出的 IP 若落在私网 / 环回 / 链路本地，
   同样拒绝。白名单只约束了「名字」，约束不了「DNS 解析结果」。

4. **不解析内容**：原样转发字节。不做 HTML 改写、不补 base64、不重排 JSON。
   页面是 GB2312 还是 UTF-8 由客户端自己判断（它本来就要处理这件事）。

5. **有上限**：响应体、超时全部封顶，防止被当成无限下载器。

## 与 `api.py` 的关系

完全独立：本模块自建 `APIRouter`，不 import `api.py` 的任何东西，
也不改它的任何一行。`main.py` 里只是多一行 `include_router`。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------- 依赖
#
# ⚠️ 这里**绝不能**写成模块顶层的 `import httpx`。
#
# 2026-11-10 实测教训：服务器 venv 里当时没有 httpx，顶层 import 直接
# `ModuleNotFoundError` → uvicorn 加载 app 失败 → **整个控制端起不来**，
# 而控制端还承担着安卓 App 的版本检查、消息、资源等路由。
# 一个可选依赖的缺失，不该把跟它无关的功能一起拖死。
#
# 现在：缺 httpx 时应用照常启动，只有 `/webproxy/*` 这两个端点返回 503。
# 部署脚本负责 `pip install`，本模块只负责「不在就明说」。
try:  # noqa: SIM105
    import httpx  # type: ignore
except Exception:  # noqa: BLE001 —— 任何导入失败都只降级，不抛出
    httpx = None  # type: ignore[assignment]

router = APIRouter()

# ---------------------------------------------------------------- 常量

#: 允许代理的站点后缀。只放校园域名 —— 控制端自己的域名由客户端直连，不必绕这一圈。
ALLOWED_SUFFIXES: tuple[str, ...] = ('.cwxu.edu.cn',)

#: 单次响应体上限（字节）。教务公告页约 19KB，PDF 课表最大见过 2MB；
#: 8MB 留足余量，同时挡住「拿这个端点下大文件」的用法。
MAX_BODY_BYTES = 8 * 1024 * 1024

#: 单次请求墙钟超时（秒）。客户端 `CampusSession` 的单跳超时是 25 秒，
#: 这里略短一点，让「代理先超时」而不是「客户端先超时」——
#: 前者能带回一个可读的错误码，后者只有一个裸的 TimeoutException。
REQUEST_TIMEOUT = 20.0

#: 允许透传的请求头。清单式而不是黑名单式：多转发一个头就多一份责任
#: （比如 `Host` 被改写会让某些站点的虚拟主机路由错乱）。
ALLOWED_REQUEST_HEADERS: tuple[str, ...] = (
    'cookie',
    'referer',
    'origin',
    'content-type',
    'accept',
    'accept-language',
    'x-requested-with',
)

#: 会被回传的响应头。`set-cookie` 必须逐个返回（不能合并成 ", "），
#: 否则带 `Expires=Wed, 21 Oct...` 的 cookie 会被逗号切碎 —— 这正是
#: 客户端宁可用 `dart:io` 而不用 `package:http` 的原因。
ALLOWED_RESPONSE_HEADERS: tuple[str, ...] = (
    'set-cookie',
    'content-type',
    'location',
    'content-disposition',
    'content-encoding',
)

#: 简易限流：每 IP 每分钟允许的请求数。
#: 不是安全边界（这个端点的价值在于白名单 + 防内网），只是防止单个客户端
#: 开着循环把服务器打满。
RATE_LIMIT_PER_MINUTE = 240

_hits: dict[str, list[float]] = {}
_hits_lock = asyncio.Lock()


# ---------------------------------------------------------------- 工具


def _fail(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={'error': {'code': code, 'message': message}},
    )


def _host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https'):
        return None
    host = (parts.hostname or '').lower()
    return host or None


def _host_allowed(host: str) -> bool:
    return any(host == suf.lstrip('.') or host.endswith(suf) for suf in ALLOWED_SUFFIXES)


async def _resolves_to_private(host: str) -> bool:
    """域名解析结果里只要有一个私网地址就拒绝。

    为什么要查：白名单只约束「名字」，不约束 DNS。若某个子域被人挂到
    `10.x` / `127.0.0.1` / `169.254.169.254`（云元数据服务），
    单靠后缀白名单就等于开了一扇内网门。
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, UnicodeError):
        # 解析不出来 → 让它去请求阶段自然失败，不在这里下结论
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return True
    return False


async def _rate_ok(ip: str) -> bool:
    now = time.monotonic()
    async with _hits_lock:
        bucket = _hits.setdefault(ip, [])
        cutoff = now - 60.0
        # 顺手把过窗的记录剔掉，避免字典无限增长
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= RATE_LIMIT_PER_MINUTE:
            return False
        bucket.append(now)
        return True


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """共享连接池。

    `follow_redirects=False` 是**必须**的：客户端 `CampusSession` 自己逐跳跟重定向，
    而且它需要看到中间那一跳的 `Set-Cookie` 与 `Location`
    （CAS 的会话就藏在 302 里）。代理一旦自动跟随，这些信息就永久丢失了。
    """
    global _client
    if httpx is None:  # pragma: no cover —— 由调用方先行拦截
        raise RuntimeError('httpx 未安装')
    if _client is None:
        _client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
            verify=True,
            # 校园站点全是 HTTP/1.1，禁用 h2 可以少一层协商
            http2=False,
        )
    return _client


# ---------------------------------------------------------------- 端点


@router.post('/api/v1/webproxy/fetch')
async def webproxy_fetch(request: Request) -> Any:
    """代发一次 HTTP 请求，把状态码 / 头 / 字节原样交回。

    请求体（JSON）：
    ```json
    {
      "url": "https://jwc.cwxu.edu.cn/index/tzgg.htm",
      "method": "GET",
      "headers": {"cookie": "...", "referer": "..."},
      "body_b64": null
    }
    ```

    响应体（JSON）：
    ```json
    {
      "status": 200,
      "url": "https://jwc.cwxu.edu.cn/index/tzgg.htm",
      "headers": {"content-type": ["text/html; charset=utf-8"]},
      "body_b64": "...",
      "truncated": false
    }
    ```
    """
    import base64

    if httpx is None:
        return _fail(
            503,
            'proxy_unavailable',
            '服务器缺少 httpx，网页版抓取暂不可用（请联系管理员）',
        )

    ip = (request.client.host if request.client else '') or '-'
    if not await _rate_ok(ip):
        return _fail(429, 'rate_limited', '请求过于频繁，请稍后重试')

    try:
        payload = await request.json()
    except Exception:
        return _fail(400, 'bad_json', '请求体不是合法 JSON')
    if not isinstance(payload, dict):
        return _fail(400, 'bad_json', '请求体必须是 JSON 对象')

    url = str(payload.get('url') or '').strip()
    if not url:
        return _fail(400, 'missing_url', '缺少 url')

    host = _host_of(url)
    if host is None:
        return _fail(400, 'bad_url', '只支持 http/https 地址')
    if not _host_allowed(host):
        return _fail(403, 'host_not_allowed', f'不允许代理该站点：{host}')
    if await _resolves_to_private(host):
        return _fail(403, 'private_address', f'该域名解析到内网地址，已拒绝：{host}')

    method = str(payload.get('method') or 'GET').upper()
    if method not in ('GET', 'POST', 'HEAD', 'PUT', 'DELETE', 'PATCH'):
        return _fail(400, 'bad_method', f'不支持的方法：{method}')

    raw_headers = payload.get('headers')
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            name = str(key).lower().strip()
            if name in ALLOWED_REQUEST_HEADERS and value is not None:
                headers[name] = str(value)

    # 🔴 必须**始终**存在 `cookie` 键，即使客户端没有 cookie 也要给空串。
    # 原因见模块 docstring 第 1 条：共享 `httpx.AsyncClient` 自带 cookie jar，
    # 只要请求头里没有 cookie，它就会把上一个用户的 `route` / `JSESSIONID` 补进去。
    # 实测（2026-11-10）：
    #   带 `cookie: ''` → 教务回 `Set-Cookie: route=800b8c57...`（正确）
    #   不带 cookie 头   → jar 注入旧 route，教务回 `set-cookie: []`（CAS 断链）
    # 注意：`cookies=` 参数对已存在的 jar **无效**（实测），只有显式头能压住它。
    if 'cookie' not in headers:
        headers['cookie'] = ''

    body: bytes | None = None
    body_b64 = payload.get('body_b64')
    if isinstance(body_b64, str) and body_b64:
        try:
            body = base64.b64decode(body_b64, validate=False)
        except Exception:
            return _fail(400, 'bad_body', 'body_b64 不是合法 base64')

    try:
        client = _http()
        # 第二道保险：清空共享 client 的 jar。
        # 只靠请求头压住其实已经够（httpx 让显式头优先），但 jar 会长到很大
        # 且下一次改动若漏了上面那行就会立刻复发；这里按请求清零，成本是一次赋值。
        client.cookies = httpx.Cookies()
        async with client.stream(method, url, headers=headers, content=body) as resp:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                if total + len(chunk) > MAX_BODY_BYTES:
                    chunks.append(chunk[: MAX_BODY_BYTES - total])
                    total = MAX_BODY_BYTES
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)

            out_headers: dict[str, list[str]] = {}
            for name in ALLOWED_RESPONSE_HEADERS:
                if name == 'set-cookie':
                    # 逐个返回，绝不合并（合并会按逗号切碎带 Expires 的 cookie）
                    values = resp.headers.get_list('set-cookie')
                else:
                    values = resp.headers.get_list(name)
                if values:
                    out_headers[name] = list(values)

            return {
                'status': resp.status_code,
                'url': str(resp.url),
                'headers': out_headers,
                'body_b64': base64.b64encode(b''.join(chunks)).decode('ascii'),
                'truncated': truncated,
            }
    except httpx.TimeoutException:
        return _fail(504, 'upstream_timeout', '连接学校系统超时，请稍后重试')
    except httpx.HTTPError as exc:
        return _fail(502, 'upstream_error', f'访问学校系统失败：{type(exc).__name__}')
    except Exception as exc:  # noqa: BLE001 —— 兜底，避免 500 页面泄露栈
        return _fail(502, 'upstream_error', f'代理失败：{type(exc).__name__}')


@router.get('/api/v1/webproxy/ping')
async def webproxy_ping() -> dict[str, Any]:
    """给客户端做「代理是否可用」的探测。也用于运维确认部署已生效。"""
    return {
        'ok': httpx is not None,
        'suffixes': list(ALLOWED_SUFFIXES),
        'httpx': getattr(httpx, '__version__', None) if httpx else None,
    }
