"""资源上传/审核/下载冒烟：独立临时数据库，不碰线上数据。"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import security  # noqa: E402
from app.config import Config  # noqa: E402
from app.main import app  # noqa: E402


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="xiyuan-res-"))
    try:
        cfg = Config(root=root)
        dbmod.init(cfg.db_path)
        app.state.config = cfg
        app.state.db = dbmod.DB(cfg.db_path)

        now = security.iso()
        with dbmod.connect(cfg.db_path) as conn:
            conn.execute(
                """INSERT INTO devices
                   (installation_id, public_key, platform, app_version,
                    device_serial, status, created_at, last_seen)
                   VALUES (?, ?, 'android', '1.0.8', 'TEST-RES-0001', 'active', ?, ?)""",
                ("11111111-1111-1111-1111-111111111111", "test-key", now, now),
            )
            conn.execute(
                """INSERT INTO device_tokens (device_id, token_hash, created_at)
                   VALUES (1, ?, ?)""",
                (security.token_hash("test-token", cfg.token_secret), now),
            )

        client = TestClient(app)
        device = {"Authorization": "Device test-token"}
        admin = {"Authorization": f"Bearer {cfg.admin_key}"}

        # 教材 PDF 上传 → 待审
        r = client.post(
            "/api/v1/resource-uploads",
            headers=device,
            data={
                "course_name": "高等数学",
                "book_name": "高等数学 上册",
                "area": "textbook",
                "note": "审核冒烟",
            },
            files=[("files", ("gm.pdf", b"%PDF-1.4 smoke", "application/pdf"))],
        )
        assert r.status_code == 200, r.text
        upload_id = r.json()["id"]
        assert r.json()["status"] == "pending"

        # 试卷 + 答案 上传 → 待审
        r2 = client.post(
            "/api/v1/resource-uploads",
            headers=device,
            data={
                "course_name": "高等数学",
                "book_name": "高等数学 上册",
                "area": "exam_paper",
                "note": "期末",
            },
            files=[("files", ("exam.pdf", b"%PDF-1.4 exam", "application/pdf"))],
        )
        assert r2.status_code == 200, r2.text
        upload_id2 = r2.json()["id"]

        # 非法格式被拒绝
        bad = client.post(
            "/api/v1/resource-uploads",
            headers=device,
            data={
                "course_name": "高等数学",
                "book_name": "高等数学 上册",
                "area": "textbook",
            },
            files=[("files", ("bad.txt", b"x", "text/plain"))],
        )
        assert bad.status_code == 400, bad.text

        # 审核：教材通过，试卷拒绝
        r = client.patch(
            f"/admin/resources/{upload_id}/status",
            headers=admin,
            json={"status": "approved", "reviewNote": "ok"},
        )
        assert r.status_code == 200, r.text
        r = client.patch(
            f"/admin/resources/{upload_id2}/status",
            headers=admin,
            json={"status": "rejected", "reviewNote": "no"},
        )
        assert r.status_code == 200, r.text

        # 通过后公开列表只有教材；列表现在需要设备令牌
        res = client.get("/api/v1/resources", headers=device).json()
        files = [f for c in res["courses"] for f in c["files"]]
        assert len(files) == 1, res
        assert files[0]["name"] == "gm.pdf"
        assert files[0]["area"] == "textbook"
        assert files[0]["uploadId"] == upload_id
        assert client.get("/api/v1/resources").status_code == 401

        # 下载通过的文件（按设备计数）
        dl = client.get(files[0]["downloadUrl"], headers=device)
        assert dl.status_code == 200, dl.text
        assert dl.content == b"%PDF-1.4 smoke"

        # 预览不占下载额度，但计入流量
        pv = client.get(files[0]["downloadUrl"] + "?mode=preview", headers=device)
        assert pv.status_code == 200, pv.text

        quota = client.get("/api/v1/resources/quota", headers=device)
        assert quota.status_code == 200, quota.text
        assert quota.json()["used"] == 1
        assert quota.json()["remaining"] == 4
        assert quota.json()["baseLimit"] == 5
        assert quota.json()["bonusLimit"] == 0
        assert quota.json()["invitedCount"] == 0

        # 每日基础额度 5 次：已下载 1 次，再下 4 次后第 6 次应 429
        for _ in range(4):
            assert client.get(files[0]["downloadUrl"], headers=device).status_code == 200
        quota = client.get(files[0]["downloadUrl"], headers=device)
        assert quota.status_code == 429, quota.text
        assert quota.json()["error"]["code"] == "download_quota"

        # 邀请联动：给该设备绑一个用户，并挂 2 个已激活邀请；
        # 每个 +10 次，额度应从 5 变成 25，之前被 429 的下载恢复。
        with dbmod.connect(cfg.db_path) as conn:
            conn.execute(
                """INSERT INTO users
                   (student_hash, student_alias, display_name, status, created_at)
                   VALUES ('smoke-user', '烟测同学', '', 'active', ?)""",
                (now,),
            )
            conn.execute(
                """INSERT INTO sessions
                   (user_id, device_id, access_hash, access_exp,
                    refresh_hash, refresh_exp, created_at, last_seen)
                   VALUES (1, 1, 'smoke-access', '2099-01-01', 
                           'smoke-refresh', '2099-01-01', ?, ?)""",
                (now, now),
            )
            for invitee in (2, 3):
                conn.execute(
                    """INSERT INTO invite_relations
                       (code, inviter_user_id, invitee_user_id,
                        invitee_device_id, source, status, bound_at,
                        activated_at, reward_state, created_at)
                       VALUES ('SMOKE01', 1, ?, 1, 'share', 'activated',
                               ?, ?, 'none', ?)""",
                    (invitee, now, now, now),
                )
        quota = client.get("/api/v1/resources/quota", headers=device).json()
        assert quota["baseLimit"] == 5, quota
        assert quota["bonusLimit"] == 20, quota
        assert quota["invitedCount"] == 2, quota
        assert quota["limit"] == 25, quota
        assert client.get(files[0]["downloadUrl"], headers=device).status_code == 200

        traffic = client.get("/admin/resources/traffic", headers=admin)
        assert traffic.status_code == 200, traffic.text
        # 基础 5 次下载 + 1 次 preview + 邀请加成后 1 次下载 = 7 条事件；
        # 被 429 拦截的那次不写 download_events。
        assert traffic.json()["totals"]["requests"] >= 7
        assert traffic.json()["devices"]

        # 账号口径：同一账号换设备后，已用下载次数必须继续累计。
        with dbmod.connect(cfg.db_path) as conn:
            conn.execute(
                """INSERT INTO users
                   (student_hash, student_alias, display_name, status, created_at)
                   VALUES ('smoke-user-2', '烟测同学2', '', 'active', ?)""",
                (now,),
            )
            for device_id, serial in ((2, "TEST-RES-0002"), (3, "TEST-RES-0003")):
                conn.execute(
                    """INSERT INTO devices
                       (installation_id, public_key, platform, app_version,
                        device_serial, status, created_at, last_seen)
                       VALUES (?, ?, 'android', '1.0.9', ?, 'active', ?, ?)""",
                    (f"22222222-2222-2222-2222-22222222222{device_id}",
                     f"test-key-{device_id}", serial, now, now),
                )
                conn.execute(
                    """INSERT INTO device_tokens (device_id, token_hash, created_at)
                       VALUES (?, ?, ?)""",
                    (device_id, security.token_hash(
                        f"test-token-{device_id}", cfg.token_secret
                    ), now),
                )
                conn.execute(
                    """INSERT INTO sessions
                       (user_id, device_id, access_hash, access_exp,
                        refresh_hash, refresh_exp, created_at, last_seen)
                       VALUES (2, ?, ?, '2099-01-01', ?, '2099-01-01', ?, ?)""",
                    (device_id, f"smoke-access-{device_id}",
                     f"smoke-refresh-{device_id}", now, now),
                )
        device2 = {"Authorization": "Device test-token-2"}
        device3 = {"Authorization": "Device test-token-3"}
        assert client.get(files[0]["downloadUrl"], headers=device2).status_code == 200
        quota2 = client.get("/api/v1/resources/quota", headers=device2).json()
        assert quota2["scope"] == "account", quota2
        assert quota2["used"] == 1, quota2
        quota3 = client.get("/api/v1/resources/quota", headers=device3).json()
        assert quota3["scope"] == "account", quota3
        assert quota3["used"] == 1, quota3
        for _ in range(4):
            assert client.get(files[0]["downloadUrl"], headers=device3).status_code == 200
        shared_limit = client.get(files[0]["downloadUrl"], headers=device2)
        assert shared_limit.status_code == 429, shared_limit.text

        # 管理端默认只列待审（此时为空）
        pend = client.get("/admin/resources?status=pending", headers=admin).json()["items"]
        assert pend == [], pend

        # 试卷 + 答案一次提交
        r3 = client.post(
            "/api/v1/resource-uploads",
            headers=device,
            data={
                "course_name": "高等数学",
                "book_name": "高等数学 上册",
                "area": "exam",
                "note": "试卷与答案同批",
            },
            files=[
                ("files", ("exam2.pdf", b"%PDF-1.4 exam2", "application/pdf")),
                ("answer_files", ("answer2.pdf", b"%PDF-1.4 answer2", "application/pdf")),
            ],
        )
        assert r3.status_code == 200, r3.text
        upload_id3 = r3.json()["id"]
        assert client.patch(
            f"/admin/resources/{upload_id3}/status",
            headers=admin,
            json={"status": "approved", "reviewNote": "ok"},
        ).status_code == 200

        # 审核通过后同一课程下应同时有试卷和答案
        res = client.get("/api/v1/resources", headers=device).json()
        areas = {
            f["area"]
            for c in res["courses"]
            for f in c["files"]
        }
        assert {"textbook", "exam_paper", "answer"} <= areas, areas
        files = [f for c in res["courses"] for f in c["files"]]
        paper = next(f for f in files if f["area"] == "exam_paper")
        answer = next(f for f in files if f["area"] == "answer")
        assert answer["parentId"] == paper["id"], (paper, answer)

        # 我的上传：统计与记录
        mine = client.get("/api/v1/resource-uploads/mine", headers=device)
        assert mine.status_code == 200, mine.text
        assert mine.json()["stats"]["contributions"] >= 1
        assert mine.json()["items"]

        # DOCX 文本预览：最小 WordprocessingML，验证不依赖 LibreOffice
        docx_buf = io.BytesIO()
        with zipfile.ZipFile(docx_buf, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<?xml version="1.0"?>
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>答案正文</w:t></w:r></w:p></w:body>
                </w:document>""",
            )
        r4 = client.post(
            "/api/v1/resource-uploads",
            headers=device,
            data={
                "course_name": "高等数学",
                "book_name": "高等数学 上册",
                "area": "exam",
                "note": "DOCX 预览",
            },
            files=[
                ("files", ("exam3.pdf", b"%PDF-1.4 exam3", "application/pdf")),
                ("answer_files", (
                    "answer3.docx",
                    docx_buf.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )),
            ],
        )
        assert r4.status_code == 200, r4.text
        upload_id4 = r4.json()["id"]
        assert client.patch(
            f"/admin/resources/{upload_id4}/status",
            headers=admin,
            json={"status": "approved", "reviewNote": "ok"},
        ).status_code == 200
        res = client.get("/api/v1/resources", headers=device).json()
        docx_file = next(
            f for c in res["courses"] for f in c["files"] if f["name"] == "answer3.docx"
        )
        text_preview = client.get(
            f"/api/v1/resources/files/{docx_file['id']}/text", headers=device
        )
        assert text_preview.status_code == 200, text_preview.text
        assert "答案正文" in text_preview.json()["text"], text_preview.text

        # COS 已配置且对象已推完时，预览/下载接口 307 到 CDN；本测试只校验
        # 跳转头，不会访问腾讯云（COS 客户端仅用于拼公网地址）。
        cfg.cos_enabled = True
        cfg.cos_secret_id = "smoke-id"
        cfg.cos_secret_key = "smoke-key"
        cfg.cos_public_base = "https://res.example.test"
        with dbmod.connect(cfg.db_path) as conn:
            conn.execute(
                "UPDATE resource_files SET object_key = ? WHERE id = ?",
                (f"resources/{docx_file['uploadId']}/{docx_file['id']}.docx",
                 docx_file["id"]),
            )
        redirected = client.get(
            f"/api/v1/resources/files/{docx_file['id']}?mode=preview",
            headers=device,
            follow_redirects=False,
        )
        assert redirected.status_code == 307, redirected.text
        # 私有桶兜底必须下发预签名 URL，而不是公开直链。
        location = redirected.headers["location"]
        assert location.startswith("https://")
        assert "/resources/" in location
        assert "q-sign-algorithm=" in location, location
        cfg.cos_enabled = False

        # 用户反馈 → 管理员下架
        report = client.post(
            "/api/v1/resources/reports",
            headers=device,
            json={
                "uploadId": paper["uploadId"],
                "fileId": paper["id"],
                "reason": "broken",
                "detail": "打不开",
            },
        )
        assert report.status_code == 200, report.text
        report_id = report.json()["id"]
        reports = client.get("/admin/resource-reports?status=pending", headers=admin)
        assert reports.status_code == 200, reports.text
        assert any(item["id"] == report_id for item in reports.json()["items"])
        resolved = client.post(
            f"/admin/resource-reports/{report_id}/resolve",
            headers=admin,
            json={"action": "remove", "note": "反馈冒烟"},
        )
        assert resolved.status_code == 200, resolved.text
        res = client.get("/api/v1/resources", headers=device).json()
        remaining = [f for c in res["courses"] for f in c["files"]]
        assert all(f["uploadId"] != paper["uploadId"] for f in remaining), remaining
        removed = client.get("/admin/resources?status=removed", headers=admin)
        assert removed.status_code == 200, removed.text
        assert any(item["id"] == paper["uploadId"] for item in removed.json()["items"])

        print("resource smoke OK: upload/admin/approve/reject/download/report")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
