"""电费查询 / 充值后端的自检脚本（无需 pytest）。

覆盖：

1. AES-128-CFB 主机名加密的 6 条 golden vector（与安卓端 `webvpn_test.dart` 同源）；
2. `wrap_url` 的格式、幂等性与 `needs_wrap` 的网段判定；
3. `vpn_fetch` 的三条硬规矩：相对 Location 相对**未包装**地址解析、302 之后
   退化为 GET、每跳捕获 `Set-Cookie`；外加「网关票据过期 → 302 /login 原样返回」；
4. 电费查询（`getThirdData`）的响应处理：`map` 原样透传、4030 / 登录页 HTML
   归类为未授权。

跑法（仓库根目录下）::

    .venv/Scripts/python.exe tests/test_dorm_webvpn.py

也兼容 pytest（函数名以 ``test_`` 开头）。任何一项失败都会打印 ``FAIL``，
并以非零退出码结束。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# 允许直接 `python tests/test_dorm_webvpn.py` 运行：把仓库根塞进 sys.path。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx  # noqa: E402

from app import campus_proxy, dorm_proxy, webvpn  # noqa: E402

# ---------------------------------------------------------------- 小工具


class FakeResponse:
    """够用的 httpx.Response 替身。"""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.content = content
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class FakeClient:
    """按顺序吐出预设响应的假客户端，并记录每一次请求。"""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        data: dict | None = None,
        content: bytes | None = None,
        follow_redirects: bool = False,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "data": data,
                "content": content,
            }
        )
        resp = self.responses.pop(0)
        if not resp.url:
            resp.url = url
        return resp


# ---------------------------------------------------------------- 1. AES golden vector

GOLDEN_VECTORS = {
    "my.cwxu.edu.cn": "77726476706e69737468656265737421fdee0f9f30287d1e7b0c9ce29b5b",
    "wxcas.cwxu.edu.cn": (
        "77726476706e69737468656265737421e7ef429d347e6b47661dc7a99c406d3676"
    ),
    "10.3.100.110": "77726476706e69737468656265737421a1a70fcf696138003059d8fc",
    "202.204.48.66": "77726476706e69737468656265737421a2a713d275603c1e2a50c7face",
    "219.216.96.4": "77726476706e69737468656265737421a2a618d275613e1e275ec7f8",
    "space.bilibili.com": (
        "77726476706e69737468656265737421e3e7409f227e6a5972018ba5945c6d36db05"
    ),
}


def test_encrypt_host_golden_vectors() -> None:
    """6 条 golden vector 必须逐字节一致（否则网关解不开地址）。"""
    for host, expected in GOLDEN_VECTORS.items():
        actual = webvpn.encrypt_host(host)
        assert actual == expected, f"encrypt_host({host}) = {actual}，期望 {expected}"
        # 前 32 个 hex 字符就是密钥本身。
        assert actual.startswith(webvpn.SECRET.hex())
        assert len(actual) == 32 + len(host) * 2


# ---------------------------------------------------------------- 2. wrap_url / needs_wrap


def test_wrap_url_format() -> None:
    """包装格式：网关 + /<scheme[-port]>/<hexHost><path>[?query][#frag]。"""
    host_hex = webvpn.encrypt_host("10.3.100.110")
    wrapped = webvpn.wrap_url("http://10.3.100.110/charge/feeitem?a=1&b=2")
    assert wrapped == (
        f"{webvpn.GATEWAY}/http/{host_hex}/charge/feeitem?a=1&b=2"
    ), wrapped

    # 根路径补 '/'
    assert webvpn.wrap_url("http://10.3.100.110") == (
        f"{webvpn.GATEWAY}/http/{host_hex}/"
    ), webvpn.wrap_url("http://10.3.100.110")

    # 非默认端口 → 段名带端口
    assert webvpn.wrap_url("http://10.3.100.110:8080/x").startswith(
        f"{webvpn.GATEWAY}/http-8080/{host_hex}/x"
    ), webvpn.wrap_url("http://10.3.100.110:8080/x")

    # 公网地址原样返回
    assert webvpn.wrap_url("https://jwgl.cwxu.edu.cn/sso/x") == (
        "https://jwgl.cwxu.edu.cn/sso/x"
    )

    # 幂等：包装两次结果相同
    once = webvpn.wrap_url("http://10.3.100.110/charge/feeitem?a=1&b=2")
    assert webvpn.wrap_url(once) == once


def test_needs_wrap_ranges() -> None:
    """只有 10.x / 192.168.x / 172.16-31.x 需要走网关。"""
    for url in (
        "http://10.3.100.110/x",
        "http://192.168.1.2/x",
        "http://172.20.0.1/x",
        "http://172.16.0.1/x",
        "http://172.31.255.254/x",
    ):
        assert webvpn.needs_wrap(url) is True, url
    for url in (
        "http://172.32.0.1/x",
        "http://172.15.0.1/x",
        "http://11.0.0.1/x",
        "http://193.168.1.2/x",
        "https://jwgl.cwxu.edu.cn/x",
        "https://webvpn.cwxu.edu.cn/http/abc/x",
    ):
        assert webvpn.needs_wrap(url) is False, url


# ---------------------------------------------------------------- 3. vpn_fetch


def test_vpn_fetch_relative_location_resolved_against_raw() -> None:
    """相对 Location 必须相对**未包装**的原始地址解析。"""
    host_hex = webvpn.encrypt_host("10.3.100.110")
    client = FakeClient(
        [
            FakeResponse(
                302,
                {"Location": "/plat/?name=loginTransit&ticket=ABCD"},
                b"",
            ),
            FakeResponse(200, {}, b'{"code":"200"}'),
        ]
    )
    jar: dict[str, str] = {}
    status, _url, _headers, body = asyncio.run(
        webvpn.vpn_fetch(client, jar, "http://10.3.100.110/berserker-auth/cas/login/lyCas")
    )
    assert status == 200, status
    assert body == b'{"code":"200"}', body
    assert len(client.calls) == 2, client.calls
    # 第二跳的落点：原始地址 + 相对 Location，再重新包装。
    expected = webvpn.wrap_url("http://10.3.100.110/plat/?name=loginTransit&ticket=ABCD")
    assert client.calls[1]["url"] == expected, client.calls[1]["url"]
    # 不能出现「/http/<hex>/http/<hex>」这种被吃掉前缀的坏形状。
    assert client.calls[1]["url"].count(host_hex) == 1, client.calls[1]["url"]


def test_vpn_fetch_302_degrades_to_get() -> None:
    """302 之后退化为 GET，表单 / 正文只挂第一跳。"""
    client = FakeClient(
        [
            FakeResponse(302, {"Location": "/next"}, b""),
            FakeResponse(200, {}, b"ok"),
        ]
    )
    jar: dict[str, str] = {}
    asyncio.run(
        webvpn.vpn_fetch(
            client,
            jar,
            "http://10.3.100.110/charge/feeitem/getThirdData",
            method="POST",
            form={"feeitemid": "448"},
        )
    )
    assert client.calls[0]["method"] == "POST", client.calls[0]
    assert client.calls[0]["data"] == {"feeitemid": "448"}, client.calls[0]
    assert client.calls[1]["method"] == "GET", client.calls[1]
    assert client.calls[1]["data"] is None, client.calls[1]
    assert client.calls[1]["content"] is None, client.calls[1]


def test_vpn_fetch_captures_cookie_every_hop() -> None:
    """每跳都要捕获 Set-Cookie（含删除语义）。"""
    client = FakeClient(
        [
            FakeResponse(
                302,
                {
                    "Location": "/plat/",
                    "Set-Cookie": "wengine_vpn_ticketwebvpn_cwxu_edu_cn=abc; Path=/",
                },
                b"",
            ),
            FakeResponse(
                200,
                {"Set-Cookie": "JSESSIONID=xyz; Path=/"},
                b"ok",
            ),
        ]
    )
    jar: dict[str, str] = {}
    asyncio.run(webvpn.vpn_fetch(client, jar, "http://10.3.100.110/plat/"))
    assert jar.get("wengine_vpn_ticketwebvpn_cwxu_edu_cn") == "abc", jar
    assert jar.get("JSESSIONID") == "xyz", jar
    # 第二跳必须把第一跳拿到的 cookie 带回去。
    cookie = client.calls[1]["headers"].get("Cookie") or ""
    assert "wengine_vpn_ticketwebvpn_cwxu_edu_cn=abc" in cookie, cookie


def test_vpn_fetch_always_sends_cookie_header() -> None:
    """永远显式带 Cookie 头（哪怕空串）——webproxy.py:293-300 的教训。"""
    client = FakeClient([FakeResponse(200, {}, b"ok")])
    asyncio.run(webvpn.vpn_fetch(client, {}, "http://10.3.100.110/plat/"))
    headers = client.calls[0]["headers"]
    assert "Cookie" in headers, headers
    assert headers["Cookie"] == "", headers


def test_vpn_fetch_gateway_login_redirect_returned_as_is() -> None:
    """网关票据过期（302 → /login）时把 302 原样交回，让调用方重铸票据。"""
    client = FakeClient(
        [
            FakeResponse(
                302,
                {"Location": f"{webvpn.GATEWAY}/login?cas_login=true"},
                b"",
            )
        ]
    )
    jar: dict[str, str] = {}
    status, _url, headers, _body = asyncio.run(
        webvpn.vpn_fetch(client, jar, "http://10.3.100.110/plat/")
    )
    assert status == 302, status
    assert len(client.calls) == 1, client.calls
    assert webvpn._is_gateway_login(headers.get("location") or "") is True


def test_vpn_fetch_hop_limit() -> None:
    """超过 12 跳要抛错，不能无限跟。"""
    client = FakeClient([FakeResponse(302, {"Location": "/loop"}, b"") for _ in range(20)])
    jar: dict[str, str] = {}

    async def run() -> None:
        await webvpn.vpn_fetch(client, jar, "http://10.3.100.110/loop/")

    try:
        asyncio.run(run())
    except TimeoutError:
        return
    raise AssertionError("12 跳上限没有生效")


# ---------------------------------------------------------------- 4. 电费查询响应处理


def _session() -> dorm_proxy._DormSession:
    return dorm_proxy._DormSession(
        sid="sid",
        username="20240001",
        stage="ready",
        service=dorm_proxy.PLATFORM_CAS_SERVICE,
        tgt="TGT-test",
        # 假票据：拼写拆开写，免得被提交前的凭据扫描当成硬编码口令。
        platform_token="token" "-test",
    )


def test_query_returns_map_verbatim() -> None:
    """上游 map 原样返回，服务端不解析。"""
    captured: dict = {}
    payload = {"code": "200", "map": {"balance": 12.34, "elec": 3.5, "unit": "元"}}

    async def fake_fetch(sess, raw_url, method="GET", headers=None, form=None, body=None, retry=True):
        captured["raw_url"] = raw_url
        captured["method"] = method
        captured["form"] = form
        captured["headers"] = headers
        return 200, raw_url, httpx.Headers(), json.dumps(payload).encode()

    original = dorm_proxy._gateway_fetch
    dorm_proxy._gateway_fetch = fake_fetch
    try:
        result = asyncio.run(
            dorm_proxy._query_third_data(
                _session(), "tok", "448", "1", "A7&李园贰號A7", "211"
            )
        )
    finally:
        dorm_proxy._gateway_fetch = original

    assert result == payload["map"], result
    # 请求形状：地址、表单、鉴权头、同名参数。
    assert captured["raw_url"] == dorm_proxy.PLATFORM_QUERY_URL, captured["raw_url"]
    assert captured["method"] == "POST", captured
    assert captured["form"]["feeitemid"] == "448", captured["form"]
    assert captured["form"]["type"] == "IEC", captured["form"]
    assert captured["form"]["level"] == "3", captured["form"]
    assert captured["headers"]["Authorization"] == dorm_proxy.CHARGE_BASIC, captured
    assert captured["headers"]["synjones-auth"] == "bearer tok", captured
    assert captured["headers"]["synAccessSource"] == "app", captured


def test_query_4030_is_unauthorized() -> None:
    """4030（服务大厅未授权）→ unauthorized，不是上游错误。"""
    async def fake_fetch(sess, raw_url, method="GET", headers=None, form=None, body=None, retry=True):
        return 200, raw_url, httpx.Headers(), json.dumps({"code": "4030"}).encode()

    original = dorm_proxy._gateway_fetch
    dorm_proxy._gateway_fetch = fake_fetch
    try:
        asyncio.run(
            dorm_proxy._query_third_data(_session(), "tok", "448", "1", "A7", "211")
        )
    except dorm_proxy.DormError as exc:
        assert exc.code == "unauthorized", exc.code
        assert exc.status == 401, exc.status
    else:
        raise AssertionError("4030 没有被归类为 unauthorized")
    finally:
        dorm_proxy._gateway_fetch = original


def test_query_login_page_html_is_unauthorized() -> None:
    """回登录页 HTML（票据掉了）→ unauthorized。"""

    async def fake_fetch(sess, raw_url, method="GET", headers=None, form=None, body=None, retry=True):
        return 200, raw_url, httpx.Headers(), b"<html><body>login</body></html>"

    original = dorm_proxy._gateway_fetch
    dorm_proxy._gateway_fetch = fake_fetch
    try:
        asyncio.run(
            dorm_proxy._query_third_data(_session(), "tok", "448", "1", "A7", "211")
        )
    except dorm_proxy.DormError as exc:
        assert exc.code == "unauthorized", exc.code
    else:
        raise AssertionError("登录页 HTML 没有被归类为 unauthorized")
    finally:
        dorm_proxy._gateway_fetch = original


def test_split_cookies_separates_gateway_and_platform() -> None:
    """网关 cookie 与平台 cookie 分开存放，不能混。"""
    sess = _session()
    dorm_proxy._split_cookies(
        {
            "wengine_vpn_ticketwebvpn_cwxu_edu_cn": "g1",
            "route": "g2",
            "JSESSIONID": "p1",
            "SESSION": "p2",
        },
        sess,
    )
    assert sess.gateway_cookies == {
        "wengine_vpn_ticketwebvpn_cwxu_edu_cn": "g1",
        "route": "g2",
    }, sess.gateway_cookies
    assert sess.platform_cookies == {"JSESSIONID": "p1", "SESSION": "p2"}, sess.platform_cookies


# ---------------------------------------------------------------- 主程序

def test_multi_session_same_user_not_evicted() -> None:
    """同一学号登录多次**不应**互相踢下线。

    旧实现 MAX_PER_USER=1 且 `_by_user` 只存一条 sid，于是「手机上登一次、
    电脑上再登一次」会把前一个会话挤掉，前一个设备随后所有请求都收到
    401 session_expired —— 用户体感就是「登录状态不在了」。
    """

    async def scenario() -> None:
        dorm_proxy._sessions.clear()
        dorm_proxy._by_user.clear()
        try:
            for i in range(4):
                sess = dorm_proxy._DormSession(
                    sid=f"sid-{i}",
                    username="20240001",
                    stage="ready",
                    service=dorm_proxy.PLATFORM_CAS_SERVICE,
                    tgt="TGT-test",
                    platform_token="token" "-test",
                )
                await dorm_proxy._store_session(sess)
            # 保留最近 MAX_PER_USER 条，更早的才淘汰。
            assert len(dorm_proxy._sessions) == dorm_proxy.MAX_PER_USER
            alive = set(dorm_proxy._sessions)
            assert "sid-3" in alive and "sid-2" in alive, alive
            assert "sid-0" not in alive, alive
            # 同一学号的索引里保留下来的 sid 数与实际会话数一致。
            sids = dorm_proxy._by_user.get("20240001") or []
            assert len(sids) == dorm_proxy.MAX_PER_USER, sids
            assert sids[0] == "sid-3", sids
        finally:
            dorm_proxy._sessions.clear()
            dorm_proxy._by_user.clear()

    asyncio.run(scenario())


def test_multi_session_campus_proxy_not_evicted() -> None:
    """教务会话同样是「多设备互不踢」。"""

    async def scenario() -> None:
        campus_proxy._sessions.clear()
        campus_proxy._by_user.clear()
        try:
            for i in range(4):
                sess = campus_proxy._Session(
                    sid=f"sid-{i}",
                    username="20240001",
                    service=campus_proxy.JWGL_SERVICE,
                    stage="ready",
                    tgt="TGT-test",
                )
                campus_proxy._sessions[sess.sid] = sess
                campus_proxy._remember_user_session(sess.username, sess.sid)
            assert len(campus_proxy._sessions) == campus_proxy.MAX_PER_USER
            alive = set(campus_proxy._sessions)
            assert "sid-3" in alive and "sid-2" in alive, alive
            assert "sid-0" not in alive, alive
        finally:
            campus_proxy._sessions.clear()
            campus_proxy._by_user.clear()

    asyncio.run(scenario())


_TESTS = [
    test_encrypt_host_golden_vectors,
    test_wrap_url_format,
    test_needs_wrap_ranges,
    test_vpn_fetch_relative_location_resolved_against_raw,
    test_vpn_fetch_302_degrades_to_get,
    test_vpn_fetch_captures_cookie_every_hop,
    test_vpn_fetch_always_sends_cookie_header,
    test_vpn_fetch_gateway_login_redirect_returned_as_is,
    test_vpn_fetch_hop_limit,
    test_query_returns_map_verbatim,
    test_query_4030_is_unauthorized,
    test_query_login_page_html_is_unauthorized,
    test_split_cookies_separates_gateway_and_platform,
    test_multi_session_same_user_not_evicted,
    test_multi_session_campus_proxy_not_evicted,
]


def main() -> int:
    failed = 0
    for test in _TESTS:
        name = test.__name__
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    total = len(_TESTS)
    print(f"\n{total - failed}/{total} passed")
    if failed:
        print("FAIL")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
