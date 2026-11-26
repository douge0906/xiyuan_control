"""资源发布 worker：独立进程消费 resource_publish_jobs。

为什么不再用 FastAPI BackgroundTasks：
- 审核通过后的 COS 上传可能几十秒到几分钟，会占住 Web 进程线程；
- 进程重启/部署会直接丢掉后台任务；
- 没有重试、没有失败记录、没有队列可观测性。

worker 与 Web 共用同一个 SQLite 文件和 data/uploads，串行处理，天然避免并发
重复上传；单个批次失败会退避重试，超过上限标 failed，等管理端处理。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# 允许 `python -m app.resource_worker` 和直接运行脚本两种方式。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .config import Config  # noqa: E402
from .db import DB, init as init_db  # noqa: E402

logger = logging.getLogger("xiyuan_control.resource_worker")
_MAX_ATTEMPTS = 6


def _stamp() -> str:
    from .security import iso
    return iso()


class WorkerApp:
    """让 api 里的发布函数继续按 `app.state.config/db` 取值。"""

    def __init__(self, cfg: Config, dbx: DB) -> None:
        self.state = SimpleNamespace(config=cfg, db=dbx)


def _claim(dbx: DB) -> dict[str, Any] | None:
    stamp = _stamp()
    with dbx as conn:
        row = conn.execute(
            """SELECT * FROM resource_publish_jobs
               WHERE status = 'pending' AND next_run_at <= ?
               ORDER BY id LIMIT 1""",
            (stamp,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE resource_publish_jobs
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
            "SELECT attempts FROM resource_publish_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        attempts = int(row["attempts"]) if row is not None else 1
        if not ok and attempts < _MAX_ATTEMPTS:
            delay = min(300, 10 * (2 ** max(0, attempts - 1)))
            next_run = time.strftime(
                "%Y-%m-%dT%H:%M:%S+08:00",
                time.localtime(time.time() + delay),
            )
            conn.execute(
                """UPDATE resource_publish_jobs
                   SET status = 'pending', last_error = ?, updated_at = ?,
                       next_run_at = ?
                   WHERE id = ?""",
                (error[:500], stamp, next_run, job_id),
            )
        else:
            conn.execute(
                """UPDATE resource_publish_jobs
                   SET status = ?, last_error = ?, updated_at = ?,
                       next_run_at = ?
                   WHERE id = ?""",
                ("done" if ok else "failed", error[:500], stamp, stamp, job_id),
            )
        if not ok:
            conn.execute(
                """INSERT INTO resource_daily_metrics (day, metric, count)
                   VALUES (?, 'publish_failed', 1)
                   ON CONFLICT(day, metric) DO UPDATE
                   SET count = count + 1""",
                (stamp[:10],),
            )


def process_one(cfg: Config, dbx: DB, job: dict[str, Any]) -> None:
    """执行一个 job。延迟导入 api，避免 app 包启动时循环依赖。"""
    from . import api

    wrapper = WorkerApp(cfg, dbx)
    upload_id = int(job["upload_id"])
    action = str(job["action"])
    try:
        if action == "publish":
            api._publish_resource_files(wrapper, upload_id)
            # 发布函数对单个文件失败只记日志；这里再做一次覆盖检查，
            # 让 job 能进入重试而不是“假成功”。
            if cfg.cos_enabled:
                with dbx as conn:
                    missing = conn.execute(
                        """SELECT COUNT(*) AS n FROM resource_files
                           WHERE upload_id = ? AND object_key = ''""",
                        (upload_id,),
                    ).fetchone()["n"]
                if missing:
                    raise RuntimeError(f"仍有 {missing} 个文件未进入 COS")
        elif action == "delete":
            api._delete_resource_objects(wrapper, upload_id)
        else:
            raise RuntimeError(f"未知 action: {action}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("资源发布 job 失败 id=%s upload=%s action=%s: %s",
                       job["id"], upload_id, action, exc)
        _finish(dbx, int(job["id"]), ok=False, error=str(exc))
        return
    _finish(dbx, int(job["id"]), ok=True)
    logger.info("资源发布 job 完成 id=%s upload=%s action=%s",
                job["id"], upload_id, action)


def backfill(cfg: Config, dbx: DB) -> int:
    """启动时把所有已审核批次入队一次；worker 会去重 pending 任务。"""
    if not cfg.resource_worker_backfill:
        return 0
    if not cfg.cos_enabled and not getattr(cfg, "edge_public_dir", None):
        return 0
    from . import resource_jobs
    with dbx as conn:
        ids = [
            int(row["upload_id"])
            for row in conn.execute(
                """SELECT DISTINCT f.upload_id FROM resource_files f
                   JOIN resource_uploads u ON u.id = f.upload_id
                   WHERE u.status = 'approved'
                     AND EXISTS (
                         SELECT 1 FROM resource_files g
                         WHERE g.upload_id = f.upload_id
                           AND (g.sha256 = '' OR g.object_key = '')
                     )
                   ORDER BY f.upload_id"""
            ).fetchall()
        ]
        for upload_id in ids:
            resource_jobs.enqueue_publish(conn, upload_id)
    logger.info("资源发布 worker 启动补队完成 batches=%s", len(ids))
    return len(ids)


def run_once(cfg: Config, dbx: DB) -> bool:
    job = _claim(dbx)
    if job is None:
        return False
    process_one(cfg, dbx, job)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="掌上锡院资源发布 worker")
    parser.add_argument("--once", action="store_true", help="只处理当前队列后退出")
    parser.add_argument("--backfill", action="store_true", help="入队历史已审核文件")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config()
    dbx = DB(cfg.db_path)
    init_db(cfg.db_path)
    if args.backfill:
        backfill(cfg, dbx)
    if args.once:
        processed = 0
        while run_once(cfg, dbx):
            processed += 1
        logger.info("worker once 完成 processed=%s", processed)
        return 0

    stop = False

    def _stop(signum, frame):  # noqa: ANN001, ARG001
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    backfill(cfg, dbx)
    logger.info("资源发布 worker 启动 poll=%ss", cfg.resource_worker_poll_seconds)
    while not stop:
        if not run_once(cfg, dbx):
            time.sleep(cfg.resource_worker_poll_seconds)
    logger.info("资源发布 worker 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
