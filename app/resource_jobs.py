"""资源发布 outbox 的入队与查询助手。

API/审核接口只负责写一条 job，实际上传/镜像/删除交给独立 worker。
这样 Web 进程不会因为几十 MB 的 COS 上传而占线程，也不会在重启后丢失任务。
"""

from __future__ import annotations

from typing import Any

from .security import iso as iso_now


def enqueue(conn: Any, upload_id: int, action: str) -> int:
    """把一次发布/删除动作写入队列；同一批次同一动作已有 pending 时复用。"""
    if action not in ("publish", "delete"):
        raise ValueError("invalid publish action")
    existing = conn.execute(
        """SELECT id FROM resource_publish_jobs
           WHERE upload_id = ? AND action = ? AND status IN ('pending', 'running')
           ORDER BY id DESC LIMIT 1""",
        (upload_id, action),
    ).fetchone()
    stamp = iso_now()
    if existing is not None:
        conn.execute(
            """UPDATE resource_publish_jobs
               SET status = 'pending', next_run_at = ?, updated_at = ?
               WHERE id = ?""",
            (stamp, stamp, existing["id"]),
        )
        return int(existing["id"])
    cur = conn.execute(
        """INSERT INTO resource_publish_jobs
           (upload_id, action, status, attempts, last_error,
            created_at, updated_at, next_run_at)
           VALUES (?, ?, 'pending', 0, '', ?, ?, ?)""",
        (upload_id, action, stamp, stamp, stamp),
    )
    return int(cur.lastrowid)


def enqueue_publish(conn: Any, upload_id: int) -> int:
    return enqueue(conn, upload_id, "publish")


def enqueue_delete(conn: Any, upload_id: int) -> int:
    return enqueue(conn, upload_id, "delete")


def pending_count(conn: Any) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM resource_publish_jobs WHERE status = 'pending'"
    ).fetchone()["n"])


def failed_count(conn: Any) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM resource_publish_jobs WHERE status = 'failed'"
    ).fetchone()["n"])


def last_error(conn: Any) -> str:
    row = conn.execute(
        """SELECT last_error, upload_id, action FROM resource_publish_jobs
           WHERE status = 'failed'
           ORDER BY updated_at DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return ""
    return f"upload={row['upload_id']} action={row['action']} {row['last_error']}"
