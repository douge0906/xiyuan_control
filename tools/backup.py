#!/usr/bin/env python3
"""SQLite 在线备份（不用 cp，避免 WAL 半提交）。

用法：
    python tools/backup.py            # 备份一次，保留最近 14 份
    python tools/backup.py --keep 7   # 自定义保留份数

cron（每天 03:10）：
    10 3 * * * cd /opt/xiyuan-control && ./venv/bin/python tools/backup.py >> data/backup.log 2>&1
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "control.db"
BACKUP_DIR = ROOT / "data" / "backup"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=14, help="保留最近多少份")
    args = parser.parse_args()
    keep = max(1, min(args.keep, 180))

    if not DB.exists():
        print(f"database not found: {DB}", file=sys.stderr)
        return 1
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"control-{stamp}.db"

    src = sqlite3.connect(DB)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()

    files = sorted(BACKUP_DIR.glob("control-*.db"), key=lambda p: p.name, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass

    print(f"backup ok: {target} ({target.stat().st_size} bytes), kept {min(len(files), keep)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
