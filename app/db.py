"""SQLite 层：建表与轻量查询助手（WAL，单机）。

表结构对齐掌上徐工控制台，裁掉与内容运营相关的部分：
devices / device_tokens / users / sessions / challenges / events /
device_days / risk_events / audit_logs
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id TEXT UNIQUE NOT NULL,
    public_key      TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT '',
    app_version     TEXT NOT NULL DEFAULT '',
    device_serial   TEXT UNIQUE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active / banned
    created_at      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_used  TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_device ON device_tokens(device_id);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_hash  TEXT UNIQUE NOT NULL,   -- SHA-256(学号)，不存明文
    student_alias TEXT NOT NULL,          -- 固定伪名，展示与业务用
    display_name  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',  -- active / banned
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    device_id    INTEGER NOT NULL,
    access_hash  TEXT UNIQUE NOT NULL,
    access_exp   TEXT NOT NULL,
    refresh_hash TEXT UNIQUE NOT NULL,
    refresh_exp  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);

CREATE TABLE IF NOT EXISTS challenges (
    id         TEXT PRIMARY KEY,
    device_id  INTEGER NOT NULL,
    nonce      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    event_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    ts        TEXT NOT NULL,
    UNIQUE(device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS device_days (
    device_id INTEGER NOT NULL,
    day       TEXT NOT NULL,
    PRIMARY KEY (device_id, day)
);

CREATE TABLE IF NOT EXISTS risk_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    device_id  INTEGER,
    user_id    INTEGER,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    cleared_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT 'admin',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER,
    content     TEXT NOT NULL,
    contact     TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',   -- new / read / done
    created_at  TEXT NOT NULL,
    handled_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, id);

CREATE TABLE IF NOT EXISTS resource_uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL,
    course_name TEXT NOT NULL,
    book_name   TEXT NOT NULL DEFAULT '',
    area        TEXT NOT NULL,                 -- textbook / exam_paper / answer
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending / approved / rejected
    created_at  TEXT NOT NULL,
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resource_uploads_status
    ON resource_uploads(status, id);

CREATE TABLE IF NOT EXISTS resource_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL,
    name        TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size        INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'exam_paper',
    downloads   INTEGER NOT NULL DEFAULT 0,
    parent_id   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(upload_id) REFERENCES resource_uploads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_resource_files_upload ON resource_files(upload_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "resource_files", "kind",
                       "TEXT NOT NULL DEFAULT 'exam_paper'")
        _ensure_column(conn, "resource_files", "downloads",
                       "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "resource_files", "parent_id",
                       "INTEGER NOT NULL DEFAULT 0")
        # 旧版只按 uploads.area 记类型；补列后把教材/答案的旧记录纠正过来。
        conn.execute(
            """UPDATE resource_files
               SET kind = (SELECT area FROM resource_uploads
                           WHERE id = resource_files.upload_id)
               WHERE kind = 'exam_paper'
                 AND upload_id IN (
                   SELECT id FROM resource_uploads
                   WHERE area IN ('textbook', 'answer')
                 )"""
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class DB:
    """极薄的连接包装：每次操作开新连接，省去跨线程问题。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> sqlite3.Connection:
        self.conn = connect(self.path)
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
