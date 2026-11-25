"""班级课表发布 worker：独立进程消费 timetable_publish_jobs。

课表发布的动作很轻（就是把 `published` 置 1 / 置 0），但它必须可观测：
审核员点了「通过」之后如果没发布成功，用户搜不到，后台要看得见并能重试。
这就是走独立 worker 而不是在 PATCH 里直接 UPDATE 的全部理由。

启动方式与 `resource_worker.py` 一致：`python -m app.timetable_worker`。
两个 worker 互不干扰，各扫各的表。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .config import Config  # noqa: E402
from .db import DB, init as init_db  # noqa: E402

logger = logging.getLogger("xiyuan_control.timetable_worker")
_MAX_ATTEMPTS = 6


def _stamp() -> str:
    from .security import iso
    return iso()


def _claim(dbx: DB) -> dict[str, Any] | None:
    stamp = _stamp()
    with dbx as conn:
        row = conn.execute(
            """SELECT * FROM timetable_publish_jobs
               WHERE status = 'pending' AND next_run_at <= ?
               ORDER BY id LIMIT 1""",
            (stamp,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE timetable_publish_jobs
               SET status = 'running', attempts = attempts + 1,
                   updated_at = ?
               WHERE id = ?""",
            (stamp, row["id"]),
        )
        claimed = dict(row)
        claimed["attempts"] = int(row["attempts"]) + 1
        return claimed


def _finish(dbx: DB, job_id: int, *, ok: bool, error: str = "") -> None:
    stamp = _stamp()
    with dbx as conn:
        row = conn.execute(
            "SELECT attempts FROM timetable_publish_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        attempts = int(row["attempts"]) if row is not None else 1
        if not ok and attempts < _MAX_ATTEMPTS:
            delay = min(300, 10 * (2 ** max(0, attempts - 1)))
            next_run = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delay)
            )
            # security.iso() 带 +08:00，字符串直接比大小会错位；统一按本地时间格式补时区。
            next_run = f"{next_run}+08:00"
            conn.execute(
                """UPDATE timetable_publish_jobs
                   SET status = 'pending', last_error = ?, updated_at = ?,
                       next_run_at = ?
                   WHERE id = ?""",
                (error[:500], stamp, next_run, job_id),
            )
            return
        conn.execute(
            """UPDATE timetable_publish_jobs
               SET status = ?, last_error = ?, updated_at = ?
               WHERE id = ?""",
            ("done" if ok else "failed", error[:500], stamp, job_id),
        )


def _run_one(dbx: DB, job: dict[str, Any]) -> tuple[bool, str]:
    job_id = int(job["id"])
    upload_id = int(job["upload_id"])
    action = str(job["action"])
    stamp = _stamp()
    try:
        with dbx as conn:
            row = conn.execute(
                "SELECT id, status, published FROM timetable_uploads WHERE id = ?",
                (upload_id,),
            ).fetchone()
            if row is None:
                _finish(dbx, job_id, ok=True, error="upload 已不存在")
                return True, ""
            if action == "publish":
                # 审核通过之后又被驳回/下架了，这条 publish 就不该再生效。
                if str(row["status"]) != "approved":
                    _finish(dbx, job_id, ok=True,
                            error=f"upload 状态变为 {row['status']}，跳过发布")
                    return True, ""
                if int(row["published"]) == 1:
                    _finish(dbx, job_id, ok=True)
                    return True, ""
                conn.execute(
                    """UPDATE timetable_uploads
                       SET published = 1, published_at = ?
                       WHERE id = ?""",
                    (stamp, upload_id),
                )
            elif action == "unpublish":
                conn.execute(
                    """UPDATE timetable_uploads
                       SET published = 0
                       WHERE id = ?""",
                    (upload_id,),
                )
            else:
                _finish(dbx, job_id, ok=False, error=f"未知动作 {action}")
                return False, f"unknown action {action}"
        _finish(dbx, job_id, ok=True)
        logger.info("timetable job %s (%s upload=%s) 完成",
                    job_id, action, upload_id)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - worker 不能因单条失败退出
        logger.exception("timetable job %s 失败", job_id)
        _finish(dbx, job_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        return False, str(exc)


def run_once(dbx: DB) -> bool:
    """跑一条；返回是否真的处理了任务（供主循环决定要不要继续空转）。"""
    job = _claim(dbx)
    if job is None:
        return False
    _run_one(dbx, job)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="班级课表发布 worker")
    parser.add_argument("--once", action="store_true", help="只处理一轮就退出")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="空队列时的轮询间隔（秒）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config()
    init_db(cfg.db_path)
    dbx = DB(cfg.db_path)

    stopping = {"now": False}

    def _stop(signum, frame):  # noqa: ARG001
        stopping["now"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info("timetable worker 启动，db=%s", cfg.db_path)
    while not stopping["now"]:
        worked = False
        # 一次尽量把队列排空；有活就接着干，没活才睡。
        while not stopping["now"] and run_once(dbx):
            worked = True
        if args.once:
            break
        if not worked:
            time.sleep(max(0.2, float(args.interval)))
    logger.info("timetable worker 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
