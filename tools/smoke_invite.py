"""邀请拉新闭环自测：TestClient + 临时 SQLite，不碰线上 data/control.db。

覆盖：
  设备登记 → 断言登录（A 邀请人 / B 被邀请人）
  GET  /invite/me        幂等发码、统计与脱敏名单
  POST /invite/bind      非法码 / 自邀 / 重复绑定 / 正常绑定（填码即激活）
  POST /invite/activate  旧客户端幂等激活
  POST /app/rating       五星评分入库与非法分值
  GET  /invite/config    开关下发

用法：
    .venv\\Scripts\\python.exe tools/smoke_invite.py
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
import app.main as main_app  # noqa: E402

DEVICE_PREFIX = "xiyuan-control-device"
LOGIN_PREFIX = "xiyuan-control-login"
ALIAS_SALT = "xiyuan-control|student|"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = b64(priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return priv, pub


def _iso() -> str:
    from app import security
    return security.iso()


def register(client: TestClient, priv, pub: str, tag: str) -> tuple[str, str]:
    install_id = f"invite-{tag}-" + b64(os.urandom(9))
    created_at = _iso()
    payload = "\n".join([DEVICE_PREFIX, install_id, pub, "android", "1.0.9+23", created_at])
    sig = b64(priv.sign(payload.encode(), ec.ECDSA(hashes.SHA256())))
    r = client.post("/api/v1/devices/register", json={
        "installation_id": install_id,
        "public_key": pub,
        "platform": "android",
        "app_version": "1.0.9+23",
        "created_at": created_at,
        "signature": sig,
    })
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["device_serial"]


def login(client: TestClient, device_token: str, serial: str,
          priv, student_id: str) -> str:
    auth = {"Authorization": f"Device {device_token}"}
    r = client.post("/api/v1/auth/challenges", json={}, headers=auth)
    assert r.status_code == 200, r.text
    challenge_id, challenge = r.json()["challenge_id"], r.json()["challenge"]

    alias = hashlib.sha256(f"{ALIAS_SALT}{student_id}".encode()).hexdigest()
    asserted_at = _iso()
    payload = "\n".join([
        LOGIN_PREFIX, challenge_id, challenge, serial, student_id,
        alias, "自测", asserted_at,
    ])
    sig = b64(priv.sign(payload.encode(), ec.ECDSA(hashes.SHA256())))
    r = client.post("/api/v1/auth/assertions", json={
        "challenge_id": challenge_id,
        "challenge": challenge,
        "device_serial": serial,
        "student_id": student_id,
        "student_alias": alias,
        "display_name": "自测",
        "asserted_at": asserted_at,
        "signature": sig,
    }, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def main() -> None:
    # 临时库，避免污染开发库业务数据。
    tmp = Path(tempfile.gettempdir()) / "invite_smoke.db"
    if tmp.exists():
        tmp.unlink()
    db.init(tmp)
    main_app.app.state.db = db.DB(tmp)

    client = TestClient(main_app.app)

    # A 邀请人、B 被邀请人，各自独立设备与会话。
    priv_a, pub_a = _key()
    tok_a, serial_a = register(client, priv_a, pub_a, "a")
    access_a = login(client, tok_a, serial_a, priv_a, "20260001")

    priv_b, pub_b = _key()
    tok_b, serial_b = register(client, priv_b, pub_b, "b")
    access_b = login(client, tok_b, serial_b, priv_b, "20260002")

    ha = {"Authorization": f"Bearer {access_a}"}
    hb = {"Authorization": f"Bearer {access_b}"}

    print("== config ==")
    r = client.get("/api/v1/invite/config")
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["egg_enabled"] is True and cfg["active"] is True
    print("  ", cfg)

    print("== OCR 未带设备令牌 → 401 ==")
    r = client.post("/api/v1/campus/ocr/course-table", json={})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "missing_device_token", r.text
    print("  ", r.status_code, r.json()["error"]["code"])

    print("== A 取邀请码（幂等） ==")
    r = client.get("/api/v1/invite/me", headers=ha)
    assert r.status_code == 200, r.text
    me_a = r.json()
    code_a = me_a["code"]
    assert code_a and len(code_a) == 8, me_a
    print("  code =", code_a, "total =", me_a["total_invited"],
          "activated =", me_a["activated_count"])

    r2 = client.get("/api/v1/invite/me", headers=ha)
    assert r2.json()["code"] == code_a, "邀请码必须按用户幂等"

    print("== B 绑定非法码 → invalid_code ==")
    r = client.post("/api/v1/invite/bind",
                     json={"code": "ZZZZZZZZ", "source": "manual_code"}, headers=hb)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "invalid_code", body
    print("  ", body["reason"])

    print("== A 绑定自己的码 → self_invite ==")
    r = client.post("/api/v1/invite/bind",
                    json={"code": code_a, "source": "manual_code"}, headers=ha)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "self_invite", body
    print("  ", body["reason"])

    print("== B 绑定 A 的码 → 直接 activated ==")
    r = client.post("/api/v1/invite/bind",
                    json={"code": code_a, "source": "manual_code",
                          "app_version": "1.0.9+23", "platform": "android"},
                    headers=hb)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "activated", body
    assert body["message"], body
    print("  ", body["status"], body["message"])

    print("== B 重复绑定 → duplicate ==")
    r = client.post("/api/v1/invite/bind",
                    json={"code": code_a, "source": "manual_code"}, headers=hb)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "duplicate", body
    print("  ", body["reason"])

    print("== B 旧客户端再上报激活 → 幂等 already ==")
    r = client.post("/api/v1/invite/activate",
                    json={"action": "course_import"}, headers=hb)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activated"] is True and body.get("already") is True, body
    print("  ", body)

    print("== B 提交五星评分 ==")
    r = client.post("/api/v1/app/rating",
                    json={"rating": 5, "app_version": "1.0.9+29",
                          "platform": "android"}, headers=hb)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, r.text
    with main_app.app.state.db as conn:
        row = conn.execute(
            "SELECT rating, app_version FROM app_ratings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row["rating"] == 5, dict(row) if row else None
    print("  rating =", row["rating"], "version =", row["app_version"])

    print("== 同一用户同版本重复评分 → 覆盖，不新增行 ==")
    r = client.post("/api/v1/app/rating",
                    json={"rating": 3, "app_version": "1.0.9+29",
                          "platform": "android"}, headers=hb)
    assert r.status_code == 200, r.text
    from app import stats as stats_mod
    with main_app.app.state.db as conn:
        rows = conn.execute(
            "SELECT COUNT(*) c, MAX(rating) r FROM app_ratings"
        ).fetchone()
        agg = stats_mod.app_ratings(conn)
    assert rows["c"] == 1 and rows["r"] == 3, dict(rows)
    assert agg["overall"]["ratings"] == 1, agg
    assert agg["versions"][0]["average"] == 3.0, agg
    print("  rows = 1, latest = 3, aggregate average =",
          agg["versions"][0]["average"])

    print("== 非法评分（6 星）→ 400 ==")
    r = client.post("/api/v1/app/rating",
                    json={"rating": 6}, headers=hb)
    assert r.status_code == 400, r.text
    print("  ", r.status_code, r.json()["error"]["code"])

    print("== 评分未带会话 → 401 ==")
    r = client.post("/api/v1/app/rating", json={"rating": 5})
    assert r.status_code == 401, r.text
    print("  ", r.status_code)

    print("== A 名单统计 ==")
    r = client.get("/api/v1/invite/me", headers=ha)
    assert r.status_code == 200, r.text
    me_a = r.json()
    assert me_a["total_invited"] == 1, me_a
    assert me_a["activated_count"] == 1, me_a
    assert len(me_a["relations"]) == 1
    rel = me_a["relations"][0]
    assert rel["status"] == "activated" and rel["activated_at"], rel
    assert rel["masked_label"].startswith("同学"), rel
    print("  total =", me_a["total_invited"], "activated =",
          me_a["activated_count"], "relation =", rel)

    print("== 未带会话 → 401 ==")
    r = client.get("/api/v1/invite/me")
    assert r.status_code == 401, r.text
    print("  ", r.status_code, r.json()["error"]["code"])

    print("== 分享落地页 /invite?c= → 200 且回显邀请码 ==")
    r = client.get(f"/invite?c={code_a}")
    assert r.status_code == 200, r.text
    assert code_a in r.text, "落地页必须回显邀请码"
    assert "邀请同学" in r.text
    print("  ", r.status_code, len(r.text), "bytes")

    print("== 新同学重新断言登录（两条会话）后仍可绑定（直接生效） ==")
    # 回归：old_user 曾按 sessions 行数判定，退出重登 / 换设备的新同学会被误杀。
    priv_c, pub_c = _key()
    tok_c, serial_c = register(client, priv_c, pub_c, "c")
    access_c1 = login(client, tok_c, serial_c, priv_c, "20260003")
    access_c2 = login(client, tok_c, serial_c, priv_c, "20260003")
    assert access_c1 != access_c2
    hc = {"Authorization": f"Bearer {access_c2}"}
    r = client.post("/api/v1/invite/bind",
                    json={"code": code_a, "source": "manual_code"}, headers=hc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "activated", body
    print("  sessions=2 →", body["status"])

    print("== 账号登记超过 7 天 → old_user ==")
    from app import security
    from datetime import timedelta
    priv_d, pub_d = _key()
    tok_d, serial_d = register(client, priv_d, pub_d, "d")
    access_d = login(client, tok_d, serial_d, priv_d, "20260004")
    with main_app.app.state.db as conn:
        conn.execute(
            "UPDATE users SET created_at = ? WHERE student_hash = ?",
            (security.iso(security.now() - timedelta(days=8)),
             security.student_hash("20260004")),
        )
    hd = {"Authorization": f"Bearer {access_d}"}
    r = client.post("/api/v1/invite/bind",
                    json={"code": code_a, "source": "manual_code"}, headers=hd)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "old_user", body
    print("  ", body["reason"])

    print("== A 名单统计（含新绑定 C） ==")
    r = client.get("/api/v1/invite/me", headers=ha)
    assert r.status_code == 200, r.text
    me_a = r.json()
    assert me_a["total_invited"] == 2, me_a
    assert me_a["activated_count"] == 2, me_a
    print("  total =", me_a["total_invited"], "activated =",
          me_a["activated_count"])

    tmp.unlink(missing_ok=True)
    print("\nALL INVITE SMOKE PASSED")


if __name__ == "__main__":
    main()
