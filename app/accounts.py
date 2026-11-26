"""账号键迁移：把可枚举的学号哈希换成 HMAC(secret, alias)。

背景：2026-11-20 之前 users.student_hash = SHA256(学号)，student_alias =
SHA256(固定前缀 + 学号)，两者都能被离线枚举还原。现在客户端只在本地
算 alias，服务端只存 HMAC(secret, alias)，库泄漏也无法反推。

迁移只做一次，幂等：
- 老用户：拿 64 位 alias 算出 account_key，改写 student_hash 与展示标签；
- 已迁移：account_key 非空、student_alias 是 8 位标签，跳过；
- 新库：没有历史行，直接跳过。
"""

from __future__ import annotations

import logging
from typing import Any

from . import security

logger = logging.getLogger("xiyuan_control.accounts")


def _looks_like_alias(value: str) -> bool:
    return security.is_alias(value or "")


def migrate(cfg: Any, dbx: Any) -> None:
    with dbx as conn:
        rows = conn.execute(
            """SELECT id, student_hash, student_alias, account_key
               FROM users ORDER BY id"""
        ).fetchall()
        migrated = 0
        skipped = 0
        for row in rows:
            if row["account_key"]:
                skipped += 1
                continue
            full_alias = str(row["student_alias"] or "")
            if not _looks_like_alias(full_alias):
                # 不是旧版 64 位伪名（例如已是短标签但没有 key）——留给
                # 下次登录用客户端带来的完整 alias 补齐，启动阶段不猜。
                logger.warning("账号迁移跳过：user=%s alias 不是完整伪名",
                               row["id"])
                skipped += 1
                continue
            key = security.account_key(cfg.account_key_secret, full_alias)
            tag = security.alias_tag(key)
            clash = conn.execute(
                "SELECT id FROM users WHERE account_key = ?", (key,)
            ).fetchone()
            if clash is not None and clash["id"] != row["id"]:
                logger.warning("账号迁移冲突：user=%s 与 user=%s 同键，保留原账号",
                               row["id"], clash["id"])
                skipped += 1
                continue
            conn.execute(
                """UPDATE users
                   SET account_key = ?, student_alias = ?, student_hash = ?
                   WHERE id = ?""",
                (key, tag, "v2:" + key, row["id"]),
            )
            migrated += 1
        if migrated:
            logger.info("账号键迁移完成：migrated=%s skipped=%s", migrated, skipped)
