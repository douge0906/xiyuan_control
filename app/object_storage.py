"""COS 对象存储（标准库签名，不引入额外 SDK）。

只做资源中心需要的四件事：上传、存在性检查、删除、下载到本地。
COS 的 S3/XML API 签名版本 v5 用 HMAC-SHA1，算法本身不复杂；这里自己实现，
避免为了一个上传适配器给服务器 venv 增加一整套 requests/加密依赖。

配置全部来自 Config.data/.env，见 README「资源中心对象存储」。
未启用时所有公开方法都返回 disabled / no-op，调用方继续走本地磁盘。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config

logger = logging.getLogger("xiyuan_control.cos")

# COS 允许参与签名的普通头；x-cos-* 另外单独纳入。
_SIGNABLE_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-type",
    "content-md5",
    "content-length",
    "expect",
    "expires",
    "host",
    "if-match",
    "if-modified-since",
    "if-none-match",
    "if-unmodified-since",
    "origin",
    "range",
    "transfer-encoding",
}

# 资源文件长期不变，允许 EdgeOne 和客户端缓存一年；对象 ACL 仍为私有，
# 公开访问只通过短时签名 URL（COS 预签名 / EdgeOne Token）。
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class CosError(RuntimeError):
    """COS 请求失败。调用方应回退本地，不要把用户上传整体打挂。"""


def _q(value: str) -> str:
    """COS 签名要求：-_.~ 不编码，空格编码成 %20，其余按 URL 编码。"""
    return quote(value, safe="-_.~")


def _content_type(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


class CosStorage:
    def __init__(self, cfg: Config) -> None:
        self.enabled = bool(cfg.cos_enabled)
        self.secret_id = cfg.cos_secret_id
        self.secret_key = cfg.cos_secret_key
        self.bucket = cfg.cos_bucket
        self.region = cfg.cos_region
        self.host = f"{self.bucket}.cos.{self.region}.myqcloud.com"
        self.prefix = cfg.cos_prefix or "resources"
        self.public_base = (cfg.cos_public_base or f"https://{self.host}").rstrip("/")
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------ 基础
    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=10.0, read=180.0, write=600.0, pool=10.0
                ),
                follow_redirects=False,
            )
        return self._client

    def object_key(self, upload_id: int, stored_name: str) -> str:
        return f"{self.prefix}/{upload_id}/{stored_name}"

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{quote(key, safe='/')}"

    def presigned_url(self, key: str, *, expire: int = 600,
                      method: str = "GET") -> str:
        """生成 COS 预签名 URL（默认 GET，短时有效）。

        对象 ACL 已设为私有，客户端只能拿这条链接下载，不再有永久公开直链。
        这里只签 host，不带额外请求头，调用方直接 GET 即可。
        """
        if not self.enabled:
            raise CosError("COS 未启用")
        now = int(time.time())
        sign_time = f"{now - 60};{now + max(60, int(expire))}"
        path = "/" + key.lstrip("/")
        sign_headers = {"host": _q(self.host)}
        header_list = "host"
        http_string = (
            f"{method.lower()}\n"
            f"{path}\n"
            "\n"
            f"host={sign_headers['host']}\n"
        )
        sha1 = hashlib.sha1(http_string.encode("utf-8")).hexdigest()
        string_to_sign = f"sha1\n{sign_time}\n{sha1}\n"
        sign_key = hmac.new(
            self.secret_key.encode("utf-8"),
            sign_time.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        signature = hmac.new(
            sign_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        query = (
            "q-sign-algorithm=sha1"
            f"&q-ak={_q(self.secret_id)}"
            f"&q-sign-time={quote(sign_time, safe=';')}"
            f"&q-key-time={quote(sign_time, safe=';')}"
            f"&q-header-list={header_list}"
            "&q-url-param-list="
            f"&q-signature={signature}"
        )
        return f"{self._url(key)}?{query}"

    # ------------------------------------------------------------ 签名
    def _signature(
        self,
        method: str,
        key: str,
        headers: dict[str, str],
        *,
        sign_time: str | None = None,
        expire: int = 900,
    ) -> str:
        """返回 Authorization 头；headers 是实际要发送的请求头。"""
        if not self.enabled:
            raise CosError("COS 未启用")

        now = int(time.time())
        if sign_time is None:
            sign_time = f"{now - 60};{now + expire}"

        path = "/" + key.lstrip("/")
        sign_headers: dict[str, str] = {"host": _q(self.host)}
        for name, value in headers.items():
            lower = name.lower()
            if lower in _SIGNABLE_HEADERS or lower.startswith("x-cos-"):
                sign_headers[lower] = _q(str(value))
        header_list = "&".join(
            f"{name}={sign_headers[name]}" for name in sorted(sign_headers)
        )
        http_string = (
            f"{method.lower()}\n"
            f"{path}\n"
            "\n"
            f"{header_list}\n"
        )
        sha1 = hashlib.sha1(http_string.encode("utf-8")).hexdigest()
        string_to_sign = f"sha1\n{sign_time}\n{sha1}\n"
        sign_key = hmac.new(
            self.secret_key.encode("utf-8"),
            sign_time.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        signature = hmac.new(
            sign_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        return (
            "q-sign-algorithm=sha1"
            f"&q-ak={_q(self.secret_id)}"
            f"&q-sign-time={sign_time}"
            f"&q-key-time={sign_time}"
            f"&q-header-list={';'.join(sorted(sign_headers))}"
            "&q-url-param-list="
            f"&q-signature={signature}"
        )

    def _request_headers(
        self,
        method: str,
        key: str,
        extra: dict[str, str] | None = None,
        *,
        expire: int = 900,
    ) -> dict[str, str]:
        headers = dict(extra or {})
        headers["Authorization"] = self._signature(
            method, key, headers, expire=expire
        )
        return headers

    def _url(self, key: str) -> str:
        return f"https://{self.host}/{quote(key, safe='/')}"

    # ------------------------------------------------------------ 操作
    def object_exists(self, key: str) -> bool:
        if not self.enabled or not key:
            return False
        response = self._http().head(
            self._url(key),
            headers=self._request_headers("HEAD", key),
            follow_redirects=False,
        )
        if response.status_code in (200, 206):
            return True
        if response.status_code == 404:
            return False
        raise CosError(f"HEAD {key} -> HTTP {response.status_code}")

    def upload_file(self, key: str, path: Path, *, content_type: str = "") -> None:
        if not self.enabled:
            return
        if not path.exists() or not path.is_file():
            raise CosError(f"本地文件不存在：{path}")
        size = path.stat().st_size
        headers = {
            "Content-Type": content_type or _content_type(path.name),
            "Content-Length": str(size),
            "Cache-Control": _IMMUTABLE_CACHE,
            # 桶保持私有；对象交给 EdgeOne Token / COS 预签名控制访问。
            # 旧对象可能是 public-read，发布 worker 会调 set_object_private 修正。
            "x-cos-acl": "private",
        }
        last_error = ""
        # 单 PUT 遇到网络抖动/5xx 重试 2 次；不把大文件重传无限放大。
        for attempt in range(3):
            try:
                auth_headers = self._request_headers(
                    "PUT", key, headers, expire=1800
                )
                with path.open("rb") as fp:
                    response = self._http().put(
                        self._url(key),
                        headers=auth_headers,
                        content=fp,
                        timeout=httpx.Timeout(
                            connect=10.0, read=180.0,
                            write=900.0, pool=10.0,
                        ),
                    )
                if response.status_code in (200, 201, 204):
                    return
                if response.status_code < 500 and response.status_code != 429:
                    raise CosError(
                        f"PUT {key} -> HTTP {response.status_code} "
                        f"{response.text[:180]}"
                    )
                last_error = (
                    f"HTTP {response.status_code} {response.text[:120]}"
                )
            except CosError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络错误可重试
                last_error = str(exc)
                if attempt >= 2:
                    raise CosError(f"PUT {key} 失败：{exc}") from exc
                time.sleep(1.5 * (attempt + 1))
        raise CosError(f"PUT {key} 重试后仍失败：{last_error}")

    def set_object_private(self, key: str) -> None:
        """把历史 public-read 对象改成 private（用 COS 同对象复制）。"""
        if not self.enabled or not key:
            return
        headers = {
            "x-cos-acl": "private",
            "x-cos-copy-source": f"{self.host}/{quote(key, safe='/')}",
            "x-cos-metadata-directive": "Replaced",
            "Content-Type": _content_type(Path(key).name),
            "Content-Length": "0",
        }
        response = self._http().put(
            self._url(key),
            headers=self._request_headers("PUT", key, headers, expire=300),
            content=b"",
            timeout=httpx.Timeout(connect=10.0, read=60.0,
                                  write=60.0, pool=10.0),
        )
        if response.status_code not in (200, 201, 204):
            raise CosError(
                f"COPY(private) {key} -> HTTP {response.status_code} "
                f"{response.text[:180]}"
            )

    def delete_object(self, key: str) -> None:
        if not self.enabled or not key:
            return
        response = self._http().delete(
            self._url(key),
            headers=self._request_headers("DELETE", key, expire=300),
        )
        # 404 也视为成功：对象已经被删了，重复删除不该让后台报错。
        if response.status_code not in (200, 204, 404):
            raise CosError(f"DELETE {key} -> HTTP {response.status_code}")

    def download_file(self, key: str, target: Path, *, max_bytes: int = 80 * 1024 * 1024) -> None:
        """仅供服务端解析 DOCX 等场景使用；普通下载走 CDN 跳转。"""
        if not self.enabled:
            raise CosError("COS 未启用")
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with self._http().stream(
            "GET",
            self._url(key),
            headers=self._request_headers("GET", key, expire=300),
        ) as response:
            if response.status_code != 200:
                raise CosError(f"GET {key} -> HTTP {response.status_code}")
            with target.open("wb") as out:
                for chunk in response.iter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise CosError("COS 对象超过 80MB 上限")
                    out.write(chunk)
        if written <= 0:
            raise CosError("COS 对象为空")


def storage_for(cfg: Config) -> CosStorage:
    return CosStorage(cfg)


def is_ready(cfg: Config) -> bool:
    return bool(cfg.cos_enabled and cfg.cos_bucket and cfg.cos_region)


__all__ = [
    "CosError",
    "CosStorage",
    "is_ready",
    "storage_for",
]
