"""配置：只从运行目录的 data/.env 读，缺文件时自动生成随机密钥。

设计对齐掌上徐工控制台：
- 不读系统环境变量（避免部署环境差异）
- 首次启动生成 token_secret / admin_key，写回 .env
- .env 权限 0600，不进 git
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


class Config:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parent.parent
        self.data_dir = self.root / "data"
        self.content_dir = self.root / "content"
        self.upload_dir = self.data_dir / "uploads"
        self.env_path = self.data_dir / ".env"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        values = self._load_env()
        self.token_secret: str = values.get("TOKEN_SECRET") or self._ensure("TOKEN_SECRET")
        self.admin_key: str = values.get("ADMIN_KEY") or self._ensure("ADMIN_KEY")
        self.db_path = self.data_dir / "control.db"
        self.timezone_offset_hours = 8

        # 时限（秒）
        self.device_sign_window = 300          # 设备登记签名时间窗 ±5 分钟
        self.challenge_ttl = 300               # challenge 5 分钟一次性
        self.access_ttl = 15 * 60              # access 15 分钟
        self.refresh_ttl = 30 * 24 * 3600      # refresh 30 天
        self.event_retention_days = 90

    # ---- .env ----
    def _load_env(self) -> dict[str, str]:
        if not self.env_path.exists():
            self._write_env({})
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass
            return {}
        values: dict[str, str] = {}
        for line in self.env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def _ensure(self, key: str) -> str:
        """补写缺失的密钥，返回新值。"""
        values = self._load_env()
        values.setdefault(key, secrets.token_urlsafe(32))
        self._write_env(values)
        return values[key]

    def _write_env(self, values: dict[str, str]) -> None:
        lines = ["# 掌上锡院控制台配置（勿提交）"]
        for key, value in values.items():
            lines.append(f"{key}={value}")
        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
