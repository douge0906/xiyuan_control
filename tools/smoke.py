"""端到端冒烟：模拟客户端完成 设备登记 → 挑战 → 断言 → 刷新 → 遥测 → 管理统计。

用法（服务已在本机 8124 端口跑起来）：
    python tools/smoke.py http://127.0.0.1:8124 <ADMIN_KEY>
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

TZ = timezone(timedelta(hours=8))
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8124"
ADMIN_KEY = sys.argv[2] if len(sys.argv) > 2 else ""

DEVICE_PREFIX = "xiyuan-control-device"
LOGIN_PREFIX = "xiyuan-control-login"
ALIAS_SALT = "xiyuan-control|student|"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def iso(offset: int = 0) -> str:
    return (datetime.now(TZ) + timedelta(seconds=offset)).isoformat(timespec="seconds")


def call(method: str, path: str, body=None, headers=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> None:
    print("== 1. 生成设备密钥 ==")
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = b64(private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    install_id = "smoke-" + b64(os.urandom(9))
    print("   公钥长度", len(public_key), "安装标识", install_id)

    print("== 2. 设备登记（带签名） ==")
    created_at = iso()
    payload = "\n".join([DEVICE_PREFIX, install_id, public_key, "android", "1.0.6+20", created_at])
    signature = b64(private_key.sign(payload.encode(), ec.ECDSA(hashes.SHA256())))
    status, body = call("POST", "/api/v1/devices/register", {
        "installation_id": install_id,
        "public_key": public_key,
        "platform": "android",
        "app_version": "1.0.6+20",
        "created_at": created_at,
        "signature": signature,
    })
    print("  ", status, body)
    assert status == 200, body
    device_token = body["token"]
    serial = body["device_serial"]

    print("== 3. 签名时间窗（超时 10 分钟应被拒） ==")
    old_at = iso(-600)
    payload_old = "\n".join([DEVICE_PREFIX, install_id, public_key, "android", "1.0.6+20", old_at])
    sig_old = b64(private_key.sign(payload_old.encode(), ec.ECDSA(hashes.SHA256())))
    status, body = call("POST", "/api/v1/devices/register", {
        "installation_id": install_id + "x", "public_key": public_key, "platform": "android",
        "app_version": "1.0.6+20", "created_at": old_at, "signature": sig_old,
    })
    print("  ", status, body.get("error"))
    assert status == 400 and body["error"]["code"] == "timestamp_out_of_window"

    print("== 4. 错误签名应被拒 ==")
    status, body = call("POST", "/api/v1/devices/register", {
        "installation_id": install_id + "y", "public_key": public_key, "platform": "android",
        "app_version": "1.0.6+20", "created_at": iso(), "signature": b64(b"\x30\x06\x02\x01\x01\x02\x01\x01"),
    })
    print("  ", status, body.get("error"))
    assert status == 400 and body["error"]["code"] == "invalid_signature"

    print("== 5. 挑战 ==")
    auth = {"Authorization": f"Device {device_token}"}
    status, body = call("POST", "/api/v1/auth/challenges", {}, auth)
    print("  ", status, {k: (v[:12] + "…" if isinstance(v, str) and len(v) > 12 else v)
                         for k, v in body.items()})
    assert status == 200
    challenge_id, challenge = body["challenge_id"], body["challenge"]

    print("== 6. 断言登录 ==")
    student_id = "20260001"
    alias = hashlib.sha256(f"{ALIAS_SALT}{student_id}".encode()).hexdigest()
    asserted_at = iso()
    login_payload = "\n".join([
        LOGIN_PREFIX, challenge_id, challenge, serial, student_id, alias, "测试同学", asserted_at,
    ])
    login_sig = b64(private_key.sign(login_payload.encode(), ec.ECDSA(hashes.SHA256())))
    status, body = call("POST", "/api/v1/auth/assertions", {
        "challenge_id": challenge_id, "challenge": challenge, "device_serial": serial,
        "student_id": student_id, "student_alias": alias, "display_name": "测试同学",
        "asserted_at": asserted_at, "signature": login_sig,
    }, auth)
    print("  ", status, {k: ("<token>" if "token" in k else v) for k, v in body.items()})
    assert status == 200, body
    access, refresh = body["access_token"], body["refresh_token"]

    print("== 7. 挑战一次性 ==")
    status, body = call("POST", "/api/v1/auth/assertions", {
        "challenge_id": challenge_id, "challenge": challenge, "device_serial": serial,
        "student_id": student_id, "student_alias": alias, "display_name": "测试同学",
        "asserted_at": asserted_at, "signature": login_sig,
    }, auth)
    print("  ", status, body.get("error"))
    assert status == 400 and body["error"]["code"] == "challenge_used"

    print("== 8. 刷新令牌（轮换） ==")
    status, body = call("POST", "/api/v1/auth/refresh", {"refresh_token": refresh})
    print("  ", status, "新 access" if status == 200 else body)
    assert status == 200
    new_refresh = body["refresh_token"]
    status, body = call("POST", "/api/v1/auth/refresh", {"refresh_token": refresh})
    print("   旧刷新令牌应失效:", status, body.get("error"))
    assert status == 401 and body["error"]["code"] == "invalid_refresh_token"

    print("== 9. 内容接口（设备令牌） ==")
    for path in ("/api/v1/config/versions", "/api/v1/messages", "/api/v1/school-calendar"):
        status, body = call("GET", path, headers=auth)
        print("  ", path, status, str(body)[:80] + "…")
        assert status == 200

    print("== 10. 遥测（幂等） ==")
    event = {"event_id": "evt-1", "name": "app_start", "ts": iso()}
    status, body = call("POST", "/api/v1/telemetry/events", [event, event], auth)
    print("  ", status, body)
    assert status == 200

    print("== 11. 管理统计 ==")
    admin_headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    status, body = call("GET", "/admin/metrics/overview", headers=admin_headers)
    print("  ", status, json.dumps({
        "users": body["users"], "devices": body["devices"],
        "active": body["active"], "events": body["events"],
    }, ensure_ascii=False))
    assert status == 200 and body["devices"]["total"] >= 1

    print("== 12. 封禁设备 → 令牌失效 ==")
    status, body = call("GET", "/admin/devices", headers=admin_headers)
    device_id = body["items"][0]["id"]
    status, body = call("PATCH", f"/admin/devices/{device_id}/status",
                        {"status": "banned"}, admin_headers)
    print("  ", status, body)
    status, body = call("GET", "/api/v1/messages", headers=auth)
    print("   封禁后取内容（令牌已撤销）:", status, body.get("error"))
    assert status == 401 and body["error"]["code"] == "invalid_device_token"

    # 重新登记同一安装标识 → 应被明确告知设备已封禁
    created_at2 = iso()
    payload2 = "\n".join([DEVICE_PREFIX, install_id, public_key, "android", "1.0.6+20", created_at2])
    sig2 = b64(private_key.sign(payload2.encode(), ec.ECDSA(hashes.SHA256())))
    status, body = call("POST", "/api/v1/devices/register", {
        "installation_id": install_id, "public_key": public_key, "platform": "android",
        "app_version": "1.0.6+20", "created_at": created_at2, "signature": sig2,
    })
    print("   封禁设备重新登记:", status, body.get("error"))
    assert status == 403 and body["error"]["code"] == "device_banned"

    status, body = call("PATCH", f"/admin/devices/{device_id}/status",
                        {"status": "active"}, admin_headers)
    print("   恢复:", status, body)

    print("== 13. 内容管理 ==")
    status, body = call("GET", "/admin/content", headers=admin_headers)
    print("  ", status, [x["name"] for x in body.get("items", [])])
    assert status == 200 and len(body["items"]) == 4
    status, body = call("GET", "/admin/content/app_release", headers=admin_headers)
    print("   读取版本发布:", status, body.get("version"))
    assert status == 200 and "data" in body
    release = body["data"]
    status, body = call("PUT", "/admin/content/app_release",
                        {"data": {"latestVersion": "bad", "downloadUrl": "https://x", "notes": ""}},
                        admin_headers)
    print("   非法版本号应被拒:", status, body.get("error"))
    assert status == 400 and body["error"]["code"] == "invalid_content"
    status, body = call("PUT", "/admin/content/app_release", {"data": release}, admin_headers)
    print("   保存原内容:", status, body.get("version"))
    assert status == 200 and body.get("ok") is True
    status, body = call("GET", "/admin/content/app_release/history", headers=admin_headers)
    print("   历史备份数:", status, len(body.get("items", [])))
    assert status == 200 and body["items"]
    status, body = call("POST", "/admin/content/app_release/restore",
                        {"file": body["items"][0]["file"]}, admin_headers)
    print("   从历史恢复:", status, body.get("version"))
    assert status == 200 and body.get("ok") is True

    print("== 14. 反馈 ==")
    status, body = call("POST", "/api/v1/feedback", {
        "content": "smoke 自动测试反馈，可忽略", "contact": "", "app_version": "1.0.8+22",
    })
    print("   提交:", status, body)
    assert status == 200 and body.get("ok") is True
    feedback_id = body["id"]
    status, body = call("GET", "/admin/feedback", headers=admin_headers)
    print("   列表:", status, len(body.get("items", [])))
    assert status == 200 and any(x["id"] == feedback_id for x in body["items"])
    status, body = call("PATCH", f"/admin/feedback/{feedback_id}/status",
                        {"status": "done"}, admin_headers)
    print("   标记已处理:", status, body)
    assert status == 200 and body.get("status") == "done"

    print("\n全部通过 ✅")
    time.sleep(0.2)


if __name__ == "__main__":
    main()
