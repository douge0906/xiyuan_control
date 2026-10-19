"""统计查询：概览、趋势、分布、列表。

DAU/WAU/MAU 基于 device_days（任何鉴权请求都会打点），
按日历日去重，避免"同一设备一天算多次"。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from . import security


def overview(conn: sqlite3.Connection, content_dir) -> dict[str, Any]:
    today = security.today()
    d7 = (security.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    d30 = (security.now() - timedelta(days=29)).strftime("%Y-%m-%d")

    def one(sql: str, *args) -> int:
        row = conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0

    return {
        "generatedAt": security.iso(),
        "users": {
            "total": one("SELECT COUNT(*) FROM users"),
            "banned": one("SELECT COUNT(*) FROM users WHERE status='banned'"),
            "newToday": one("SELECT COUNT(*) FROM users WHERE created_at >= ?", today),
        },
        "devices": {
            "total": one("SELECT COUNT(*) FROM devices"),
            "newToday": one("SELECT COUNT(*) FROM devices WHERE created_at >= ?", today),
            "banned": one("SELECT COUNT(*) FROM devices WHERE status='banned'"),
        },
        "active": {
            "dau": one("SELECT COUNT(DISTINCT device_id) FROM device_days WHERE day = ?", today),
            "wau": one("SELECT COUNT(DISTINCT device_id) FROM device_days WHERE day >= ?", d7),
            "mau": one("SELECT COUNT(DISTINCT device_id) FROM device_days WHERE day >= ?", d30),
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
            FROM devices GROUP BY name ORDER BY value DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def series(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    days = max(1, min(days, 90))
    start = (security.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    new_devices = {
        r["day"]: r["value"] for r in conn.execute(
            """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS value
               FROM devices WHERE created_at >= ? GROUP BY day""",
            (start,),
        )
    }
    active = {
        r["day"]: r["value"] for r in conn.execute(
            """SELECT day, COUNT(DISTINCT device_id) AS value
               FROM device_days WHERE day >= ? GROUP BY day""",
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


def devices(conn: sqlite3.Connection, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    sql = """SELECT d.id, d.installation_id, d.platform, d.app_version, d.device_serial,
                    d.status, d.created_at, d.last_seen,
                    (SELECT COUNT(*) FROM sessions s WHERE s.device_id = d.id
                       AND s.revoked_at IS NULL) AS sessions
             FROM devices d"""
    args: tuple = ()
    if query:
        sql += " WHERE d.device_serial LIKE ? OR d.installation_id LIKE ?"
        args = (f"%{query}%", f"%{query}%")
    sql += " ORDER BY d.last_seen DESC LIMIT ?"
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


def audit(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
