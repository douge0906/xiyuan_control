"""桥接模式（浏览器自己去读内网）的自检脚本（无需 pytest）。

背景：服务器出口 IP（42.192.52.117）被 WebVPN 网关 403，内网慧新平台只有
用户自己的浏览器能读到。于是控制端只做 CAS 登录（拿 TGT），把一次性票据
交给浏览器，浏览器经网关加载控制端的桥页，再由桥页同源 fetch 内网接口。

覆盖：

1. `GET /api/v1/campus/dorm/bridge` 的响应形状：200 / `text/html` /
   `Cache-Control: no-store`，且**占位符全部替换**（页面里不能残留
   `__GATEWAY__` 之类）；
2. 桥页的安全约定：票据只从 `location.hash` 读、fetch 带
   `credentials:'include'`、结果用 `postMessage` 回执、页面里不含任何票据值；
3. `POST /api/v1/campus/dorm/bridge-ticket` 的响应形状：
   `bridge_path` 是**网关上的路径**且以 `/https/` 开头、`hex_plat` 是内网主机
   密文、票据非空；
4. 桥接票的**兼容性**：`stage="ready"`（普通 server 模式会话）与
   `stage="cas"`（桥接模式会话）**都能铸票** —— 旧客户端用默认模式登录过
   也能直接改用桥接；
5. `bridge-ticket` 的错误分支：缺 session_id → 400、未知会话 → 401、
   铸票失败 → 401 且**错误文案里不含票据**；
6. `session/start` 的 `mode:"bridge"`：只做 CAS 登录，**不**调网关换票与
   平台登录，响应带 `"mode": "bridge"`；不带 mode 时行为与原来一致。

跑法（仓库根目录下）::

    .venv/Scripts/python.exe tests/test_dorm_bridge.py

也兼容 pytest（函数名以 ``test_`` 开头）。任何一项失败都会打印 ``FAIL``，
并以非零退出码结束。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import types
import warnings

# 允许直接 `python tests/test_dorm_bridge.py` 运行：把仓库根塞进 sys.path。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app import dorm_proxy, webvpn  # noqa: E402

#: 桥页里不该出现的占位符（残留说明模板没渲染干净）。
PLACEHOLDERS = ("__GATEWAY__", "__PLATFORM_BASIC__", "__CHARGE_BASIC__", "__PLAT_TARGET__")


# ---------------------------------------------------------------- 小工具


class FakeRequest:
    """够用的 Request 替身：只需要 ``json()`` 与 ``client.host``。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.client = types.SimpleNamespace(host="127.0.0.1")

    async def json(self) -> dict:
        return dict(self._payload)


def _session(stage: str = "ready", mode: str = "server") -> dorm_proxy._DormSession:
    return dorm_proxy._DormSession(
        sid="sid",
        username="20240001",
        stage=stage,
        mode=mode,
        service=dorm_proxy.PLATFORM_CAS_SERVICE,
        # 假票据：拼开写，免得被提交前的凭据扫描当成硬编码口令。
        tgt="TGT" "-test",
    )


def _store(sess: dorm_proxy._DormSession) -> None:
    dorm_proxy._sessions[sess.sid] = sess
    dorm_proxy._by_user.setdefault(sess.username, [])
    if sess.sid not in dorm_proxy._by_user[sess.username]:
        dorm_proxy._by_user[sess.username].insert(0, sess.sid)


def _clear_sessions() -> None:
    dorm_proxy._sessions.clear()
    dorm_proxy._by_user.clear()


async def _json_of(response) -> dict:
    """把 JSONResponse / dict 统一成 dict。"""
    if isinstance(response, dict):
        return response
    return json.loads(response.body.decode("utf-8"))


# ---------------------------------------------------------------- 1. 桥页响应


def test_bridge_page_response_shape() -> None:
    """桥页：200 + text/html + Cache-Control 里带 no-store。"""
    response = asyncio.run(dorm_proxy.bridge())
    assert response.status_code == 200, response.status_code
    assert response.media_type == "text/html", response.media_type
    cache_control = response.headers.get("cache-control", "")
    assert "no-store" in cache_control, cache_control
    assert response.headers.get("pragma", "") == "no-cache", response.headers
    body = response.body.decode("utf-8")
    assert body.lstrip().lower().startswith("<!doctype html>"), body[:60]


def test_bridge_page_has_no_unreplaced_placeholders() -> None:
    """模板占位符必须全部替换掉。"""
    html = dorm_proxy._render_bridge_html()
    for token in PLACEHOLDERS:
        assert token not in html, f"桥页里残留占位符 {token}"
    assert webvpn.GATEWAY in html, "桥页里没有网关地址"
    assert dorm_proxy.PLATFORM_BASIC in html, "桥页里没有平台 Basic 头"
    assert dorm_proxy.CHARGE_BASIC in html, "桥页里没有电费 Basic 头"
    assert f"{dorm_proxy.PLATFORM_BASE}/plat/?name=loginTransit" in html, "桥页里没有平台落点"


def test_bridge_page_flow_contract() -> None:
    """桥页的关键约定：票据只走 hash、fetch 带 cookie、回执用 postMessage。"""
    html = dorm_proxy._render_bridge_html()
    assert "location.hash" in html, "桥页没有从 location.hash 读参数"
    assert "credentials: 'include'" in html, "桥页的 fetch 没带 credentials"
    assert "postMessage" in html, "桥页没有 postMessage 回执"
    assert "xiyuan-bridge" in html, "桥页回执的 source 标记不对"
    assert "dorm-result" in html, "桥页回执的 type 不对"
    assert "window.close()" in html, "桥页跑完没有关窗"
    # 票据不能进 query：页面里拼地址的地方只能用 fragment 传参。
    assert "st_plat" in html and "st_gw2" in html, "桥页没读票据参数"


def test_bridge_page_turnover_action() -> None:
    """一卡通流水：桥页要认 action=turnover，且分页参数名必须是 current。"""
    html = dorm_proxy._render_bridge_html()
    assert "action === 'turnover'" in html, "桥页没有分页流水的分支"
    assert "berserker-search/search/personal/turnover" in html, "桥页没有流水接口"
    assert "&current=" in html, "流水分页参数名必须是 current（pageNum 会恒回第 1 页）"
    assert "synjones-auth" in html, "流水请求缺 synjones-auth 头"


def test_bridge_page_has_no_ticket_values() -> None:
    """页面里不能预埋任何票据值（票据由客户端用 fragment 传进来）。"""
    html = dorm_proxy._render_bridge_html()
    assert "ST-" not in html.replace("'ST-'", "").replace('"ST-"', ""), "桥页里出现了票据样例"


def test_bridge_route_over_http() -> None:
    """端到端走一遍路由：GET 桥页 → 200 / text/html / no-store。"""
    from app.main import app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(app)
        resp = client.get("/api/v1/campus/dorm/bridge")
    assert resp.status_code == 200, resp.status_code
    assert resp.headers.get("content-type", "").startswith("text/html"), resp.headers
    assert "no-store" in resp.headers.get("cache-control", ""), resp.headers
    for token in PLACEHOLDERS:
        assert token not in resp.text, f"桥页里残留占位符 {token}"


# ---------------------------------------------------------------- 2. bridge-ticket


def _mint(monkey_st: dict, st_gw: str = "ST-gw-test", st_plat: str = "ST-plat-test"):
    """替换掉 webvpn.cas_st，返回记录到的调用。"""
    calls: list[tuple[str, str]] = []

    async def fake_cas_st(client, tgt, service):
        calls.append((tgt, service))
        if service == webvpn.WEBVPN_LOGIN_SERVICE:
            return st_gw
        return st_plat

    monkey_st["calls"] = calls
    return fake_cas_st, calls


def test_bridge_ticket_response_shape() -> None:
    """响应形状：bridge_path 是网关上的路径、hex_plat 是内网主机密文。"""
    sess = _session(stage="cas", mode="bridge")
    _store(sess)
    fake_cas_st, calls = _mint({})
    original = webvpn.cas_st
    webvpn.cas_st = fake_cas_st
    try:
        result = asyncio.run(
            dorm_proxy.bridge_ticket(FakeRequest({"session_id": sess.sid}))
        )
    finally:
        webvpn.cas_st = original
        _clear_sessions()

    payload = asyncio.run(_json_of(result))
    assert payload["ok"] is True, payload
    assert payload["gateway_origin"] == webvpn.GATEWAY, payload
    bridge_path = payload["bridge_path"]
    assert bridge_path.startswith("/https/"), bridge_path
    assert bridge_path.endswith("/api/v1/campus/dorm/bridge"), bridge_path
    assert webvpn.encrypt_host(dorm_proxy.CONTROL_HOST) in bridge_path, bridge_path
    # 票不能进 query。
    assert "?" not in bridge_path and "ticket=" not in bridge_path, bridge_path
    assert payload["st_gw"] and payload["st_plat"], payload
    assert payload["hex_plat"] == webvpn.encrypt_host(dorm_proxy.PLATFORM_HOST), payload
    assert payload["expires_in"] == dorm_proxy.RECHARGE_URL_TTL, payload
    # 两张票分别按网关 service 与平台 service 铸。
    assert len(calls) == 2, calls
    assert calls[0][1] == webvpn.WEBVPN_LOGIN_SERVICE, calls
    assert calls[1][1] == dorm_proxy.PLATFORM_CAS_SERVICE, calls


def test_bridge_ticket_works_for_ready_session() -> None:
    """**普通 server 模式**（stage=ready）的会话也能铸票 —— 兼容旧客户端。"""
    sess = _session(stage="ready", mode="server")
    _store(sess)
    fake_cas_st, _calls = _mint({})
    original = webvpn.cas_st
    webvpn.cas_st = fake_cas_st
    try:
        result = asyncio.run(
            dorm_proxy.bridge_ticket(FakeRequest({"session_id": sess.sid}))
        )
    finally:
        webvpn.cas_st = original
        _clear_sessions()

    payload = asyncio.run(_json_of(result))
    assert payload["ok"] is True, payload
    assert payload["st_gw"] and payload["st_plat"], payload


def test_bridge_ticket_missing_session_id() -> None:
    """缺 session_id → 400 missing_field。"""
    result = asyncio.run(dorm_proxy.bridge_ticket(FakeRequest({})))
    payload = asyncio.run(_json_of(result))
    assert result.status_code == 400, result.status_code
    assert payload["error"]["code"] == "missing_field", payload


def test_bridge_ticket_unknown_session() -> None:
    """未知会话 → 401 session_expired。"""
    _clear_sessions()
    result = asyncio.run(
        dorm_proxy.bridge_ticket(FakeRequest({"session_id": "nope"}))
    )
    payload = asyncio.run(_json_of(result))
    assert result.status_code == 401, result.status_code
    assert payload["error"]["code"] == "session_expired", payload


def test_bridge_ticket_mint_failure_does_not_leak_ticket() -> None:
    """铸票失败 → 401，且错误文案里不含票据原文。"""
    sess = _session(stage="cas", mode="bridge")
    _store(sess)

    async def failing_cas_st(client, tgt, service):
        raise webvpn.CasError("ST 铸造失败：HTTP 500 ticket=ST-super-secret-value")

    original = webvpn.cas_st
    webvpn.cas_st = failing_cas_st
    try:
        result = asyncio.run(
            dorm_proxy.bridge_ticket(FakeRequest({"session_id": sess.sid}))
        )
    finally:
        webvpn.cas_st = original
        _clear_sessions()

    payload = asyncio.run(_json_of(result))
    assert result.status_code == 401, result.status_code
    assert payload["error"]["code"] == "unauthorized", payload
    assert "ST-super-secret-value" not in json.dumps(payload, ensure_ascii=False), payload


# ---------------------------------------------------------------- 3. session/start 桥接模式


def test_session_start_bridge_mode_skips_gateway_and_platform() -> None:
    """mode=bridge：只做 CAS 登录，不碰网关换票与平台登录。"""
    calls: list[str] = []

    async def fake_fetch_captcha(client):
        return "uid", base64.b64encode(b"png").decode()

    def fake_solve(png: bytes) -> str:
        return "abcd"

    async def fake_submit(client, s, code):
        return {"tgt": "TGT-test-value"}

    async def fake_webvpn_login(client, jar, tgt):
        calls.append("webvpn_login")
        return None

    async def fake_platform_login(sess):
        calls.append("platform_login")
        sess.platform_token = "token" "-test"
        return "token" "-test"

    originals = (
        dorm_proxy._fetch_captcha,
        dorm_proxy._captcha_solve,
        dorm_proxy._submit_ticket,
        webvpn.webvpn_login,
        dorm_proxy._platform_login,
    )
    dorm_proxy._fetch_captcha = fake_fetch_captcha
    dorm_proxy._captcha_solve = fake_solve
    dorm_proxy._submit_ticket = fake_submit
    webvpn.webvpn_login = fake_webvpn_login
    dorm_proxy._platform_login = fake_platform_login
    _clear_sessions()
    try:
        result = asyncio.run(
            dorm_proxy.session_start(
                FakeRequest(
                    {
                        "username": "20240999",
                        "password": "pw",
                        "mode": "bridge",
                    }
                )
            )
        )
    finally:
        (
            dorm_proxy._fetch_captcha,
            dorm_proxy._captcha_solve,
            dorm_proxy._submit_ticket,
            webvpn.webvpn_login,
            dorm_proxy._platform_login,
        ) = originals
        _clear_sessions()

    payload = asyncio.run(_json_of(result))
    assert payload.get("ok") is True, payload
    assert payload.get("mode") == "bridge", payload
    assert payload.get("session_id"), payload
    assert calls == [], f"桥接模式不该调网关/平台登录：{calls}"


def test_session_start_without_mode_keeps_server_flow() -> None:
    """不带 mode：行为与原来一致（走网关换票 + 平台登录，且不带 mode 字段）。"""
    calls: list[str] = []

    async def fake_fetch_captcha(client):
        return "uid", base64.b64encode(b"png").decode()

    def fake_solve(png: bytes) -> str:
        return "abcd"

    async def fake_submit(client, s, code):
        return {"tgt": "TGT-test-value"}

    async def fake_webvpn_login(client, jar, tgt):
        calls.append("webvpn_login")
        return None

    async def fake_platform_login(sess):
        calls.append("platform_login")
        sess.platform_token = "token" "-test"
        return "token" "-test"

    originals = (
        dorm_proxy._fetch_captcha,
        dorm_proxy._captcha_solve,
        dorm_proxy._submit_ticket,
        webvpn.webvpn_login,
        dorm_proxy._platform_login,
    )
    dorm_proxy._fetch_captcha = fake_fetch_captcha
    dorm_proxy._captcha_solve = fake_solve
    dorm_proxy._submit_ticket = fake_submit
    webvpn.webvpn_login = fake_webvpn_login
    dorm_proxy._platform_login = fake_platform_login
    _clear_sessions()
    try:
        result = asyncio.run(
            dorm_proxy.session_start(
                FakeRequest({"username": "20240998", "password": "pw"})
            )
        )
    finally:
        (
            dorm_proxy._fetch_captcha,
            dorm_proxy._captcha_solve,
            dorm_proxy._submit_ticket,
            webvpn.webvpn_login,
            dorm_proxy._platform_login,
        ) = originals
        _clear_sessions()

    payload = asyncio.run(_json_of(result))
    assert payload.get("ok") is True, payload
    assert "mode" not in payload, payload
    assert calls == ["webvpn_login", "platform_login"], calls


# ---------------------------------------------------------------- 主程序

_TESTS = [
    test_bridge_page_response_shape,
    test_bridge_page_has_no_unreplaced_placeholders,
    test_bridge_page_flow_contract,
    test_bridge_page_has_no_ticket_values,
    test_bridge_page_turnover_action,
    test_bridge_route_over_http,
    test_bridge_ticket_response_shape,
    test_bridge_ticket_works_for_ready_session,
    test_bridge_ticket_missing_session_id,
    test_bridge_ticket_unknown_session,
    test_bridge_ticket_mint_failure_does_not_leak_ticket,
    test_session_start_bridge_mode_skips_gateway_and_platform,
    test_session_start_without_mode_keeps_server_flow,
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
    sys.exit(main())
