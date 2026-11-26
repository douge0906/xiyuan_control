"""密码学与时间工具。

- 令牌：随机 32 字节，数据库只存 HMAC-SHA256，泄漏库也拿不到原值
- 设备签名：客户端 P-256（secp256r1）ECDSA/SHA-256，服务端用公钥验签
- 规范化签名文本：字段逐行拼接，禁止换行，避免拼接歧义
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

TZ = timezone(timedelta(hours=8))
DEVICE_PREFIX = "xiyuan-control-device"
LOGIN_PREFIX = "xiyuan-control-login"
LOGIN_PREFIX_V2 = "xiyuan-control-login-v2"
ALIAS_SALT = "xiyuan-control|student|"


# ---------------------------------------------------------------- 时间

def now() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def today() -> str:
    return now().strftime("%Y-%m-%d")


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed


# ---------------------------------------------------------------- 令牌

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def new_token() -> str:
    return b64url_encode(secrets.token_bytes(32))


def token_hash(token: str, secret: str) -> str:
    """数据库里只存这个，不存令牌原文。"""
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- 伪名

def student_hash(student_id: str) -> str:
    """旧客户端兼容用：仅用于在库中兜底查找未迁移的历史账号。

    2026-11-20 起新账号不再写入这个可枚举的裸哈希；存量账号启动时会
    被改写成 ``v2:<account_key>``，旧客户端仍可凭 alias 找到账号。
    """
    return hashlib.sha256(student_id.encode()).hexdigest()


def student_alias(student_id: str) -> str:
    return hashlib.sha256(f"{ALIAS_SALT}{student_id}".encode()).hexdigest()


def account_key(secret: str, alias: str) -> str:
    """把客户端伪名变成服务端账号键：HMAC(secret, alias)。

    伪名本身是固定前缀 SHA-256，学号空间小、可离线枚举；数据库只存这个
    HMAC 结果，拿库没有 secret 就无法反推伪名或学号。
    """
    return hmac.new(
        secret.encode(), f"xiyuan-account|{alias}".encode(), hashlib.sha256
    ).hexdigest()


def alias_tag(key: str) -> str:
    """给后台展示用的短标签，不可反推。"""
    return key[:8]


def is_alias(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# ---------------------------------------------------------------- 签名

def canonical(lines: list[str]) -> bytes:
    for line in lines:
        if "\n" in line or "\r" in line or "\x00" in line:
            raise ValueError("signed fields cannot contain line breaks")
    return "\n".join(lines).encode()


def device_payload(installation_id: str, public_key: str, platform: str,
                   app_version: str, created_at: str) -> bytes:
    return canonical([
        DEVICE_PREFIX, installation_id, public_key, platform, app_version, created_at,
    ])


def login_payload(challenge_id: str, challenge: str, device_serial: str,
                  student_id: str, alias: str, display_name: str,
                  asserted_at: str) -> bytes:
    return canonical([
        LOGIN_PREFIX, challenge_id, challenge, device_serial,
        student_id, alias, display_name, asserted_at,
    ])


def login_payload_v2(challenge_id: str, challenge: str, device_serial: str,
                     alias: str, display_name: str,
                     asserted_at: str) -> bytes:
    """客户端本地只算伪名，原始学号不再进入请求或签名。"""
    return canonical([
        LOGIN_PREFIX_V2, challenge_id, challenge, device_serial,
        alias, display_name, asserted_at,
    ])


def verify_signature(public_key_b64: str, payload: bytes, signature_b64: str) -> bool:
    """P-256 ECDSA/SHA-256 验签；公钥是 SPKI DER 的 base64url。"""
    try:
        key = serialization.load_der_public_key(b64url_decode(public_key_b64))
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        # 客户端输出的是 DER 编码的 (r,s)，与 cryptography 期望的一致
        key.verify(b64url_decode(signature_b64), payload, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, Exception):
        return False


def der_signature(r: int, s: int) -> bytes:
    """测试用：把 r/s 编成 DER。生产侧由客户端完成。"""
    return encode_dss_signature(r, s)
