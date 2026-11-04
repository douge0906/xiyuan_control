"""统计查询：概览、趋势、分布、列表。

DAU/WAU/MAU 自 2026-11-04 起按「去重用户」统计（此前是 device_days 的设备数）：
device_days 只记 device_id，设备归谁要靠 sessions 反查 —— 取该设备上最后一次
登录的那条会话。查不到用户的设备（从没登录过的纯匿名设备）按设备本身各计 1，
不让它们凭空消失。一个用户换了几台设备仍只算 1；归档设备依旧不进任何统计。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from . import security

# 2026-11-02：归档设备（status='removed'）是设备去重留下的历史记录，
# 只是为了让旧记录可追溯，不代表一台在用的设备。
# 它不能进任何统计：概览卡片、版本/平台分布、新增趋势、DAU/WAU/MAU。
_NOT_REMOVED = "status != 'removed'"
_LIVE_DEVICE_IDS = "SELECT id FROM devices WHERE status != 'removed'"

# 设备 → 用户。同一台设备可能被多个学号登录过（换人用、重装重登），
# 取最后登录的那条会话（last_seen 打平时用 id 兜底，和 devices() 的排序同规矩）。
# 刻意不过滤 revoked_at：令牌被撤销（归档重复设备、refresh 轮换、封禁）不代表
# 「这个人没在这台设备上活跃过」，一过滤就会把历史用户全漏成匿名设备。
_DEVICE_USER = """(
        SELECT s.device_id AS device_id,
               (SELECT s2.user_id FROM sessions s2
                 WHERE s2.device_id = s.device_id
                 ORDER BY s2.last_seen DESC, s2.id DESC LIMIT 1) AS user_id
          FROM sessions s GROUP BY s.device_id
    )"""

# 一行 device_days 折算成一个「活跃用户」：能对上用户按用户去重，对不上退回设备。
# 'u7' 和 'd12' 是两套互不冲突的键，COUNT(DISTINCT ...) 不会把人和设备串味。
_ACTIVE_KEY = """COUNT(DISTINCT CASE WHEN du.user_id IS NOT NULL
                                 THEN 'u' || du.user_id
                                 ELSE 'd' || dd.device_id END)"""
_ACTIVE_FROM = """FROM device_days dd
             JOIN devices d ON d.id = dd.device_id AND d.status != 'removed'
             LEFT JOIN """ + _DEVICE_USER + """ du ON du.device_id = dd.device_id"""


def overview(conn: sqlite3.Connection, content_dir) -> dict[str, Any]:
    today = security.today()
    d7 = (security.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    d30 = (security.now() - timedelta(days=29)).strftime("%Y-%m-%d")

    def one(sql: str, *args) -> int:
        row = conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0

    def active_since(day: str, exact: bool = False) -> int:
        return one(
            "SELECT " + _ACTIVE_KEY + " " + _ACTIVE_FROM + " WHERE dd.day "
            + ("= ?" if exact else ">= ?"),
            day,
        )

    return {
        "generatedAt": security.iso(),
        "users": {
            "total": one("SELECT COUNT(*) FROM users"),
            "banned": one("SELECT COUNT(*) FROM users WHERE status='banned'"),
            "newToday": one("SELECT COUNT(*) FROM users WHERE created_at >= ?", today),
        },
        "devices": {
            "total": one("SELECT COUNT(*) FROM devices WHERE " + _NOT_REMOVED),
            "newToday": one(
                "SELECT COUNT(*) FROM devices WHERE created_at >= ? AND " + _NOT_REMOVED,
                today,
            ),
            "banned": one("SELECT COUNT(*) FROM devices WHERE status='banned'"),
        },
        "active": {
            # 口径见模块说明：去重用户，无主设备各自算 1 台。
            "dau": active_since(today, exact=True),
            "wau": active_since(d7),
            "mau": active_since(d30),
        },
        "events": {
            "today": one("SELECT COUNT(*) FROM events WHERE ts LIKE ?", f"{today}%"),
            "total": one("SELECT COUNT(*) FROM events"),
        },
        "risk": {
            "open": one("SELECT COUNT(*) FROM risk_events WHERE cleared_at IS NULL"),
        },
        "versions": breakdown(conn, "app_version"),
        "platforms": breakdown(conn, "platform"),
    }


def breakdown(conn: sqlite3.Connection, column: str, limit: int = 8) -> list[dict[str, Any]]:
    if column not in ("app_version", "platform"):
        raise ValueError("unsupported column")
    rows = conn.execute(
        f"""SELECT COALESCE(NULLIF({column}, ''), '未知') AS name, COUNT(*) AS value
            FROM devices WHERE {_NOT_REMOVED} GROUP BY name ORDER BY value DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def series(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    days = max(1, min(days, 90))
    start = (security.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    new_devices = {
        r["day"]: r["value"] for r in conn.execute(
            """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS value
               FROM devices WHERE created_at >= ? AND status != 'removed' GROUP BY day""",
            (start,),
        )
    }
    active = {
        r["day"]: r["value"] for r in conn.execute(
            "SELECT dd.day AS day, " + _ACTIVE_KEY + " AS value " + _ACTIVE_FROM
            + " WHERE dd.day >= ? GROUP BY dd.day",
            (start,),
        )
    }
    events = {
        r["day"]: r["value"] for r in conn.execute(
            """SELECT substr(ts, 1, 10) AS day, COUNT(*) AS value
               FROM events WHERE ts >= ? GROUP BY day""",
            (start,),
        )
    }
    out = []
    for i in range(days):
        day = (security.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        out.append({
            "day": day,
            "newDevices": new_devices.get(day, 0),
            "activeDevices": active.get(day, 0),
            "events": events.get(day, 0),
        })
    return out


def devices(conn: sqlite3.Connection, query: str = "", limit: int = 50,
            include_removed: bool = False) -> list[dict[str, Any]]:
    # 2026-11-01 设备去重：status 多了一个 'removed'（归档的重复设备）。
    # 默认不列出来，免得用户重装几次就把列表刷满；需要核对时带 include_removed。
    sql = """SELECT d.id, d.installation_id, d.platform, d.app_version, d.device_serial,
                    d.status, d.created_at, d.last_seen, d.merged_into,
                    (SELECT COUNT(*) FROM sessions s WHERE s.device_id = d.id
                       AND s.revoked_at IS NULL) AS sessions
             FROM devices d
             WHERE 1 = 1"""
    args: list = []
    if not include_removed:
        sql += " AND d.status != 'removed'"
    if query:
        sql += " AND (d.device_serial LIKE ? OR d.installation_id LIKE ?)"
        args += [f"%{query}%", f"%{query}%"]
    # last_seen 只精确到秒，同一秒内注册的多台会打平 —— 再用 id 兜底，
    # 保证「保留最近活跃那台」这个判断始终确定（打平时保留更晚创建的那条）。
    sql += " ORDER BY d.last_seen DESC, d.id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*args, limit))]


def users(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT u.id, u.student_alias, u.display_name, u.status,
                  u.created_at, u.last_login_at,
                  (SELECT COUNT(DISTINCT device_id) FROM sessions s
                     WHERE s.user_id = u.id AND s.revoked_at IS NULL) AS devices
           FROM users u ORDER BY u.last_login_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def risk_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.*, d.device_serial, u.student_alias
           FROM risk_events r
           LEFT JOIN devices d ON d.id = r.device_id
           LEFT JOIN users u ON u.id = r.user_id
           ORDER BY r.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def feedback(conn: sqlite3.Connection, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
    sql = """SELECT f.*, d.device_serial
             FROM feedback f
             LEFT JOIN devices d ON d.id = f.device_id"""
    args: tuple = ()
    if status:
        sql += " WHERE f.status = ?"
        args = (status,)
    sql += " ORDER BY f.id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*args, limit))]


def audit(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
