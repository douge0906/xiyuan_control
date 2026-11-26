"""账号断言冒烟：验证 v1 兼容、v2 去学号、账号键不可枚举。

独立临时库，不碰线上数据。运行：
    cd /opt/xiyuan-control
    PYTHONPATH=/opt/xiyuan-control venv/bin/python tools/smoke_accounts.py
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import accounts, api, db as dbmod, security
from app.config import Config


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="xiyuan-acc-"))
    try:
        cfg = Config(root=root)
        dbmod.init(cfg.db_path)
        dbx = dbmod.DB(cfg.db_path)

        app = FastAPI()
        app.state.config = cfg
        app.state.db = dbx
        app.include_router(api.router)
        client = TestClient(app)

        private = ec.generate_private_key(ec.SECP256R1())
        public = b64url(private.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        token = "acc-smoke-token"
        now = security.iso()
        student_id = "20260001"
        alias = security.student_alias(student_id)
        with dbmod.connect(cfg.db_path) as conn:
            conn.execute(
                """INSERT INTO devices
                   (installation_id, public_key, platform, app_version,
                    device_serial, status, created_at, last_seen)
                   VALUES ('acc-smoke-inst', ?, 'android', '1.0.9',
                           'ACC-SMOKE-01', 'active', ?, ?)""",
                (public, now, now),
            )
            device_id = conn.execute(
                "SELECT id FROM devices WHERE device_serial='ACC-SMOKE-01'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO device_tokens (device_id, token_hash, created_at) VALUES (?, ?, ?)",
                (device_id, security.token_hash(token, cfg.token_secret), now),
            )
            # 造一个“旧库”用户：裸 SHA-256 学号 + 64 位伪名，无 account_key。
            conn.execute(
                """INSERT INTO users (student_hash, student_alias, display_name,
                                      status, created_at, last_login_at)
                   VALUES (?, ?, '旧用户', 'active', ?, ?)""",
                (hashlib.sha256(student_id.encode()).hexdigest(),
                 alias, now, now),
            )
        accounts.migrate(cfg, dbx)

        headers = {"Authorization": f"Device {token}"}

        def login(version: int) -> dict:
            challenge = client.post(
                "/api/v1/auth/challenges", headers=headers, json={}
            ).json()
            asserted_at = security.iso()
            serial = "ACC-SMOKE-01"
            if version == 1:
                payload = security.login_payload(
                    challenge["challenge_id"], challenge["challenge"], serial,
                    student_id, alias, "旧用户", asserted_at,
                )
            else:
                payload = security.login_payload_v2(
                    challenge["challenge_id"], challenge["challenge"], serial,
                    alias, "旧用户", asserted_at,
                )
            sig = b64url(private.sign(payload, ec.ECDSA(hashes.SHA256())))
            body = {
                "assertion_version": version,
                "challenge_id": challenge["challenge_id"],
                "challenge": challenge["challenge"],
                "device_serial": serial,
                "student_alias": alias,
                "display_name": "旧用户",
                "asserted_at": asserted_at,
                "signature": sig,
            }
            if version == 1:
                body["student_id"] = student_id
            r = client.post("/api/v1/auth/assertions", headers=headers, json=body)
            assert r.status_code == 200, r.text
            return r.json()

        v1 = login(1)
        v2 = login(2)

        with dbmod.connect(cfg.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM users ORDER BY id LIMIT 1"
            ).fetchone()
            all_text = " ".join(
                str(v) for v in dict(row).values() if isinstance(v, str)
            )
            assert row["account_key"] == security.account_key(
                cfg.account_key_secret, alias
            ), row
            assert row["student_alias"] == security.alias_tag(row["account_key"]), row
            assert row["student_hash"] == "v2:" + row["account_key"], row
            assert student_id not in all_text, all_text
            assert alias not in all_text, all_text
            assert len(row["student_alias"]) == 8, row
            count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            assert count == 1, count

        assert v1["user"]["alias"] == v2["user"]["alias"]

        # 全新 v2 用户：只带伪名也能建号，库里没有学号痕迹。
        student2 = "2026123456"
        alias2 = security.student_alias(student2)
        challenge = client.post(
            "/api/v1/auth/challenges", headers=headers, json={}
        ).json()
        asserted_at = security.iso()
        payload = security.login_payload_v2(
            challenge["challenge_id"], challenge["challenge"], "ACC-SMOKE-01",
            alias2, "新用户", asserted_at,
        )
        sig = b64url(private.sign(payload, ec.ECDSA(hashes.SHA256())))
        r = client.post("/api/v1/auth/assertions", headers=headers, json={
            "assertion_version": 2,
            "challenge_id": challenge["challenge_id"],
            "challenge": challenge["challenge"],
            "device_serial": "ACC-SMOKE-01",
            "student_alias": alias2,
            "display_name": "新用户",
            "asserted_at": asserted_at,
            "signature": sig,
        })
        assert r.status_code == 200, r.text
        with dbmod.connect(cfg.db_path) as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            assert len(rows) == 2, len(rows)
            text = " ".join(
                str(v) for row in rows for v in dict(row).values()
                if isinstance(v, str)
            )
            assert student2 not in text, text
            assert alias2 not in text, text

        print("account smoke OK: v1 兼容 / v2 去学号 / 账号键不可枚举")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
