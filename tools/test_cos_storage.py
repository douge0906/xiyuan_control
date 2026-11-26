"""COS 适配器的离线单测：不访问腾讯云，只校验请求形态与回退语义。

运行：
    ./venv/bin/python tools/test_cos_storage.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.object_storage import CosStorage  # noqa: E402


class CosStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        cfg = Config(root=ROOT)
        cfg.cos_enabled = True
        cfg.cos_secret_id = "AKIDTESTID"
        cfg.cos_secret_key = "TESTSECRET"
        cfg.cos_bucket = "test-1250000000"
        cfg.cos_region = "ap-nanjing"
        cfg.cos_public_base = "https://res.example.test"
        cfg.cos_prefix = "resources"
        self.cfg = cfg
        self.store = CosStorage(cfg)

    def tearDown(self) -> None:
        self.store.close()

    def test_public_url_is_stable_and_encoded(self) -> None:
        key = "resources/12/课程 1.pdf"
        self.assertEqual(
            self.store.public_url(key),
            "https://res.example.test/resources/12/%E8%AF%BE%E7%A8%8B%201.pdf",
        )

    def test_upload_sets_cache_and_acl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            path.write_bytes(b"%PDF-1.4 test")
            seen: dict[str, object] = {}

            def handler(request: httpx.Request) -> httpx.Response:
                seen["method"] = request.method
                seen["path"] = request.url.path
                seen["auth"] = request.headers.get("authorization")
                seen["cache"] = request.headers.get("cache-control")
                seen["acl"] = request.headers.get("x-cos-acl")
                seen["body"] = request.read()
                return httpx.Response(200)

            self.store._client = httpx.Client(
                transport=httpx.MockTransport(handler)
            )
            self.store.upload_file(
                "resources/12/paper.pdf", path, content_type="application/pdf"
            )

        self.assertEqual(seen["method"], "PUT")
        self.assertEqual(seen["path"], "/resources/12/paper.pdf")
        self.assertTrue(str(seen["auth"]).startswith("q-sign-algorithm=sha1"))
        self.assertEqual(
            seen["cache"], "public, max-age=31536000, immutable"
        )
        self.assertEqual(seen["acl"], "private")
        self.assertEqual(seen["body"], b"%PDF-1.4 test")

    def test_disabled_store_is_noop(self) -> None:
        self.cfg.cos_enabled = False
        store = CosStorage(self.cfg)
        self.assertFalse(store.enabled)
        store.upload_file("resources/1/x.pdf", Path("missing.pdf"))
        store.delete_object("resources/1/x.pdf")
        store.close()


if __name__ == "__main__":
    unittest.main()
