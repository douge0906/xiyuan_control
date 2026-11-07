# -*- coding: utf-8 -*-
"""微信推送：把新反馈即时推到维护者微信（ShowDoc 推送）。

设计取舍
--------
- 只用标准库（urllib），控制台的 requirements 里没有 requests，也不为一条推送加依赖。
- 在后台线程里发，绝不阻塞反馈接口的响应：推送失败不能影响用户提交。
- 只从 data/.env 读 FEEDBACK_SHOWDOC_TOKEN；没配就静默跳过（本地开发不该报错）。
- ShowDoc 推送的成功判据是响应体 ``error_code == 0``，不是 HTTP 200
  （token 错也返回 200，只看状态码会「假成功」—— 这是掌上锡院老服务端踩过的坑）。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from typing import Any

logger = logging.getLogger("xiyuan_control.notify")

# uvicorn 只给 "uvicorn.*" 装 handler，根 logger 没有 handler，
# 这里不自己挂一个的话 logger.info/warning 全部落空 —— 推送成没成功在
# journalctl 里根本看不见（2026-11-08 排查过：接口 200 但日志空）。
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

PUSH_BASE = "https://push.showdoc.com.cn/server/api/push/"


def _post(url: str, payload: dict[str, Any], timeout: int = 15) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except Exception as exc:  # 网络层失败
        return False, f"{type(exc).__name__}: {exc}"

    try:
        parsed = json.loads(body)
    except Exception:
        return False, f"HTTP {status} 非 JSON 响应: {body[:200]}"

    if parsed.get("error_code") == 0:
        return True, "ok"
    return False, parsed.get("error_message") or f"error_code={parsed.get('error_code')}"


def _format(feedback: dict[str, Any]) -> str:
    """Markdown 卡片：微信里一眼看完是谁、从哪、说了什么。"""
    content = (feedback.get("content") or "").strip() or "（空）"
    contact = (feedback.get("contact") or "").strip() or "未填写"
    source = (feedback.get("app_version") or "").strip() or "未知"
    stamp = (feedback.get("created_at") or "").strip()
    rid = feedback.get("id")

    return (
        f"## 📮 新反馈 #{rid}\n\n"
        f"> {content}\n\n"
        f"---\n\n"
        f"**🕒 时间**：{stamp}\n\n"
        f"**📱 来源**：{source}\n\n"
        f"**📮 联系方式**：{contact}\n\n"
        f"处理：<https://con.wxxydouge.icu/admin> → 反馈"
    )


def push_feedback(token: str, feedback: dict[str, Any]) -> None:
    """推送一条反馈。同步执行，调用方负责放进线程。"""
    if not token:
        return
    title = f"📮 新反馈 #{feedback.get('id')}"
    ok, detail = _post(PUSH_BASE + token, {"title": title, "content": _format(feedback)})
    if ok:
        logger.info("feedback push ok id=%s", feedback.get("id"))
    else:
        logger.warning("feedback push failed id=%s: %s", feedback.get("id"), detail)


def push_feedback_async(token: str, feedback: dict[str, Any]) -> None:
    """后台线程推送，任何异常都不外抛。"""
    if not token:
        return

    def _job() -> None:
        try:
            push_feedback(token, feedback)
        except Exception:  # 兜底：daemon 线程里绝不能把异常带到主流程
            logger.warning("feedback push crashed id=%s", feedback.get("id"), exc_info=True)

    try:
        threading.Thread(target=_job, daemon=True).start()
    except Exception:
        logger.warning("feedback push thread start failed id=%s", feedback.get("id"))
