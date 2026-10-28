"""资源上传/审核/下载冒烟：独立临时数据库，不碰线上数据。"""

from __future__ import annotations

import shutil
import sys
import tempfile
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

        # 通过后公开列表只有教材
        res = client.get("/api/v1/resources").json()
        files = [f for c in res["courses"] for f in c["files"]]
        assert len(files) == 1, res
        assert files[0]["name"] == "gm.pdf"
        assert files[0]["area"] == "textbook"

        # 下载通过的文件
        dl = client.get(files[0]["downloadUrl"])
        assert dl.status_code == 200, dl.text
        assert dl.content == b"%PDF-1.4 smoke"

        # 管理端默认只列待审（此时为空）
        pend = client.get("/admin/resources?status=pending", headers=admin).json()["items"]
        assert pend == [], pend

        print("resource smoke OK: upload/admin/approve/reject/download")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
