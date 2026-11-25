"""SQLite 层：建表与轻量查询助手（WAL，单机）。

表结构对齐掌上徐工控制台，裁掉与内容运营相关的部分：
devices / device_tokens / users / sessions / challenges / events /
device_days / risk_events / audit_logs
"""

from __future__ import annotations

import sqlite3
import threading
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
    student_hash  TEXT UNIQUE NOT NULL,   -- 旧客户端兼容键；新账号写 v2:<account_key>
    student_alias TEXT NOT NULL,          -- 对外的不可反推短标签（8 位十六进制）
    account_key   TEXT NOT NULL DEFAULT '', -- HMAC(ACCOUNT_KEY_SECRET, 客户端伪名)
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
    object_key  TEXT NOT NULL DEFAULT '',
    sha256      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(upload_id) REFERENCES resource_uploads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_resource_files_upload ON resource_files(upload_id);

CREATE TABLE IF NOT EXISTS download_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL,
    user_id     INTEGER,
    file_id     INTEGER NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    mode        TEXT NOT NULL DEFAULT 'download',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_download_events_device_time
    ON download_events(device_id, created_at);
CREATE INDEX IF NOT EXISTS idx_download_events_time
    ON download_events(created_at);

-- 资源发布 outbox：审核通过/下架只入队，独立 worker 消费并重试。
CREATE TABLE IF NOT EXISTS resource_publish_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL,
    action      TEXT NOT NULL,                 -- publish / delete
    status      TEXT NOT NULL DEFAULT 'pending', -- pending / running / done / failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    next_run_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resource_jobs_queue
    ON resource_publish_jobs(status, next_run_at, id);
CREATE INDEX IF NOT EXISTS idx_resource_jobs_upload
    ON resource_publish_jobs(upload_id, id);

-- 资源中心可观测性：按天累计，不保留每个请求的明细，避免无限膨胀。
CREATE TABLE IF NOT EXISTS resource_daily_metrics (
    day    TEXT NOT NULL,
    metric TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(day, metric)
);

CREATE TABLE IF NOT EXISTS resource_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL,
    file_id     INTEGER NOT NULL DEFAULT 0,
    device_id   INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    handled_at  TEXT,
    handle_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resource_reports_status
    ON resource_reports(status, id);

-- ── 邀请拉新（2026-11-17）─────────────────────────────────────────
-- 邀请码：一个用户一条有效码；code 为分享凭证（不含学号/姓名），可明文存。
CREATE TABLE IF NOT EXISTS invite_codes (
    code         TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL UNIQUE,
    device_id    INTEGER,
    status       TEXT NOT NULL DEFAULT 'active',  -- active / removed
    used_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    expires_at   TEXT
);

-- 邀请关系：一个被邀请人仅一条关系（唯一索引兜底），状态机
-- pending → activated；异常绑定置 invalid 并记 invalid_reason。
CREATE TABLE IF NOT EXISTS invite_relations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL,
    inviter_user_id    INTEGER NOT NULL,
    invitee_user_id    INTEGER NOT NULL,
    invitee_device_id  INTEGER,
    source             TEXT NOT NULL DEFAULT 'share',
    status             TEXT NOT NULL DEFAULT 'pending', -- pending/activated/invalid/revoked
    invalid_reason     TEXT NOT NULL DEFAULT '',
    bound_at           TEXT NOT NULL,
    activated_at       TEXT,
    reward_state       TEXT NOT NULL DEFAULT 'none',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_rel_inviter ON invite_relations(inviter_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_invite_rel_invitee
    ON invite_relations(invitee_user_id);

CREATE TABLE IF NOT EXISTS invite_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER,
    event_type  TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_events_rel ON invite_events(relation_id);

-- App 体验评分（2026-11-18）：课表页累计进入 3 次时的一次五星简易反馈。
-- user_id/device_id 只用于「同一用户同一版本只算一次」与幂等，不存学号/姓名；
-- 对外统计只输出版本/平台/平均分/分布。
CREATE TABLE IF NOT EXISTS app_ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    device_id   INTEGER,
    rating      INTEGER NOT NULL,
    app_version TEXT NOT NULL DEFAULT '',
    platform    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_ratings_time ON app_ratings(created_at);

-- 班级课表（2026-11-25）：同学上传本班课表，审核通过并发布后，别的同学能直接导入。
-- 不建附件表：文件在客户端就解析成结构化课程，服务端只存结果，磁盘上没有可被
-- 越权下载的原件。同班多份并存是产品要求（所以不覆盖旧行，version 递增）。
CREATE TABLE IF NOT EXISTS timetable_uploads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      INTEGER NOT NULL,
    contributor    TEXT NOT NULL DEFAULT '',
    uploader_name  TEXT NOT NULL DEFAULT '',
    class_key      TEXT NOT NULL DEFAULT '',
    xnm            TEXT NOT NULL,
    xqm            TEXT NOT NULL,
    jgmc           TEXT NOT NULL,
    zymc           TEXT NOT NULL,
    bj             TEXT NOT NULL,
    njdm_id        TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    course_count   INTEGER NOT NULL DEFAULT 0,
    diff_matched   INTEGER NOT NULL DEFAULT 0,
    diff_total     INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    campus_baseline TEXT NOT NULL DEFAULT '',
    personal_flag  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending',
    published      INTEGER NOT NULL DEFAULT 0,
    review_note    TEXT NOT NULL DEFAULT '',
    review_reason  TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    reviewed_at    TEXT,
    published_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tt_uploads_class
    ON timetable_uploads(class_key, version);
CREATE INDEX IF NOT EXISTS idx_tt_uploads_gate
    ON timetable_uploads(status, published);
CREATE INDEX IF NOT EXISTS idx_tt_uploads_device
    ON timetable_uploads(device_id, created_at);

CREATE TABLE IF NOT EXISTS timetable_courses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id  INTEGER NOT NULL,
    name       TEXT NOT NULL,
    teacher    TEXT NOT NULL DEFAULT '',
    classroom  TEXT NOT NULL DEFAULT '',
    weekday    INTEGER NOT NULL,
    start_slot INTEGER NOT NULL,
    end_slot   INTEGER NOT NULL,
    weeks      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tt_courses_upload ON timetable_courses(upload_id);

-- 收藏的是「班级引用」，不是课表副本：同班出了新版，收藏仍然指向这个班。
CREATE TABLE IF NOT EXISTS timetable_favorites (
    device_id  INTEGER NOT NULL,
    class_key  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, class_key)
);

-- 发布 outbox。课表没有文件要传，发布只是把可见性闸门打开，
-- 但仍然走队列：审核接口不能因为一次写失败就把「已通过」变成「看不到」。
CREATE TABLE IF NOT EXISTS timetable_publish_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL,
    action      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    next_run_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tt_jobs_status
    ON timetable_publish_jobs(status, next_run_at);
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
        # 账号键：2026-11-20 起不再把可枚举的学号哈希当账号主键。
        _ensure_column(conn, "users", "account_key",
                       "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_account_key
               ON users(account_key) WHERE account_key != ''"""
        )
        _ensure_column(conn, "resource_files", "kind",
                       "TEXT NOT NULL DEFAULT 'exam_paper'")
        _ensure_column(conn, "resource_files", "downloads",
                       "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "resource_files", "parent_id",
                       "INTEGER NOT NULL DEFAULT 0")
        # COS 对象键：空串 = 还在本地磁盘，只有已成功上传的文件才写值。
        # 下载接口优先 307 跳 CDN，本地文件作为回退/审核前预览。
        _ensure_column(conn, "resource_files", "object_key",
                       "TEXT NOT NULL DEFAULT ''")
        # 文件内容 SHA-256：上传时计算，发布 worker 为历史文件补齐。
        # 下载/对账用它判断三处副本是否真正一致，而不是只看大小。
        _ensure_column(conn, "resource_files", "sha256",
                       "TEXT NOT NULL DEFAULT ''")
        # 下载额度计数口径（2026-11-20）：登录后按 user_id 跨设备累计；
        # 未登录才退回 device_id。旧记录 user_id 为空，由查询侧做设备兜底。
        _ensure_column(conn, "download_events", "user_id", "INTEGER")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_download_events_user_time
               ON download_events(user_id, created_at)"""
        )
        # 贡献者署名：上传时用户自愿填，空串 = 匿名（客户端不展示）。
        _ensure_column(conn, "resource_uploads", "contributor",
                       "TEXT NOT NULL DEFAULT ''")
        # 设备去重（2026-11-01）：归档重复设备时记下「并到哪台」，0 = 没归档。
        # 归档只改 status='removed' 并撤销令牌/会话，**不删行** ——
        # resource_uploads.device_id 还指着它，删了会丢上传归属。
        _ensure_column(conn, "devices", "merged_into",
                       "INTEGER NOT NULL DEFAULT 0")
        # 评分幂等：同一用户在同一版本/平台只保留最后一次评分。
        # 先清历史重复再建唯一索引，避免旧库里有重复行导致启动失败。
        conn.execute(
            """DELETE FROM app_ratings
               WHERE id NOT IN (
                 SELECT MAX(id) FROM app_ratings
                 GROUP BY user_id, app_version, platform
               )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_app_ratings_user_version
               ON app_ratings(user_id, app_version, platform)"""
        )
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
    """极薄的连接包装：每次操作开新连接，省去跨线程问题。

    连接**按线程**隔离（2026-11-03 修）。原先存在 `self.conn` 这个实例属性上，
    而全进程只有一个 DB 实例（main.py 的 app.state.db），FastAPI 的同步路由
    又跑在线程池里 —— 两个并发请求会互相覆盖 `self.conn`，`__exit__` 于是
    commit/close 了**别人的**连接，自己那笔未提交的写事务连人带锁留在 WAL 里：

      1) 被关掉那一路抛 sqlite3.ProgrammingError（SQLite objects created in a
         thread can only be used in that same thread）→ 秒回 500，读路由 ~50%;
      2) 泄漏的写事务一直持有写锁，后续所有写请求卡满 busy_timeout(10s) 再抛
         database is locked → 慢回 500，写路由 100%，且不重启不自愈。

    不要改成 `check_same_thread=False` 绕过：那只是把报错换成静默的跨线程
    使用同一个连接，事务边界会互相穿插。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def __enter__(self) -> sqlite3.Connection:
        conn = connect(self.path)
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        stack.append(conn)
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        stack = getattr(self._local, "stack", None)
        if not stack:
            return
        conn = stack.pop()
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()
