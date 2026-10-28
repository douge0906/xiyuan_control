"""应用装配：配置、DB、路由、统一错误与 request_id。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import admin, api, db
from .config import Config
from .privacy import PRIVACY_HTML
from .security import iso as iso_now

APP_VERSION = "0.4.0"

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

app.include_router(api.router)
app.include_router(admin.router)


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
    return _error(500, "server_error", "服务器内部错误", request.state.request_id)


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
