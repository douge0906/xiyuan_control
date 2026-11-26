"""网页版资源直链的单测（自带 runner，仓库里没有 pytest）。

跑法：`python tests/test_resource_link.py`
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app import resource_link as link_mod  # noqa: E402
from app.db import DB, init as db_init  # noqa: E402
from app.security import iso  # noqa: E402


class _Cfg:
    def __init__(self, root: Path) -> None:
        self.token_secret = 'unit-test-secret'
        self.upload_dir = root / 'uploads'
        self.upload_dir.mkdir(parents=True, exist_ok=True)


class _Request:
    def __init__(self, cfg: _Cfg, db: DB) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(config=cfg, db=db))


def _env():
    """建一套临时库 + 一条已审核资源 + 一个真实文件。"""
    tmp = Path(tempfile.mkdtemp(prefix='reslink-'))
    db_path = tmp / 'control.db'
    db_init(db_path)
    cfg = _Cfg(tmp)
    db = DB(db_path)
    with db as conn:
        conn.execute(
            """INSERT INTO devices
               (installation_id, public_key, platform, app_version,
                device_serial, status, created_at, last_seen)
               VALUES ('inst-1', 'pub-1', 'web', '1.0.10+40',
                       'serial-1', 'active', ?, ?)""",
            (iso(), iso()),
        )
        device_id = conn.execute("SELECT id FROM devices").fetchone()['id']
        conn.execute(
            """INSERT INTO resource_uploads
               (device_id, course_name, book_name, area, note,
                status, created_at, reviewed_at, review_note)
               VALUES (?, '高等数学', '', 'exam_paper', '', 'approved', ?, ?, '')""",
            (device_id, iso(), iso()),
        )
        upload_id = conn.execute(
            "SELECT id FROM resource_uploads").fetchone()['id']
        stored = 'paper.pdf'
        target = cfg.upload_dir / str(upload_id) / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'%PDF-1.4 test')
        conn.execute(
            """INSERT INTO resource_files
               (upload_id, name, stored_name, kind, size, sha256, downloads,
                object_key)
               VALUES (?, '期末试卷.pdf', ?, 'exam_paper', 13, 'abc123', 0, '')""",
            (upload_id, stored),
        )
        file_id = conn.execute(
            "SELECT id FROM resource_files").fetchone()['id']
    return tmp, cfg, db, device_id, file_id


def _expect(predicate, code: str) -> None:
    try:
        predicate()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        assert detail.get('code') == code, detail
        return
    raise AssertionError('应当抛 ' + code)


def test_signature_is_stable_and_field_sensitive() -> None:
    base = link_mod.signature('s', 7, 'download', 3, 1000)
    assert base == link_mod.signature('s', 7, 'download', 3, 1000)
    assert base != link_mod.signature('s', 8, 'download', 3, 1000)
    assert base != link_mod.signature('s', 7, 'preview', 3, 1000)
    assert base != link_mod.signature('s', 7, 'download', 4, 1000)
    assert base != link_mod.signature('s', 7, 'download', 3, 1001)
    assert base != link_mod.signature('t', 7, 'download', 3, 1000)
    assert len(base) == 64


def test_check_signature_accepts_valid() -> None:
    exp = int(time.time()) + 300
    sig = link_mod.signature('unit-test-secret', 7, 'download', 3, exp)
    assert link_mod._check_signature(
        SimpleNamespace(token_secret='unit-test-secret'), 7, 'download',
        '3', str(exp), sig) == 3


def test_check_signature_rejects_expired() -> None:
    exp = int(time.time()) - 5
    sig = link_mod.signature('unit-test-secret', 7, 'download', 3, exp)
    _expect(lambda: link_mod._check_signature(
        SimpleNamespace(token_secret='unit-test-secret'), 7, 'download',
        '3', str(exp), sig), 'link_expired')


def test_check_signature_rejects_tampered() -> None:
    exp = int(time.time()) + 300
    sig = link_mod.signature('unit-test-secret', 7, 'download', 3, exp)
    _expect(lambda: link_mod._check_signature(
        SimpleNamespace(token_secret='unit-test-secret'), 8, 'download',
        '3', str(exp), sig), 'link_invalid')


def test_check_signature_rejects_missing() -> None:
    _expect(lambda: link_mod._check_signature(
        SimpleNamespace(token_secret='unit-test-secret'), 7, 'download',
        None, None, None), 'missing_device_token')


def test_banned_device_rejected() -> None:
    _tmp, cfg, db, device_id, _file_id = _env()
    req = _Request(cfg, db)
    with db as conn:
        conn.execute("UPDATE devices SET status = 'banned' WHERE id = ?",
                     (device_id,))
    _expect(lambda: link_mod._device_by_id(req, device_id), 'device_banned')


def test_link_endpoint_returns_usable_path() -> None:
    _tmp, cfg, db, device_id, file_id = _env()
    req = _Request(cfg, db)

    original = link_mod.require_device
    link_mod.require_device = lambda request, authorization: {
        'id': device_id, 'status': 'active'}
    try:
        body = link_mod.resource_download_link(
            req, file_id, authorization='Device tok', mode='download')
    finally:
        link_mod.require_device = original

    assert body['ok'] is True and body['expiresIn'] == link_mod.LINK_TTL_SECONDS
    path = body['path']
    assert path.startswith(f'/api/v1/resources/files/{file_id}/raw?')
    assert 'sig=' in path and 'exp=' in path and 'dev=' in path

    # 直链里的参数必须能被 /raw 认下来（闭环）
    query = dict(
        part.split('=', 1) for part in path.split('?', 1)[1].split('&'))
    response = link_mod.resource_file_raw(
        req, file_id, mode=query['mode'], dev=query['dev'],
        exp=query['exp'], sig=query['sig'])
    assert response.filename == '期末试卷.pdf'
    assert response.headers['content-disposition'].startswith('attachment')
    assert response.headers['x-content-sha256'] == 'abc123'

    with db as conn:
        downloads = conn.execute(
            'SELECT downloads FROM resource_files WHERE id = ?',
            (file_id,)).fetchone()['downloads']
        events = conn.execute(
            'SELECT COUNT(*) AS n FROM download_events').fetchone()['n']
    assert downloads == 1 and events == 1


def test_raw_preview_is_inline_and_no_quota() -> None:
    _tmp, cfg, db, device_id, file_id = _env()
    req = _Request(cfg, db)
    exp = int(time.time()) + 300
    sig = link_mod.signature(cfg.token_secret, file_id, 'preview', device_id, exp)
    response = link_mod.resource_file_raw(
        req, file_id, mode='preview', dev=str(device_id), exp=str(exp), sig=sig)
    assert response.headers['content-disposition'].startswith('inline')
    with db as conn:
        downloads = conn.execute(
            'SELECT downloads FROM resource_files WHERE id = ?',
            (file_id,)).fetchone()['downloads']
    assert downloads == 0


def test_raw_quota_exhausted() -> None:
    _tmp, cfg, db, device_id, file_id = _env()
    req = _Request(cfg, db)
    with db as conn:
        for _ in range(link_mod.DAILY_DOWNLOAD_LIMIT):
            conn.execute(
                """INSERT INTO download_events
                   (device_id, file_id, bytes, mode, created_at)
                   VALUES (?, ?, 1, 'download', ?)""",
                (device_id, file_id, iso()),
            )
    exp = int(time.time()) + 300
    sig = link_mod.signature(cfg.token_secret, file_id, 'download', device_id, exp)
    _expect(lambda: link_mod.resource_file_raw(
        req, file_id, mode='download', dev=str(device_id), exp=str(exp),
        sig=sig), 'download_quota')


def test_link_missing_resource() -> None:
    _tmp, cfg, db, device_id, _file_id = _env()
    req = _Request(cfg, db)
    original = link_mod.require_device
    link_mod.require_device = lambda request, authorization: {
        'id': device_id, 'status': 'active'}
    try:
        _expect(lambda: link_mod.resource_download_link(
            req, 999, authorization='Device tok', mode='download'),
            'resource_missing')
    finally:
        link_mod.require_device = original


_TESTS = [
    test_signature_is_stable_and_field_sensitive,
    test_check_signature_accepts_valid,
    test_check_signature_rejects_expired,
    test_check_signature_rejects_tampered,
    test_check_signature_rejects_missing,
    test_banned_device_rejected,
    test_link_endpoint_returns_usable_path,
    test_raw_preview_is_inline_and_no_quota,
    test_raw_quota_exhausted,
    test_link_missing_resource,
]


def main() -> int:
    passed = 0
    for case in _TESTS:
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            print(f'FAIL {case.__name__}: {type(exc).__name__}: {exc}')
            continue
        passed += 1
        print(f'PASS {case.__name__}')
    print(f'\n{passed}/{len(_TESTS)} passed')
    print('ALL PASS' if passed == len(_TESTS) else 'SOME FAILED')
    return 0 if passed == len(_TESTS) else 1


if __name__ == '__main__':
    sys.exit(main())
