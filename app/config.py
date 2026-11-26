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

try:  # Windows 本地调试没有 fcntl；服务器（Linux）用它串行化首次密钥生成
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


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
        # 账号键 HMAC：与令牌密钥分离，避免轮换一方影响另一方。
        self.account_key_secret: str = (
            values.get("ACCOUNT_KEY_SECRET") or self._ensure("ACCOUNT_KEY_SECRET")
        )
        self.admin_key: str = values.get("ADMIN_KEY") or self._ensure("ADMIN_KEY")
        self.db_path = self.data_dir / "control.db"
        self.timezone_offset_hours = 8

        # 反馈微信推送令牌（ShowDoc）。可选：没配就不推，不影响反馈落库。
        self.feedback_showdoc_token: str = values.get("FEEDBACK_SHOWDOC_TOKEN", "")

        # 邀请/彩蛋开关：默认开启，部署时可在 data/.env 改。
        self.invite_active: bool = values.get("INVITE_ACTIVE", "1") not in (
            "0", "false", "False", "off", "OFF",
        )
        self.egg_enabled: bool = values.get("EGG_ENABLED", "1") not in (
            "0", "false", "False", "off", "OFF",
        )

        # 资源中心对象存储：COS 存文件，CDN 域名负责回源/边缘缓存。
        # 未配置 SecretId/SecretKey 或 COS_ENABLED 非真时，完全沿用本地磁盘，
        # 不影响旧部署；配置完整后才把已审核文件异步推到 COS。
        self.cos_enabled = (
            values.get("COS_ENABLED", "0").strip().lower()
            in ("1", "true", "yes", "on")
            and bool(values.get("COS_SECRET_ID", "").strip())
            and bool(values.get("COS_SECRET_KEY", "").strip())
        )
        self.cos_secret_id = values.get("COS_SECRET_ID", "").strip()
        self.cos_secret_key = values.get("COS_SECRET_KEY", "").strip()
        self.cos_bucket = values.get(
            "COS_BUCKET", "xiyuanzyzx-1313027026"
        ).strip()
        self.cos_region = values.get("COS_REGION", "ap-nanjing").strip()
        self.cos_prefix = values.get("COS_PREFIX", "resources").strip().strip("/")
        self.cos_public_base = values.get(
            "COS_PUBLIC_BASE",
            f"https://{self.cos_bucket}.cos.{self.cos_region}.myqcloud.com",
        ).strip().rstrip("/")

        # 现有 EdgeOne 静态缓存旁路：COS 凭证未就绪时，已审核文件也能走 CDN。
        # EDGE_RESOURCE_BASE 是公网前缀，EDGE_PUBLIC_DIR 是 Nginx 静态目录；
        # 两者同时配置才启用。常规文件优先 EdgeOne，超大文件见下方直连阈值。
        self.edge_resource_base = values.get(
            "EDGE_RESOURCE_BASE", ""
        ).strip().rstrip("/")
        edge_dir = values.get("EDGE_PUBLIC_DIR", "").strip()
        self.edge_public_dir = Path(edge_dir) if edge_dir else None

        # EdgeOne Token 鉴权（Type D）。开启后下载接口只为每次 307 生成短时签名，
        # 公开直链不再永久可用。Secret 只放服务端 data/.env，不下发客户端。
        self.edge_token_auth = values.get(
            "EDGE_TOKEN_AUTH", "0"
        ).strip().lower() in ("1", "true", "yes", "on")
        self.edge_token_secret = values.get("EDGE_TOKEN_SECRET", "").strip()
        self.edge_token_ttl = max(60, min(86400, int(
            values.get("EDGE_TOKEN_TTL", "900") or 900
        )))
        self.edge_token_auth_param = values.get(
            "EDGE_TOKEN_AUTH_PARAM", "sign"
        ).strip() or "sign"
        self.edge_token_time_param = values.get(
            "EDGE_TOKEN_TIME_PARAM", "t"
        ).strip() or "t"
        self.edge_zone_id = values.get("EDGE_ZONE_ID", "").strip()

        # 资源中心每日下载额度：登录后按账号跨设备累计，未登录按设备累计。
        #   基础额度 5 次/天；
        #   每成功邀请 1 人，额外加 10 次/天（按邀人账号累计，设上限防滥用）。
        self.resource_daily_download_limit = max(1, int(
            values.get("RESOURCE_DAILY_DOWNLOAD_LIMIT", "5") or 5
        ))
        self.resource_invite_bonus_per_invite = max(0, int(
            values.get("RESOURCE_INVITE_BONUS_PER_INVITE", "10") or 10
        ))
        self.resource_invite_bonus_limit = max(0, int(
            values.get("RESOURCE_INVITE_BONUS_LIMIT", "1000") or 1000
        ))
        # EdgeOne 对超大文件的回源/缓存存在实际边界：源站带宽只有约
        # 0.42 MiB/s 时，大文件首次回源要 50–100 秒，38 MB 虽能缓存但首单
        # 仍很慢，47 MB 则连续 MISS。为避免教材类文件首次下载等待，超过
        # 8 MiB 的文件直接下发 COS 预签名，走对象存储的高带宽链路；
        # 小文件继续用 EdgeOne 缓存。阈值设为 0 表示关闭直连、全部走 EdgeOne。
        self.resource_cos_direct_threshold = max(0, int(
            values.get("RESOURCE_COS_DIRECT_THRESHOLD", "8388608") or 8388608
        ))
        self.resource_worker_poll_seconds = max(2, int(
            values.get("RESOURCE_WORKER_POLL_SECONDS", "8") or 8
        ))
        self.resource_worker_backfill = values.get(
            "RESOURCE_WORKER_BACKFILL", "1"
        ).strip().lower() in ("1", "true", "yes", "on")

        # 时限（秒）
        self.device_sign_window = 300          # 设备登记签名时间窗 ±5 分钟
        self.challenge_ttl = 300               # challenge 5 分钟一次性
        self.access_ttl = 15 * 60              # access 15 分钟
        self.refresh_ttl = 30 * 24 * 3600      # refresh 30 天
        self.event_retention_days = 90

    # ---- .env ----
    def _load_env(self) -> dict[str, str]:
        if not self.env_path.exists():
            # 只读；首次落盘统一由 _ensure 在 .env.lock 内完成，避免多进程
            # 同时创建临时文件互相覆盖。
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
        """补写缺失的密钥，返回最终生效值。

        control 与 resource_worker 会同时启动；如果两边都在缺失时各自生成
        一个密钥，后写的会覆盖先写的，而先启动的进程内存里仍是旧值，导致
        ACCOUNT_KEY_SECRET 不一致、账号全部对不上。这里用 .env.lock 串行化，
        拿到锁后重新读一次，确保所有进程返回文件里的同一个值。
        """
        values = self._load_env()
        if values.get(key):
            return values[key]

        lock_path = self.data_dir / ".env.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                values = self._load_env()
                if not values.get(key):
                    values[key] = secrets.token_urlsafe(32)
                    self._write_env(values)
                return values[key]
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_env(self, values: dict[str, str]) -> None:
        lines = ["# 掌上锡院控制台配置（勿提交）"]
        for key, value in values.items():
            lines.append(f"{key}={value}")
        tmp = self.data_dir / ".env.tmp"
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.env_path)
