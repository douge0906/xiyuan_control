"""资源中心 EdgeOne 增量预热：只补 MISS，避免重复拉取打满源站。

背景：全量无脑预热会让 EdgeOne 多节点对同一文件重复回源，源站出口只有
约 5Mbps，153 个文件足以把真实用户下载压到 30KB/s。因此这里改成：

1. 只处理体积小于 COS 直连阈值（默认 32MB）的文件——更大的文件走 COS，
   预热没有意义，也最容易拖垮源站；
2. 提交前先 HEAD 一次，`EO-Cache-Status: HIT` 的直接跳过，只对 MISS 预热，
   日常重预热通常只剩个位数文件；
3. 小批提交（默认 10 条一批），批间 sleep，给真实用户让带宽。

用法（生产）：
    cd /opt/xiyuan-control
    PYTHONPATH=/opt/xiyuan-control venv/bin/python -m app.prefetch_all
    ... --dry-run / --limit 5 / --force（跳过 HEAD 全量重推）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .config import Config  # noqa: E402
from .db import DB, init as init_db  # noqa: E402
from .edge_api import prefetch_urls, sign_url  # noqa: E402

logger = logging.getLogger("xiyuan_control.prefetch")

DEFAULT_MAX_SIZE = 8 * 1024 * 1024


def collect_rows(cfg: Config, dbx: DB, max_size: int) -> list[dict]:
    """全部已审核资源中，体积不超过阈值的那部分。"""
    if not cfg.edge_resource_base:
        return []
    with dbx as conn:
        rows = conn.execute(
            """SELECT f.upload_id, f.stored_name, f.size
               FROM resource_files f
               JOIN resource_uploads u ON u.id = f.upload_id
               WHERE u.status = 'approved'
               ORDER BY f.size ASC, f.id"""
        ).fetchall()
    return [
        dict(row)
        for row in rows
        if row["stored_name"] and 0 < int(row["size"] or 0) <= max_size
    ]


def edge_url(cfg: Config, row: dict) -> str:
    base = cfg.edge_resource_base.rstrip("/")
    return sign_url(cfg, f"{base}/{row['upload_id']}/{row['stored_name']}")


def is_cached(url: str, timeout: float = 10.0) -> bool:
    """HEAD 探一次边缘缓存；拿不到明确 HIT 一律按 MISS 处理。"""
    try:
        resp = httpx.head(url, timeout=timeout, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001 - 探测失败不应中断预热
        logger.warning("HEAD 失败，按 MISS 处理: %s", exc)
        return False
    if resp.status_code != 200:
        return False
    return (resp.headers.get("eo-cache-status") or "").upper() == "HIT"


def main() -> int:
    parser = argparse.ArgumentParser(description="资源中心 EdgeOne 增量预热")
    parser.add_argument("--dry-run", action="store_true", help="只统计不提交")
    parser.add_argument("--limit", type=int, default=0, help="最多提交多少条")
    parser.add_argument("--max-size", type=int, default=0,
                        help="超过该字节数的文件不预热（默认取 COS 直连阈值）")
    parser.add_argument("--batch", type=int, default=10, help="每批提交条数")
    parser.add_argument("--sleep", type=float, default=8.0,
                        help="批间休眠秒数")
    parser.add_argument("--force", action="store_true",
                        help="跳过 HEAD 检查，全部重推")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HEAD 探测会把每条 httpx 请求写进 cron 日志，压到 WARNING 只留结果。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    cfg = Config()
    dbx = DB(cfg.db_path)
    init_db(cfg.db_path)
    threshold = int(getattr(cfg, "resource_cos_direct_threshold", 0) or 0)
    max_size = args.max_size or threshold or DEFAULT_MAX_SIZE
    rows = collect_rows(cfg, dbx, max_size)
    if args.limit > 0:
        rows = rows[: args.limit]
    logger.info(
        "预热扫描 files=%s max_size=%s zone=%s",
        len(rows), max_size, cfg.edge_zone_id,
    )
    miss: list[str] = []
    hit = 0
    for row in rows:
        url = edge_url(cfg, row)
        if not args.force and is_cached(url):
            hit += 1
            continue
        miss.append(url)
    logger.info("缓存探测结束 hit=%s miss=%s", hit, len(miss))
    if args.dry_run:
        for url in miss[:5]:
            logger.info("sample %s", url.split("?")[0])
        return 0
    if not miss:
        logger.info("无需预热，全部命中")
        return 0
    submitted = 0
    failed = 0
    for index in range(0, len(miss), max(1, args.batch)):
        chunk = miss[index:index + max(1, args.batch)]
        try:
            result = prefetch_urls(cfg, chunk, batch_size=len(chunk))
            submitted += int(result.get("urls", 0))
            failed += int(result.get("failedCount", 0))
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响后续
            failed += len(chunk)
            logger.error("批量预热失败 batch=%s: %s", index // args.batch + 1, exc)
        if index + args.batch < len(miss):
            time.sleep(max(0.0, args.sleep))
    logger.info(
        "预热提交结束 submitted=%s failed=%s hit=%s",
        submitted, failed, hit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
