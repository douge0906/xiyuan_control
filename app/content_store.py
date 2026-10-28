"""内容文件的安全读写层。

管理后台不再直接 SSH 改 JSON，而是走这里：
- 白名单名称，拒绝任意路径；
- 按内容类型做结构校验；
- 先写临时文件再 os.replace，避免半截文件；
- 每次保存前把旧内容备份到 content/.history/，保留最近 N 份；
- 版本号仍由 content.py 按文件 sha1 计算。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import content

NAMES: tuple[str, ...] = ("app_release", "messages", "school_calendar", "exam_papers")
MAX_BYTES = 512 * 1024
HISTORY_KEEP = 20
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([+\-][A-Za-z0-9.\-]+)?$")


class ContentError(ValueError):
    """内容不符合约定；由 API 转成 400。"""


def _path(content_dir: Path, name: str) -> Path:
    if name not in NAMES:
        raise ContentError("不支持的内容类型")
    return content_dir / f"{name}.json"


def read(content_dir: Path, name: str) -> Any:
    data = content.load(content_dir, name)
    if data is None:
        raise ContentError("内容尚未配置")
    return data


def validate(name: str, data: Any) -> None:
    if name == "app_release":
        _validate_release(data)
    elif name == "messages":
        _validate_messages(data)
    elif name == "school_calendar":
        _validate_calendar(data)
    elif name == "exam_papers":
        _validate_papers(data)
    else:
        raise ContentError("不支持的内容类型")


def _require_dict(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ContentError("顶层必须是 JSON 对象")
    return data


def _require_list(data: Any) -> list:
    if not isinstance(data, list):
        raise ContentError("顶层必须是 JSON 数组")
    return data


def _validate_release(data: Any) -> None:
    obj = _require_dict(data)
    version = str(obj.get("latestVersion") or "").strip()
    if not _VERSION_RE.match(version):
        raise ContentError("latestVersion 必须是 x.y.z 形式")
    url = str(obj.get("downloadUrl") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ContentError("downloadUrl 必须是 http(s) 地址")
    notes = obj.get("notes", "")
    if notes is not None and not isinstance(notes, str):
        raise ContentError("notes 必须是字符串")
    if len(str(notes or "")) > 2000:
        raise ContentError("notes 太长（最多 2000 字）")


def _validate_messages(data: Any) -> None:
    items = _require_list(data)
    if len(items) > 200:
        raise ContentError("公告最多 200 条")
    seen: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ContentError(f"第 {index} 条公告必须是对象")
        item_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        if not item_id or len(item_id) > 64:
            raise ContentError(f"第 {index} 条公告 id 无效")
        if item_id in seen:
            raise ContentError(f"公告 id 重复：{item_id}")
        seen.add(item_id)
        if not title or len(title) > 120:
            raise ContentError(f"第 {index} 条公告标题为空或过长")
        if not body or len(body) > 5000:
            raise ContentError(f"第 {index} 条公告正文为空或过长")
        date = item.get("date", "")
        if date is not None and not isinstance(date, str):
            raise ContentError(f"第 {index} 条公告 date 必须是字符串")
        if "pinned" in item and not isinstance(item["pinned"], bool):
            raise ContentError(f"第 {index} 条公告 pinned 必须是布尔值")


def _validate_calendar(data: Any) -> None:
    obj = _require_dict(data)
    if not str(obj.get("semester") or "").strip():
        raise ContentError("缺少 semester")
    start = str(obj.get("startDate") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        raise ContentError("startDate 必须是 YYYY-MM-DD")
    weeks = obj.get("totalWeeks")
    if not isinstance(weeks, int) or not (1 <= weeks <= 30):
        raise ContentError("totalWeeks 必须是 1~30 的整数")
    for key in ("holidays", "adjustments"):
        if key in obj and not isinstance(obj[key], list):
            raise ContentError(f"{key} 必须是数组")
        for index, item in enumerate(obj.get(key, []), 1):
            if not isinstance(item, dict):
                raise ContentError(f"{key} 第 {index} 项必须是对象")
            date = str(item.get("date") or "").strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                raise ContentError(f"{key} 第 {index} 项日期格式错误")


def _validate_papers(data: Any) -> None:
    items = _require_list(data)
    if len(items) > 500:
        raise ContentError("文库目录最多 500 项")
    seen_ids: set[str] = set()
    total_files = 0
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ContentError(f"第 {index} 项必须是对象")
        name = str(item.get("name") or item.get("title") or "").strip()
        if not name:
            raise ContentError(f"第 {index} 项缺少 name/title")
        folder_id = str(item.get("id") or "").strip()
        if folder_id:
            if folder_id in seen_ids:
                raise ContentError(f"文库目录 id 重复：{folder_id}")
            seen_ids.add(folder_id)
        raw_files = item.get("items") or item.get("files") or []
        if not isinstance(raw_files, list):
            raise ContentError(f"第 {index} 项的 items/files 必须是数组")
        total_files += len(raw_files)
        if total_files > 2000:
            raise ContentError("文库资料总数最多 2000 份")
        for file_index, file in enumerate(raw_files, 1):
            if not isinstance(file, dict):
                raise ContentError(f"「{name}」第 {file_index} 份资料必须是对象")
            file_name = str(file.get("name") or file.get("title") or "").strip()
            if not file_name:
                raise ContentError(f"「{name}」第 {file_index} 份资料缺少标题")
            url = str(file.get("url") or file.get("download_url") or "").strip()
            if url and not url.startswith(("https://", "http://")):
                raise ContentError(f"「{name}」第 {file_index} 份资料链接必须是 http(s)")


def _history_dir(content_dir: Path) -> Path:
    path = content_dir / ".history"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_history(content_dir: Path, name: str) -> None:
    files = sorted(
        (p for p in _history_dir(content_dir).glob(f"{name}-*.json")),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in files[HISTORY_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def _backup(content_dir: Path, name: str, path: Path) -> str:
    if not path.exists():
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = _history_dir(content_dir) / f"{name}-{stamp}.json"
    shutil.copy2(path, target)
    _prune_history(content_dir, name)
    return target.name


def save(content_dir: Path, name: str, data: Any) -> str:
    """校验 → 备份旧文件 → 原子替换。返回新版本号。"""
    validate(name, data)
    try:
        raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ContentError(f"内容无法序列化为 JSON：{exc}") from exc
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ContentError("内容超过 512KB 上限")

    path = _path(content_dir, name)
    _backup(content_dir, name, path)
    tmp = path.with_name(f".{name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    content.invalidate(name)
    return content.version(content_dir, name)


def history(content_dir: Path, name: str, limit: int = 10) -> list[dict[str, Any]]:
    if name not in NAMES:
        raise ContentError("不支持的内容类型")
    files = sorted(
        _history_dir(content_dir).glob(f"{name}-*.json"),
        key=lambda p: p.name,
        reverse=True,
    )[: max(1, min(limit, 50))]
    out = []
    for item in files:
        stat = item.stat()
        out.append({
            "file": item.name,
            "size": stat.st_size,
            "savedAt": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return out


def restore(content_dir: Path, name: str, filename: str) -> str:
    """从历史备份恢复；恢复动作本身也会再备份当前文件。"""
    if name not in NAMES:
        raise ContentError("不支持的内容类型")
    if not re.match(rf"^{re.escape(name)}-\d{{8}}-\d{{6}}-\d{{6}}\.json$", filename or ""):
        raise ContentError("历史文件名无效")
    path = _history_dir(content_dir) / filename
    if not path.exists():
        raise ContentError("历史文件不存在")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"历史文件无法解析：{exc}") from exc
    return save(content_dir, name, data)
