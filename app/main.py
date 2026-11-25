"""应用装配：配置、DB、路由、统一错误与 request_id。"""

from __future__ import annotations

import logging
import uuid
from html import escape
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import (
    admin,
    api,
    accounts,
    campus_proxy,
    content,
    course_ocr,
    db,
    dorm_proxy,
    timetables,
    webproxy,
)
from .config import Config
from .privacy import PRIVACY_HTML
from .security import iso as iso_now

APP_VERSION = "0.6.5"

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

# 资源发布由独立的 resource_worker 进程消费 outbox；Web 进程不再在启动时
# 起 daemon 线程补传历史文件，避免重启/部署丢任务、上传占线程。

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
# Web 端「电费查询 / 充值」代理：服务端代跑内网慧新 E 校链路（经 WebVPN 网关），
# 独立模块、独立前缀 /api/v1/campus/dorm，每会话独立 client 与 cookie 罐。
app.include_router(dorm_proxy.router)
app.include_router(course_ocr.router)
# 班级课表在线库（2026-11-25）：同学上传本班课表 → 审核 → 发布 → 别人直接导入。
# 独立模块、独立前缀 /api/v1/timetables 与 /admin/timetables，不碰 api.py 既有路由。
app.include_router(timetables.router)


@app.on_event("startup")
def _migrate_account_keys() -> None:
    """启动时把存量账号键迁到 HMAC；幂等，失败不影响对外服务。"""
    try:
        accounts.migrate(app.state.config, app.state.db)
    except Exception:  # noqa: BLE001 - 迁移失败时保留旧键，登录仍可走兼容路径
        logger.exception("账号键迁移失败，保留旧账号键")


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


#: App 的下载页（总站）。邀请落地页的下载按钮指向这里而不是 APK 直链，
#: 让新同学先看到功能简介与更新记录。改域名时只改这一处。
DOWNLOAD_PAGE = "https://wxxydouge.icu/pocket"


@app.get("/invite", response_class=HTMLResponse)
def invite_landing(request: Request, c: str = "") -> HTMLResponse:
    """邀请落地页（公开）：展示邀请码并引导新同学安装、填码。

    客户端分享出去的 `${baseUrl}/invite?c=<code>` 就落在这里。
    码只作为展示内容，不做任何服务端绑定动作，避免被爬取后自动建关系。
    """
    code = "".join(ch for ch in c.upper() if ch.isalnum())[:12]
    code_block = (
        f'<div class="code">{escape(code)}</div>'
        f'<button class="copy" type="button" data-code="{escape(code)}">复制邀请码</button>'
        if code
        else '<p class="muted">请向邀请你的同学索取 8 位邀请码。</p>'
    )

    # 下载按钮指向「下载页」而不是安装包直链：新同学先看到功能简介、更新记录
    # 与体积，再决定装不装。直链点下去是一个 20MB 的 APK 立刻落盘，对 App
    # 一无所知；下载页恒定可达，也不用跟每次发版同步 app_release.json。
    download = (
        f'<a class="download" href="{DOWNLOAD_PAGE}" rel="noopener">'
        "了解并下载掌上锡院</a>"
    )

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>掌上锡院 · 邀请同学</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 24px;
    background: #f1f2f5; color: #1f2937;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }}
  main {{
    width: 100%; max-width: 420px; background: #fff; border-radius: 18px;
    padding: 28px 24px 24px; box-shadow: 0 10px 30px rgba(31, 41, 55, .08);
  }}
  .badge {{
    width: 52px; height: 52px; border-radius: 14px; background: rgba(31, 41, 55, .08);
    display: flex; align-items: center; justify-content: center; font-size: 26px;
  }}
  h1 {{ font-size: 21px; margin: 16px 0 6px; }}
  .sub {{ color: #6b7280; font-size: 14px; margin: 0 0 20px; line-height: 1.6; }}
  .code {{
    font-size: 28px; font-weight: 700; letter-spacing: 6px; text-align: center;
    background: #f1f2f5; border-radius: 12px; padding: 16px 8px; margin-bottom: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    word-break: break-all;
  }}
  .copy {{
    width: 100%; border: 0; border-radius: 10px; padding: 12px;
    background: #1f2937; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }}
  .copy:active {{ opacity: .85; }}
  ol {{ margin: 22px 0 0; padding-left: 20px; color: #374151; font-size: 14px; line-height: 2; }}
  .download {{
    display: block; text-align: center; margin-top: 20px; padding: 13px;
    border: 1px solid #1f2937; border-radius: 10px; color: #1f2937;
    font-size: 15px; font-weight: 600; text-decoration: none;
  }}
  .openapp {{
    display: block; text-align: center; margin-top: 10px; padding: 13px;
    border-radius: 10px; background: #1f2937; color: #fff;
    font-size: 15px; font-weight: 600; text-decoration: none;
  }}
  .muted {{ color: #9ca3af; }}
  .small {{ font-size: 13px; margin-top: 18px; }}
  .tip {{ margin-top: 18px; color: #9ca3af; font-size: 12px; line-height: 1.7; }}
</style>
</head>
<body>
<main>
  <div class="badge" aria-hidden="true">
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none"
      stroke="#1f2937" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 9.5 12 5l9 4.5-9 4.5-9-4.5Z"/>
      <path d="M6.5 11.6V16c0 1.4 2.5 2.5 5.5 2.5s5.5-1.1 5.5-2.5v-4.4"/>
    </svg>
  </div>
  <h1>同学邀请你一起用「掌上锡院」</h1>
  <p class="sub">查课表、找空教室、看成绩，一个 App 搞定。</p>
  {code_block}
  <ol>
    <li>安装并打开「掌上锡院」，完成统一认证登录</li>
    <li>进入「我的」→「邀请同学」→「我有邀请码」</li>
    <li>填入上面的邀请码，提交后立即生效</li>
  </ol>
  {download}
  <a class="openapp" href="xiyuan://invite{('?c=' + escape(code)) if code else ''}">已安装？打开 App 填码</a>
  <p class="tip">邀请码仅用于建立邀请关系，不含你的学号、姓名等信息；同一账号只能绑定一次。</p>
</main>
<script>
  document.querySelectorAll('.copy').forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      var code = btn.getAttribute('data-code') || '';
      function fail() {{
        btn.textContent = '复制失败，请长按邀请码';
        setTimeout(function () {{ btn.textContent = '复制邀请码'; }}, 2200);
      }}
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(code).then(function () {{
          btn.textContent = '已复制';
          setTimeout(function () {{ btn.textContent = '复制邀请码'; }}, 1500);
        }}).catch(fail);
      }} else {{
        fail();
      }}
    }});
  }});
</script>
</body>
</html>"""
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=60"})


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
