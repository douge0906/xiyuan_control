"""应用装配：配置、DB、路由、统一错误与 request_id。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import admin, api, campus_proxy, db, webproxy
from .config import Config
from .privacy import PRIVACY_HTML
from .security import iso as iso_now

APP_VERSION = "0.6.0"

logger = logging.getLogger("xiyuan_control")

app = FastAPI(
    title="掌上锡院控制台",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

cfg = Config()
db.init(cfg.db_path)
app.state.config = cfg
app.state.db = db.DB(cfg.db_path)

# Web 版（Flutter Web / PWA）从浏览器直接调本服务，必须放行跨域。
#
# 为什么 `allow_credentials=False` 是**故意**的：
# 本服务的鉴权走 `Authorization: Device <token>` 这类自定义头，
# 不依赖浏览器自动携带的 cookie。关掉 credentials 之后，
# 即使别的站点拿着用户浏览器来打我们，也带不上任何本域的会话。
# 真要放开凭据，`allow_origins` 就不能是 `*`，那是另一个量级的风险。
#
# 允许的方法里必须有 OPTIONS：预检请求走的就是它。此前没配 CORS，
# `OPTIONS /api/v1/messages` 直接被路由判成 405（实测 nginx 返回
# `allow: GET`），Web 端因此连公告都取不到。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
    max_age=600,
)

app.include_router(api.router)
app.include_router(admin.router)
# Web 版专用：代浏览器抓校园站点（浏览器受同源策略限制，读不到无 CORS 头的响应）。
# 独立模块、独立前缀 /api/v1/webproxy，不碰 api.py 的任何既有路由。
app.include_router(webproxy.router)
# Web 端有状态 CAS 会话代理：服务端按学号持有 cookie 罐，独立于无状态 webproxy。
app.include_router(campus_proxy.router)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(status: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}, "request_id": request_id},
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail: Any = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or "error")
        message = str(detail.get("message") or "请求失败")
    else:
        code = "error" if exc.status_code < 500 else "server_error"
        message = str(detail)
    return _error(exc.status_code, code, message, request.state.request_id)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # 未捕获异常必须留下 traceback（2026-11-03 修）。
    # 原来这里只返回统一文案、什么都不记 —— 线上「服务器内部错误」只能靠猜，
    # db.py 的并发缺陷因此藏了很久。响应体仍然不泄露内部信息，日志里记全量。
    request_id = getattr(request.state, "request_id", "-")
    logger.error(
        "unhandled error request_id=%s %s %s",
        request_id, request.method, request.url.path,
        exc_info=exc,
    )
    return _error(500, "server_error", "服务器内部错误", request_id)


@app.get("/")
def root() -> RedirectResponse:
    """访问域名根路径 → 直接进管理后台。"""
    return RedirectResponse(url="/admin", status_code=302)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """公开健康检查：只回 ok 与时间，不暴露版本、设备数等内部信息。"""
    return {"ok": True, "time": iso_now()}


@app.get("/privacy", response_class=HTMLResponse)
@app.get("/privacy.html", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    """隐私政策（公开，供备案与应用内查看）。"""
    return HTMLResponse(PRIVACY_HTML, headers={"Cache-Control": "public, max-age=300"})


@app.get("/admin/health")
def admin_health(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """详细健康信息：仅管理员可见。"""
    admin.require_admin(request, authorization)
    with app.state.db as conn:
        devices = conn.execute("SELECT COUNT(*) c FROM devices").fetchone()["c"]
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        sessions = conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE revoked_at IS NULL"
        ).fetchone()["c"]
    return {
        "ok": True,
        "app": "xiyuan-control",
        "version": APP_VERSION,
        "time": iso_now(),
        "devices": devices,
        "users": users,
        "sessions": sessions,
    }
