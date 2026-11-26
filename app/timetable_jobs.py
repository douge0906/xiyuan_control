"""班级课表发布 outbox 的入队与查询助手。

课表没有文件要传，按理审核通过就能立刻可见。但它仍然走队列，原因是：
`PATCH /admin/timetables/{id}/status` 一次要写 status、写 audit、开可见性闸门，
如果闸门那一步失败，就变成「审核已通过、用户却永远搜不到」——后台看不出问题，
用户也不知道该找谁。走 outbox 之后，失败会留在队列里，后台能看见、能重试。

与 `resource_jobs` 分开实现，不共用表：资源那条链路的 action 白名单和 worker
分支都写死了资源逻辑，硬塞进来会在最不该出错的地方引入耦合。
"""

from __future__ import annotations

from typing import Any

from .security import iso as iso_now


def enqueue(conn: Any, upload_id: int, action: str) -> int:
    """把一次发布/下架动作写入队列；同一条上传同一动作已有未完成的 job 时复用。"""
    if action not in ("publish", "unpublish"):
        raise ValueError("invalid publish action")
    existing = conn.execute(
        """SELECT id FROM timetable_publish_jobs
           WHERE upload_id = ? AND action = ? AND status IN ('pending', 'running')
           ORDER BY id DESC LIMIT 1""",
        (upload_id, action),
    ).fetchone()
    stamp = iso_now()
    if existing is not None:
        conn.execute(
            """UPDATE timetable_publish_jobs
               SET status = 'pending', next_run_at = ?, updated_at = ?
               WHERE id = ?""",
            (stamp, stamp, existing["id"]),
        )
        return int(existing["id"])
    cur = conn.execute(
        """INSERT INTO timetable_publish_jobs
           (upload_id, action, status, attempts, last_error,
            created_at, updated_at, next_run_at)
           VALUES (?, ?, 'pending', 0, '', ?, ?, ?)""",
        (upload_id, action, stamp, stamp, stamp),
    )
    return int(cur.lastrowid)


def enqueue_publish(conn: Any, upload_id: int) -> int:
    return enqueue(conn, upload_id, "publish")


def enqueue_unpublish(conn: Any, upload_id: int) -> int:
    return enqueue(conn, upload_id, "unpublish")


def counts(conn: Any) -> dict[str, int]:
    rows = conn.execute(
        """SELECT status, COUNT(*) AS n FROM timetable_publish_jobs GROUP BY status"""
    ).fetchall()
    got = {str(r["status"]): int(r["n"]) for r in rows}
    return {
        "pending": got.get("pending", 0),
        "running": got.get("running", 0),
        "done": got.get("done", 0),
        "failed": got.get("failed", 0),
    }


def recent(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
    """最近的任务，带班级名，供后台队列面板直接显示。"""
    rows = conn.execute(
        """SELECT j.id, j.upload_id, j.action, j.status, j.attempts,
                  j.last_error, j.created_at, j.updated_at, j.next_run_at,
                  u.bj, u.jgmc, u.zymc
           FROM timetable_publish_jobs j
           LEFT JOIN timetable_uploads u ON u.id = j.upload_id
           ORDER BY j.id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "uploadId": int(r["upload_id"]),
            "class": str(r["bj"] or ""),
            "org": " · ".join(
                [x for x in (str(r["jgmc"] or ""), str(r["zymc"] or "")) if x]
            ),
            "action": str(r["action"]),
            "status": str(r["status"]),
            "attempts": int(r["attempts"]),
            "lastError": str(r["last_error"] or ""),
            "createdAt": str(r["created_at"]),
            "updatedAt": str(r["updated_at"]),
            "nextRunAt": str(r["next_run_at"]),
        })
    return out
