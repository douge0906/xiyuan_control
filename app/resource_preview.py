"""资源文本预览：只依赖标准库，避免为 DOCX 引入 Office/LibreOffice。

DOCX 是 ZIP + WordprocessingML；这里按文档顺序提取段落与表格文本。
`.doc`（二进制 Word 97-2003）不在此列，调用方应明确提示用户下载后用 Word 打开。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        tag = node.tag
        if tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif tag == f"{_W}tab":
            parts.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def docx_text(path: Path, max_chars: int = 200_000) -> tuple[str, bool]:
    """提取 DOCX 正文文本，返回 (文本, 是否截断)。

    仅接受 `.docx`；文件损坏、缺少 `document.xml` 或不是 ZIP 时抛 ValueError。
    """
    if path.suffix.lower() != ".docx":
        raise ValueError("unsupported_format")
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise ValueError("invalid_docx") from exc

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid_docx") from exc

    body = root.find(f"{_W}body")
    if body is None:
        raise ValueError("invalid_docx")

    chunks: list[str] = []
    for child in list(body):
        if child.tag == f"{_W}p":
            text = _paragraph_text(child)
            if text:
                chunks.append(text)
        elif child.tag == f"{_W}tbl":
            rows: list[str] = []
            for row in child.findall(f"{_W}tr"):
                cells: list[str] = []
                for cell in row.findall(f"{_W}tc"):
                    cell_parts: list[str] = []
                    for paragraph in cell.findall(f"{_W}p"):
                        text = _paragraph_text(paragraph)
                        if text:
                            cell_parts.append(text)
                    cells.append(" ".join(cell_parts).strip())
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                chunks.append("\n".join(rows))

    text = "\n\n".join(chunks).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip(), True
    return text, False
