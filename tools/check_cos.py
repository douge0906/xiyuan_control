"""COS 真实联通性检查（会读写一个临时对象，默认检查后删除）。

用法：
    ./venv/bin/python tools/check_cos.py --confirm

检查内容：
- 用当前 data/.env 的 COS 配置上传一个 `__healthcheck__/` 下的小对象；
- HEAD 对象是否存在；
- 用 COS_PUBLIC_BASE 匿名 GET，验证桶是否真的允许公有读 / CDN 是否回源成功；
- 删除临时对象（除非 --keep）。

不打印 SecretId / SecretKey。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.object_storage import CosError, storage_for  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="确认执行真实上传/删除")
    parser.add_argument("--keep", action="store_true",
                        help="检查后保留临时对象")
    args = parser.parse_args()

    cfg = Config(root=ROOT)
    store = storage_for(cfg)
    if not store.enabled:
        print("COS 未启用：请在 data/.env 配 COS_ENABLED=1、"
              "COS_SECRET_ID、COS_SECRET_KEY")
        return 2
    if not args.confirm:
        print("这是真实写入检查。确认请加 --confirm")
        return 0

    key = f"__healthcheck__/{uuid.uuid4().hex}.txt"
    body = b"xiyuan cos healthcheck\n"
    url = store.public_url(key)
    print("bucket =", store.bucket)
    print("region =", store.region)
    print("public =", store.public_base)
    print("key    =", key)

    fd, tmp_name = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(body)
        try:
            store.upload_file(key, tmp, content_type="text/plain")
            print("upload  OK")
        except CosError as exc:
            print("upload  FAIL:", exc)
            return 1

        try:
            print("head    ", "OK" if store.object_exists(key) else "MISS")
        except CosError as exc:
            print("head    FAIL:", exc)

        try:
            with httpx.stream("GET", url, timeout=20,
                              follow_redirects=True) as response:
                print("public  ", response.status_code,
                      "len=", response.headers.get("content-length"),
                      "cache=", response.headers.get("cache-control"),
                      "server=", response.headers.get("server"))
                data = b"".join(response.iter_bytes())
                print("payload ", "OK" if data == body else "MISMATCH")
        except Exception as exc:  # noqa: BLE001
            print("public  FAIL:", exc)
    finally:
        if not args.keep:
            try:
                store.delete_object(key)
                print("delete  OK")
            except Exception as exc:  # noqa: BLE001
                print("delete  FAIL:", exc)
        store.close()
        try:
            tmp.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
