"""EdgeOne 缓存刷新：资源下架/替换后，让 edge 节点不再返回旧文件。

腾讯云 TEO API 使用 TC3-HMAC-SHA256 签名；这里用标准库 + httpx 实现，
不要求生产 venv 安装 tencentcloud SDK。凭据复用 COS 子账号，但在 CAM 中必须
额外授予 teo:CreatePurgeTask（见部署说明）。未配置 zone/凭据时自动跳过。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .config import Config

logger = logging.getLogger("xiyuan_control.edge")

_HOST = "teo.tencentcloudapi.com"
_SERVICE = "teo"
_VERSION = "2022-09-01"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _call(cfg: Config, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not (cfg.edge_zone_id and cfg.cos_secret_id and cfg.cos_secret_key):
        raise RuntimeError("EdgeOne zone/凭据未配置")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stamp = int(time.time())
    date = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
    canonical_headers = (
        "content-type:application/json; charset=utf-8\n"
        f"host:{_HOST}\n"
    )
    signed_headers = "content-type;host"
    canonical_request = (
        "POST\n/\n\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{_sha256_hex(body.encode('utf-8'))}"
    )
    credential_scope = f"{date}/{_SERVICE}/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        f"{stamp}\n"
        f"{credential_scope}\n"
        f"{_sha256_hex(canonical_request.encode('utf-8'))}"
    )
    secret_date = _hmac(
        f"TC3{cfg.cos_secret_key}".encode("utf-8"), date
    )
    secret_service = _hmac(secret_date, _SERVICE)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={cfg.cos_secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": _HOST,
        "X-TC-Action": action,
        "X-TC-Version": _VERSION,
        "X-TC-Timestamp": str(stamp),
        "X-TC-Region": "",
    }
    response = httpx.post(
        f"https://{_HOST}/",
        content=body.encode("utf-8"),
        headers=headers,
        timeout=httpx.Timeout(connect=10.0, read=30.0,
                              write=30.0, pool=10.0),
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"TEO {action} HTTP {response.status_code} {response.text[:180]}"
        )
    data = response.json()
    error = data.get("Response", {}).get("Error")
    if error:
        raise RuntimeError(
            f"TEO {action} {error.get('Code')}: {error.get('Message')}"
        )
    return data.get("Response", {})


def sign_url(cfg: Config, url: str) -> str:
    """按 EdgeOne Type D 规则给静态 URL 追加短时签名。

    算法：MD5(SecretKey + Path + timestamp)，timestamp 为十六进制 Unix 秒。
    Token 鉴权未开启时原样返回，便于灰度回滚。
    """
    if not url:
        return url
    if not (
        getattr(cfg, "edge_token_auth", False)
        and getattr(cfg, "edge_token_secret", "")
    ):
        return url
    parts = urlsplit(url)
    path = parts.path
    stamp = format(int(time.time()), "x")
    digest = hashlib.md5(
        f"{cfg.edge_token_secret}{path}{stamp}".encode("utf-8")
    ).hexdigest()
    query = parts.query
    sep = "&" if query else ""
    return (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        f"?{query}{sep}"
        f"{quote(cfg.edge_token_auth_param, safe='')}={digest}"
        f"&{quote(cfg.edge_token_time_param, safe='')}={stamp}"
        f"{('#' + parts.fragment) if parts.fragment else ''}"
    )


def purge_urls(cfg: Config, urls: list[str]) -> dict[str, Any]:
    """按 URL 刷新 EdgeOne；单次最多 20 条，URL 必须带协议。"""
    clean = [u.strip() for u in urls if u and u.strip().startswith("http")]
    if not clean:
        return {"ok": True, "skipped": True, "reason": "empty"}
    if not cfg.edge_zone_id:
        return {"ok": False, "skipped": True, "reason": "zone_not_configured"}
    jobs: list[dict[str, Any]] = []
    for index in range(0, len(clean), 20):
        chunk = clean[index:index + 20]
        resp = _call(cfg, "CreatePurgeTask", {
            "ZoneId": cfg.edge_zone_id,
            "Type": "purge_url",
            "Targets": chunk,
        })
        jobs.append({
            "jobId": resp.get("JobId", ""),
            "failed": resp.get("FailedList", []),
        })
        logger.info("EdgeOne 刷新已提交 job=%s urls=%s",
                    resp.get("JobId", ""), len(chunk))
    return {"ok": True, "skipped": False, "jobs": jobs}


def purge_upload(cfg: Config, dbx: Any, upload_id: int) -> dict[str, Any]:
    """刷新某批次的全部资源 URL（无 zone 配置时跳过）。"""
    if not (cfg.edge_resource_base and cfg.edge_zone_id):
        return {"ok": False, "skipped": True, "reason": "edge_not_configured"}
    with dbx as conn:
        names = [
            str(row["stored_name"])
            for row in conn.execute(
                """SELECT stored_name FROM resource_files
                   WHERE upload_id = ? ORDER BY id""",
                (upload_id,),
            ).fetchall()
        ]
    return purge_keys(cfg, upload_id, names)


def purge_keys(cfg: Config, upload_id: int, names: list[str]) -> dict[str, Any]:
    base = cfg.edge_resource_base.rstrip("/")
    urls = [f"{base}/{upload_id}/{name}" for name in names if name]
    return purge_urls(cfg, urls)


def prefetch_urls(
    cfg: Config, urls: list[str], *, batch_size: int = 100
) -> dict[str, Any]:
    """按 URL 预热 EdgeOne 缓存（CreatePrefetchTask）。

    预热是"让边缘节点提前回源拉取"，因此会占源站上行带宽；个人版单次
    最多 1000 条、每日 5000 条，这里按 100 条一批提交，便于失败定位。
    URL 必须带协议；站点开启 Type D 鉴权时，必须先由 sign_url 签名，
    否则预热请求会被 403，任务会直接失败。
    """
    clean = [u.strip() for u in urls if u and u.strip().startswith("http")]
    if not clean:
        return {"ok": True, "skipped": True, "reason": "empty"}
    if not cfg.edge_zone_id:
        return {"ok": False, "skipped": True, "reason": "zone_not_configured"}
    jobs: list[dict[str, Any]] = []
    failed_all: list[Any] = []
    for index in range(0, len(clean), batch_size):
        chunk = clean[index:index + batch_size]
        resp = _call(cfg, "CreatePrefetchTask", {
            "ZoneId": cfg.edge_zone_id,
            "Targets": chunk,
        })
        failed = resp.get("FailedList") or []
        failed_all.extend(failed)
        jobs.append({
            "jobId": resp.get("JobId", ""),
            "submitted": len(chunk) - len(failed),
            "failed": failed,
        })
        logger.info("EdgeOne 预热已提交 job=%s urls=%s failed=%s",
                    resp.get("JobId", ""), len(chunk), len(failed))
    return {
        "ok": True,
        "skipped": False,
        "urls": len(clean),
        "jobs": jobs,
        "failedCount": len(failed_all),
    }


def prefetch_upload(cfg: Config, dbx: Any, upload_id: int) -> dict[str, Any]:
    """预热某批次全部已审核资源 URL（自带签名）。"""
    if not (cfg.edge_resource_base and cfg.edge_zone_id):
        return {"ok": False, "skipped": True, "reason": "edge_not_configured"}
    with dbx as conn:
        names = [
            str(row["stored_name"])
            for row in conn.execute(
                """SELECT stored_name FROM resource_files
                   WHERE upload_id = ? ORDER BY id""",
                (upload_id,),
            ).fetchall()
        ]
    base = cfg.edge_resource_base.rstrip("/")
    urls = [
        sign_url(cfg, f"{base}/{upload_id}/{name}")
        for name in names if name
    ]
    return prefetch_urls(cfg, urls)
