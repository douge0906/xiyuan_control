"""课表截图识别：服务端 OCR + 字段抽取。

## 为什么放服务端

安卓端原来是「ML Kit 本地识别 → Dart 规则解析」，Web 端则直接不具备该能力。
把识别搬到服务端后：① 一套引擎同时服务安卓和 Web，Web 不再需要原生插件；
② 解析规则只有一份，改一次两端同时生效；③ 客户端只上传图片、接收字段，
不用把 3~10MB 的图片在端上解码再跑识别。

## 引擎

`rapidocr_onnxruntime`（PP-OCRv4 轻量模型，CPU 推理）。选它而不是 PaddleOCR 全量
或 tesseract：纯 wheel、无系统依赖、模型随包自带，小云主机装得下也跑得动；
tesseract 的中文识别在截图这种小字号上明显更差。

## 返回什么

**字段**，不是原始文本：`name / className / academicYear / academicTerm /
teacher / classroom / weekday / startSlot / endSlot / weeks`，与客户端
`RecognizedCourse` 一一对应。`className` 来自「班级名称/班级」列或无表头兜底，
`academicYear`/`academicTerm` 从 8 位日期推（8 月~次年 1 月第一学期、2~7 月
第二学期）。原始文本块只在 `blocks` 里附带返回，供出问题时排查，不参与客户端渲染。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
from datetime import date
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from . import webproxy

router = APIRouter()

#: 单张图片解码后的上限（手机截图一般 0.5~3MB，10MB 足够且能挡住误传大图）
MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: JSON/base64 请求体上限（base64 约比原图大 1/3，留 2MB 余量）
MAX_BODY_BYTES = 15 * 1024 * 1024
#: 识别结果最多返回多少门候选，避免异常图把响应撑爆
MAX_COURSES = 60

_engine: Any = None
_engine_lock = threading.Lock()
_infer_lock = threading.Lock()


def _get_engine():
    """懒加载识别引擎（线程安全）。

    `_recognize_sync` 跑在 `asyncio.to_thread` 的工作线程里，同一进程可能有
    多个 OCR 请求并发进入；没有锁会同时构造多个 RapidOCR 实例，白耗内存。
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR  # 懒导入：缺包时应用照常启动

                _engine = RapidOCR()
    return _engine


# ---------------------------------------------------------------- 字段抽取
#
# 规则与客户端 `lib/services/ocr/course_ocr_parser.dart` 完全一致（同一套正则、
# 同一套打分），这样「端上离线跑」和「服务端跑」不会给出两种答案。

_WEEKDAY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

_TIME_RANGE = re.compile(r"\d{1,2}:\d{2}\s*[-~–—]\s*\d{1,2}:\d{2}")
_WEEK_RANGE = re.compile(
    r"(\d{1,2}\s*[-~–—]\s*\d{1,2}\s*周(?:\s*[（(][单双][)）])?)"
)
_WEEK_SINGLE = re.compile(r"(\d{1,2}\s*周(?:\s*[（(][单双][)）])?)")
_SLOT_RANGE = re.compile(r"第?\s*(\d{1,2})\s*[-~–—]\s*(\d{1,2})\s*节?")
_SLOT_SINGLE = re.compile(r"第?\s*(\d{1,2})\s*节")
_WEEKDAY = re.compile(r"(?:星期|周)\s*([一二三四五六日天])")
_FEATURE_WORDS = (
    "实验", "基础", "原理", "设计", "英语", "数学", "物理", "化学", "电路",
    "电子", "力学", "平台课", "概论", "语言", "制图", "编程", "程序",
)
_CLASSROOM_WORDS = re.compile(
    r"实验室|教室|场地|地点|教学楼|实训|思齐楼|电路与电子技术实验室"
)
_TEACHER_LIKE = re.compile(r"^[一-龥]{2,4}\s*[(（]\d{1,6}[)）]$")
_HAS_CN = re.compile(r"[一-龥]")
_SPLIT = re.compile(r"[/／,，;；\s]+")


def _normalize(raw: str) -> str:
    text = raw.replace("▲", " ").replace("□", " ").replace("undefined", " ")
    # 剔除 8 位日期（20261212 这类 20 开头的年月日），不误伤工号/实验室编号
    text = re.sub(r"\b20\d{6}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_label(text: str, labels: list[str]) -> str:
    for label in labels:
        m = re.search(rf"{label}\s*[:：]\s*([^/，,\s]+)", text)
        if m:
            return m.group(1).strip()
    return ""


def _extract_weekday(text: str) -> int:
    m = _WEEKDAY.search(text)
    return _WEEKDAY_CN.get(m.group(1), 0) if m else 0


def _extract_weeks(text: str) -> str:
    m = _WEEK_RANGE.search(text)
    if m:
        return m.group(1).replace("～", "-").strip()
    m = _WEEK_SINGLE.search(text)
    return m.group(1).strip() if m else ""


def _extract_slots(text: str) -> tuple[int, int] | None:
    t = _TIME_RANGE.sub(" ", text)
    t = _WEEK_RANGE.sub(" ", t)
    t = _WEEK_SINGLE.sub(" ", t)
    m = _SLOT_RANGE.search(t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= 12:
            return a, b
    m = _SLOT_SINGLE.search(t)
    if m:
        a = int(m.group(1))
        if 1 <= a <= 12:
            return a, a
    return None


def _is_code_only(s: str) -> bool:
    return not re.sub(r"[(（）()\d\s]", "", s)


def _classroom_fallback(text: str) -> str:
    for part in _SPLIT.split(text):
        t = part.strip()
        if t and _CLASSROOM_WORDS.search(t):
            return re.sub(r"[(（][^)）]*[)）]", "", t).strip()
    return ""


def _extract_name(text: str, teacher: str, classroom: str) -> str:
    rest = text
    rest = re.sub(r"(教师|老师|场地|教室|地点|上课地点)\s*[:：]\s*[^/，,\s]+", " ", rest)
    rest = _TIME_RANGE.sub(" ", rest)
    rest = _WEEK_RANGE.sub(" ", rest)
    rest = _WEEK_SINGLE.sub(" ", rest)
    rest = re.sub(r"第?\s*\d{1,2}\s*[-~–—]\s*\d{1,2}\s*节?", " ", rest)
    rest = re.sub(r"第?\s*\d{1,2}\s*节", " ", rest)
    rest = re.sub(r"(?:星期|周)\s*[一二三四五六日天]", " ", rest)

    segments = []
    for seg in _SPLIT.split(rest):
        s = re.sub(r"[(（]\d+[)）]", "", seg).strip()
        if not s or not _HAS_CN.search(s):
            continue
        if _CLASSROOM_WORDS.search(s):
            continue
        if _TEACHER_LIKE.match(s):
            continue
        if _is_code_only(s):
            continue
        if s == teacher or (classroom and s == classroom):
            continue
        segments.append(s)
    if not segments:
        return ""

    def score(s: str) -> int:
        return (100 if any(w in s for w in _FEATURE_WORDS) else 0) + len(s)

    segments.sort(key=score, reverse=True)
    return segments[0]


def parse_block(raw: str) -> dict[str, Any] | None:
    """把一个文本块解析成一门候选课程；抽不出课程名返回 None。"""
    # 学年/学期要从**原始**文本里的 8 位日期推；`_normalize` 会先删日期，
    # 删完再取就永远是空字符串（线上合成表格实测 weekday=0、学期为空）。
    raw_dates = _DATE_8.findall(raw)
    text = _normalize(raw)
    if not text:
        return None

    teacher = _extract_label(text, ["教师", "老师"])
    classroom = _extract_label(text, ["场地", "教室", "实验室", "地点", "上课地点"])
    if not classroom:
        classroom = _classroom_fallback(text)
    klass = _extract_label(text, ["班级名称", "班级"]) or _class_from_text(text)
    weeks = _extract_weeks(text)
    weekday = _extract_weekday(text)
    if not weekday and raw_dates:
        # 卡片式块里常常只有 8 位日期、没有「星期X」标签。
        weekday = _date_weekday(raw_dates[0])
    slots = _extract_slots(text)
    # 0 表示未识别，不再伪装成「周一第 1 节」。
    start, end = slots if slots else (0, 0)
    name = _extract_name(text, teacher, classroom)
    if not name:
        return None
    academic_year, academic_term = _academic_from_dates(raw_dates)
    return {
        "name": name,
        "className": _clean_class(klass),
        "academicYear": academic_year,
        "academicTerm": academic_term,
        "teacher": teacher,
        "classroom": classroom,
        "weekday": weekday,
        "startSlot": start,
        "endSlot": end,
        "weeks": weeks,
    }


def _is_junk_course_name(name: str) -> bool:
    """这个名字明显不是课程：表头字段名、纯数字、8 位日期、单字。

    主要防 `parse_blocks` 兜底把表格的表头行、周次列当成课程（实测会出现
    「周次」「日期」「星期」「上课时间」这种候选，用户点进去全是空的）。
    """
    t = re.sub(r"\s+", "", name or "")
    if not t:
        return True
    if len(t) <= 1:
        return True
    if t.isdigit():
        return True
    if _DATE_8.fullmatch(t):
        return True
    for label in _HEADER_LABELS:
        if t == label:
            return True
    return False


def parse_blocks(blocks: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for b in blocks:
        c = parse_block(b)
        if c is None:
            continue
        if _is_junk_course_name(c.get("name", "")):
            continue
        # 教师/教室也进去重键：同一门课不同老师/不同教室是不同上课记录，
        # 只按课程名+时间会把它们误并成一条。
        key = (f"{c['name']}|{c.get('className', '')}|"
               f"{c.get('academicYear', '')}|{c.get('academicTerm', '')}|"
               f"{c.get('teacher', '')}|{c.get('classroom', '')}|"
               f"{c['weekday']}|{c['startSlot']}-{c['endSlot']}|{c['weeks']}")
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out[:MAX_COURSES]


# ---------------------------------------------------------------- 版面聚块

def _cluster(lines: list[dict[str, Any]]) -> list[str]:
    """把识别出的文本行聚成「课程块」。

    表格截图里一门课是一个单元格：若干行文字纵向紧挨、横向重叠。单纯按 y 聚类
    会把同一行里并排的几门课拼成一句（T3 那次的「识别混乱」就是这么来的），
    所以这里同时看纵向间距与横向重叠。
    """
    if not lines:
        return []

    def box_ok(a: dict[str, Any], b: dict[str, Any]) -> bool:
        ha = a["bottom"] - a["top"]
        hb = b["bottom"] - b["top"]
        gap = max(0.0, max(a["top"], b["top"]) - min(a["bottom"], b["bottom"]))
        if gap > 0.75 * max(ha, hb, 8.0):
            return False
        overlap = min(a["right"], b["right"]) - max(a["left"], b["left"])
        return overlap >= 0.35 * max(1.0, min(a["right"] - a["left"], b["right"] - b["left"]))

    n = len(lines)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            # 同一列附近才需要比较，纵向差太远直接跳过
            if abs(lines[i]["top"] - lines[j]["top"]) > 120:
                continue
            if box_ok(lines[i], lines[j]):
                union(i, j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for i, ln in enumerate(lines):
        groups.setdefault(find(i), []).append(ln)

    texts: list[str] = []
    for root in sorted(groups, key=lambda r: (lines[r]["top"], lines[r]["left"])):
        members = groups[root]
        members.sort(key=lambda x: (x["top"], x["left"]))
        texts.append(" ".join(m["text"] for m in members))
    return texts


# ---------------------------------------------------------------- 表格还原
#
# 教务/实验室的课表截图绝大多数是「一行一条上课记录」的宽表（列 = 周次/星期/
# 时间/节次/班级/课程名称/项目名称/实验室/教师，或 课程/周次/星期/节次/班级/
# 实验室）。这类图不能按「一门课一张卡片」去解析：一行里并排放着好几列，
# 合并成一串只会得到垃圾。所以先把 OCR 的行按**纵向空白投影**切成列、按横向
# 空白切成行，再按列的内容判断它是什么字段，最后把同一门课的多次上课记录合并。

_HEADER_LABELS = {
    # 「项目名称」在实验课表里就是这一节课上做什么（基本门电路、三态输出门…），
    # 表里往往根本没有「课程名称」列，所以它要在缺课程名时顶上 name。见 record 组装。
    "课程": "name", "课程名称": "name", "项目名称": "project",
    "周次": "weeks", "星期": "weekday", "节次": "slots", "节次/时间": "slots",
    "上课时间": "slots", "节次时间": "slots",
    "时间": "date", "日期": "date", "班级": "klass", "班级名称": "klass",
    "实验室": "classroom", "教室": "classroom", "场地": "classroom", "地点": "classroom",
    "教师": "teacher", "老师": "teacher",
    "指导教师": "teacher", "指导教师姓名": "teacher", "任课教师": "teacher",
    "授课教师": "teacher", "教师姓名": "teacher",
}

_W_DAYS = re.compile(r"星期\s*([一二三四五六日天])")
_W_DAYS_NUM = re.compile(r"星期\s*([1-7])")
_WEEKS_TOKEN = re.compile(r"第?\s*(\d{1,2})(?:\s*[-–~—]\s*(\d{1,2}))?\s*周")
_SLOTS_TOKEN = re.compile(r"(\d{1,2})\s*[-–~—]\s*(\d{1,2})")
_DATE_SLOT = re.compile(r"20\d{6}\s*[（(]?\s*(\d{1,2})\s*[-–~—]\s*(\d{1,2})")
_DATE_8 = re.compile(r"20\d{6}")
_LAB_WORD = re.compile(r"实验室|教室|场地|地点|教学楼|楼")
_CN_NAME = re.compile(r"^[一-龥]{2,4}")


def _runs(spans: list[tuple[float, float]], gap_min: float, max_end: float) -> list[tuple[int, int]]:
    """把若干区间按「空白 ≥ gap_min 就断开」合并成带。

    用来切列（x 方向）与切行（y 方向）：表格里同一列的字符在纵向上是连着的，
    列与列之间有稳定的空白沟。
    """
    if not spans:
        return []
    occ = bytearray(int(max_end) + 2)
    for a, b in spans:
        ia, ib = max(0, int(a)), max(1, int(b))
        occ[ia:ib + 1] = b"\x01" * (ib - ia + 1)

    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(occ):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(occ) - 1))

    merged: list[tuple[int, int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= gap_min:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)
    return merged


def _assign(bands: list[tuple[int, int]], v: float) -> int:
    for i, (a, b) in enumerate(bands):
        if a <= v <= b:
            return i
    best, dist = 0, 1e18
    for i, (a, b) in enumerate(bands):
        d = 0 if a <= v <= b else min(abs(v - a), abs(v - b))
        if d < dist:
            best, dist = i, d
    return best


def _row_bands(lines: list[dict[str, Any]], line_h: float) -> list[tuple[float, float]]:
    """按文字中心聚类切行。

    Excel 截图里上下两行的文字框可能只隔 1~2px，按「空白 ≥2.5px」切会把十几行
    粘成一行；但同一行的文字中心相差很小、相邻行相差约一个行高。这里改用中心
    距离聚类，gap 取 0.55 行高，既不会把同一行拆开，也不会把相邻行粘住。
    """
    order = sorted(lines, key=lambda ln: (ln["top"] + ln["bottom"]) / 2)
    groups: list[list[float]] = []
    gap = max(4.0, line_h * 0.55)
    for ln in order:
        c = (ln["top"] + ln["bottom"]) / 2
        if not groups or c - groups[-1][-1] > gap:
            groups.append([c])
        else:
            groups[-1].append(c)

    bands: list[tuple[float, float]] = []
    for g in groups:
        lo, hi = min(g), max(g)
        members = [ln for ln in order if lo <= (ln["top"] + ln["bottom"]) / 2 <= hi]
        if members:
            bands.append((min(ln["top"] for ln in members),
                          max(ln["bottom"] for ln in members)))
    return bands


def _fmt_weeks(nums: list[int], raw: str) -> str:
    """把周次数字压成 `2,6-7,10-13周`；一个数字都没抽到时退回原始文本。"""
    nums = sorted({n for n in nums if 1 <= n <= 30})
    if not nums:
        return raw.strip()
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(parts) + "周"


def _cell_weekday(text: str) -> int:
    m = _W_DAYS.search(text)
    if m:
        return _WEEKDAY_CN.get(m.group(1), 0)
    m = _W_DAYS_NUM.search(text)
    if m:
        return int(m.group(1))
    t = text.strip()
    if t.isdigit() and 1 <= int(t) <= 7:
        return int(t)
    return 0


def _cell_slots(text: str) -> tuple[int, int] | None:
    # Excel 里日期和节次会粘在一起：202610101-2(08:00-09:40) 表示 1-2 节。
    # 先按日期后面的两位数取，避免正则把 101-2 误读成 10-2。
    m = _DATE_SLOT.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= 12:
            return a, b
    for m in _SLOTS_TOKEN.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= 12:
            return a, b
    return None


def _date_weekday(text: str) -> int:
    """从 20261011 这类 8 位日期反推星期（1=周一…7=周日）。"""
    for m in re.finditer(r"(?<!\d)(20\d{6})", text):
        raw = m.group(1)
        try:
            d = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        except ValueError:
            continue
        if 1 <= d.isoweekday() <= 7:
            return d.isoweekday()
    return 0


def _cell_weeks(text: str) -> list[int]:
    out: list[int] = []
    for m in _WEEKS_TOKEN.finditer(text):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if 1 <= a <= 30 and 1 <= b <= 30 and b >= a:
            out.extend(range(a, b + 1))
    if not out:
        # 实验室课表的「周次」列常常只写数字（8 / 9 / 10），没有「周」字
        for tok in re.findall(r"\d{1,2}", text):
            n = int(tok)
            if 1 <= n <= 30:
                out.append(n)
    return out


def _join_cjk(text: str) -> str:
    """拼接被 OCR 拆开的同一格文字：只在两个汉字之间去空格，英文/数字间的空格保留。"""
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch == " " and 0 < i < len(text) - 1 and "\u4e00" <= text[i - 1] <= "\u9fa5" \
                and "\u4e00" <= text[i + 1] <= "\u9fa5":
            continue
        out.append(ch)
    return "".join(out).strip()


_WEEK_CELL = re.compile(r"第?\s*\d{1,2}\s*周")
_SLOT_CELL = re.compile(r"\d{1,2}\s*[-–~—]\s*\d{1,2}")
_TEACHER_CELL = re.compile(r"[一-龥]{2,4}\s*[(（]\s*\d{2,8}\s*[)）]")
_NAME_HINT = re.compile(r"实验|基础|原理|设计|英语|数学|物理|化学|电路|电子|力学|制图|编程|程序|平台课|概论")

# 没有表头的单行截图：OCR 只按视觉行切框，连「第14」和「周」都会分成两行。
# 这里不依赖列位置，直接按内容特征把字段认回来；只在表格还原失败时兜底。
_CODE_COURSE = re.compile(r"[(（]\s*(\d[\d\s]{7,14})\s*[)）]")
_CODE_COURSE_LOOSE = re.compile(r"[(（]\s*(\d[\d\s]{7,14})")
_CODE_LONG = re.compile(r"[(（]\s*\d{4,}\s*[)）]")
_CODE_TEACHER = re.compile(r"[(（]\s*(\d{6})\s*[)）]")
_CJK_CHAR = re.compile(r"[一-龥]")
_CLASS_PREFIX = re.compile(r"^(\d{2}[一-龥]{1,8}\d)(?=[一-龥])")
# 班级 token：25微电子1 / 2025级电气1班。用于「班级名称」列和无表头兜底。
_CLASS_TOKEN = re.compile(r"\d{2,4}(?:级)?[一-龥]{1,12}\d{0,2}(?:班)?")
_CLASS_GROUP = re.compile(
    r"\d{2,4}(?:级)?[一-龥]{1,12}\d{0,2}(?:班)?"
    r"(?:\s*[,，、;；.．]\s*\d{2,4}(?:级)?[一-龥]{1,12}\d{0,2}(?:班)?)*"
)
_CLASS_SPLIT = re.compile(r"[,，、;；.．/]+")


def _class_core(raw: str) -> str:
    """去掉空白和末尾括号说明，只留下可用于校验的班级主体。

    Excel 的合班列会出现 `25电科1,25电科2（25电科1）`：括号是合班里
    某个行政班的说明，不是班级主体的一部分；校验主体时不能把它算进去。
    """
    s = re.sub(r"\s+", "", raw.strip())
    s = re.sub(r"[（(][^）)]*[）)]$", "", s)
    return s


def _clean_class(raw: str) -> str:
    """班级列规范化：`25电气1,25电气2,25电气3` → `25电气1、25电气2、25电气3`。

    OCR 常把合班列的小数点/顿号认错，或者把 `25电气3` 吃掉只剩 `25`；
    只保留能完整匹配班级 token 的段，避免把残缺数字带进课表。括号说明
    保留在输出里（例如 `25电科2（25电科1）`），因为它属于原始班级信息。
    """
    text = _clean_field(raw)
    if not text:
        return ""
    # OCR 偶尔把合班串的顿号吞掉：`25电科125电科2`。在两个班级 token 之间
    # 补一个顿号，再走正常分段校验。
    text = re.sub(r"(?<=\d)(?=\d{2,4}(?:级)?[一-龥])", "、", text)
    parts = [p.strip() for p in _CLASS_SPLIT.split(text) if p.strip() and p.strip()]
    kept: list[str] = []
    for p in parts:
        core = _class_core(p)
        if _CLASS_TOKEN.fullmatch(core):
            kept.append(re.sub(r"\s+", "", p))
    if len(kept) >= 2:
        return "、".join(kept)
    if len(kept) == 1:
        return kept[0]
    return text


def _class_from_text(text: str) -> str:
    """从一行 OCR 文本里找班级组（单行兜底 / 卡片式块共用）。"""
    m = _CLASS_GROUP.search(text)
    return _clean_class(m.group(0)) if m else ""


def _title_class(lines: list[dict[str, Any]], rows: list[tuple[int, int]],
                 header_r: int, line_h: float) -> str:
    """从表格标题行里抓班级。

    实验课表的表头没有「班级」列，班级只写在表格上方的大标题里
    （如「25级电气3班」）；脚注里也可能带班级，所以表格下方那几行一并当候选。
    """
    if len(lines) < 3:
        return ""
    cands: list[str] = []
    limit = rows[header_r][0] if header_r >= 0 else rows[1][0]
    for ln in lines:
        cy = (ln["top"] + ln["bottom"]) / 2
        if cy < limit or cy > rows[-1][0]:
            cands.append(str(ln.get("text") or ""))
    if not cands:
        return ""
    for text in cands:
        m = _CLASS_GROUP.search(re.sub(r"\s+", "", text))
        if m:
            got = _clean_class(m.group(0))
            if got:
                return got
    return ""


def _looks_class_cell(raw: str) -> bool:
    """单元格是否整格都是班级 token（支持合班用逗号/顿号分隔）。"""
    parts = [p.strip() for p in _CLASS_SPLIT.split(raw.strip()) if p.strip()]
    return bool(parts) and all(_CLASS_TOKEN.fullmatch(_class_core(p)) for p in parts)


def _academic_from_dates(dates: list[str]) -> tuple[str, str]:
    """8 位日期 → `(2026-2027, 第一学期)`；推不出返回两个空串。"""
    for raw in dates:
        digits = re.sub(r"[^0-9]", "", raw)
        if len(digits) < 8:
            continue
        y = int(digits[:4])
        m = int(digits[4:6])
        if y < 2000 or y > 2100 or m < 1 or m > 12:
            continue
        start = y if m >= 8 else y - 1
        term = "第一学期" if (m >= 8 or m == 1) else "第二学期"
        return f"{start}-{start + 1}", term
    return "", ""
_JUNK_TEXT = re.compile(r"^(?:on|undefined|口|日|B|周)$", re.I)
_NOT_PERSON = re.compile(r"基本|应用|实验|原理|技术|电路|电子|课程|班级|周次|星期|节次|时间|教师|实验室|楼")


def _column_kind(values: list[str]) -> str:
    """按一列的实际内容判断它是什么字段（没有表头时靠这个）。"""
    vals = [v.strip() for v in values if v.strip()]
    if not vals:
        return "ignore"
    n = len(vals)
    ratio = lambda f: sum(1 for v in vals if f(v)) / n  # noqa: E731

    if ratio(lambda v: bool(_WEEK_CELL.search(v))) >= 0.5:
        return "weeks"
    if ratio(lambda v: bool(_W_DAYS.search(v) or _W_DAYS_NUM.search(v))) >= 0.5:
        return "weekday"
    if ratio(lambda v: bool(_DATE_8.fullmatch(v.replace(" ", ""))) or bool(_DATE_8.search(v))) >= 0.6:
        return "date"
    if ratio(lambda v: _cell_slots(v) is not None and (
        ":" in v or "节" in v or _SLOT_CELL.search(v))) >= 0.5:
        return "slots"
    if ratio(lambda v: bool(_LAB_WORD.search(v))) >= 0.5:
        return "classroom"
    # 班级列（25微电子1 / 25电气1,25电气2）常被误判成教师列，先认它。
    if ratio(_looks_class_cell) >= 0.5:
        return "klass"
    if ratio(lambda v: bool(_TEACHER_CELL.search(v))) >= 0.4:
        return "teacher"
    # 无表头的宽表里，课程列不一定含「实验/原理/平台课」等关键词（例如
    # 「科技论文写作Ⅱ」）。课程编码是最稳定的特征：8~10 位数字，即使 OCR
    # 把编码中间断开（17150301 2），仍然保留至少 8 位连续数字。
    if ratio(lambda v: bool(re.search(r"\d{8,10}", v))) >= 0.4:
        return "name"
    if ratio(lambda v: bool(_NAME_HINT.search(v))) >= 0.3:
        return "name"
    if ratio(lambda v: v.isdigit() and 1 <= int(v) <= 7) >= 0.75:
        return "weekday"
    if ratio(lambda v: bool(_CN_NAME.match(v))) >= 0.6:
        return "teacher"
    return "ignore"


def _clean_course_name(text: str) -> str:
    """去掉课程名后面的课程编码；括号可能被 OCR 吃掉一半，也一起清掉。"""
    s = re.sub(r"[(（]\s*\d[\d\s]{7,14}\s*[)）]?", "", text)
    s = re.sub(r"[(（]\s*\d[\d\s]{7,14}\s*$", "", s)
    return _join_cjk(s).strip(" -—")


def _canonical_course_names(records: list[dict[str, Any]]) -> None:
    """同一课程编码只保留一个课程名。

    OCR 会把「Ⅱ」认成「工」、或吃掉右括号；用课程编码把同码记录归并，取出现
    次数最多的写法作为标准名，避免同一门课被拆成两条。
    """
    buckets: dict[str, list[str]] = {}
    for rec in records:
        code = rec.get("_code") or ""
        name = rec.get("name") or ""
        if code and name:
            buckets.setdefault(code, []).append(name)
    canon: dict[str, str] = {}
    for code, names in buckets.items():
        uniq = set(names)
        canon[code] = max(uniq, key=lambda n: (names.count(n), len(n)))
    for rec in records:
        code = rec.get("_code") or ""
        if code and code in canon:
            rec["name"] = canon[code]


def _table_courses(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把表格截图还原成课程字段；判断不出表格结构时返回空列表。"""
    if len(lines) < 6:
        return []

    heights = sorted(ln["bottom"] - ln["top"] for ln in lines)
    line_h = heights[len(heights) // 2] or 10.0
    max_bottom = max(ln["bottom"] for ln in lines)

    # 行：按纵向空白切。表格行距只有几个像素，阈值放很小；同一单元格里换行的
    # 两段文字间距更小，会被合到同一行（本来就该合）。
    rows = _row_bands(lines, line_h)
    if len(rows) < 2:
        return []

    def row_lines(r: int) -> list[dict[str, Any]]:
        a, b = rows[r]
        return [ln for ln in lines if a <= (ln["top"] + ln["bottom"]) / 2 <= b]

    # 表头：前两行里，若某一行有 ≥2 个单元格以字段名开头，就按该行的单元格定列
    header_r = -1
    header_cells: list[tuple[float, float, str, str]] = []
    labels = sorted(_HEADER_LABELS.items(), key=lambda kv: -len(kv[0]))
    for r in range(min(2, len(rows))):
        cells = _runs([(ln["left"], ln["right"]) for ln in row_lines(r)],
                      max(4.0, line_h * 0.25), max(ln["right"] for ln in lines))
        runs: list[tuple[float, float, str, str]] = []
        hits = 0
        for a, b in cells:
            text = " ".join(x["text"] for x in sorted(row_lines(r), key=lambda z: z["left"])
                            if a <= x["left"] <= b).strip()
            head = re.sub(r"\s+", "", text)
            kind = ""
            for label, k in labels:
                if head.startswith(label):
                    kind = k
                    hits += 1
                    break
            runs.append((a, b, text, kind))
        if hits >= 2:
            header_r, header_cells = r, runs
            break

    if header_r >= 0:
        # 只保留认得的字段名，以及「人数/强排/状态/申请时间/操作」这类真实但
        # 我们不需要的列。后者保留成 ignore，否则数据会被吸到最近的教师/实验室列。
        kept: list[tuple[float, float, str, str]] = []
        for a, b, text, kind in header_cells:
            if kind:
                kept.append((a, b, text, kind))
            elif len(_CJK_CHAR.findall(re.sub(r"\s+", "", text))) >= 2:
                kept.append((a, b, text, "ignore"))
        header_cells = kept
        centers = [((a + b) / 2, kind) for a, b, _t, kind in header_cells]
        col_kind = [kind for _c, kind in centers]
        n_cols = len(centers)
        data_rows = list(range(header_r + 1, len(rows)))

        def cell_of(ln: dict[str, Any]) -> int:
            return min(range(len(centers)), key=lambda i: abs(
                (ln["left"] + ln["right"]) / 2 - centers[i][0]))
    else:
        # 没有表头（教务列表视图那种）：按左边界聚类定列，再按列内容猜字段
        tol = max(4.0, line_h * 0.45)
        starts: list[float] = []
        for v in sorted(ln["left"] for ln in lines):
            if not starts or v - starts[-1] > tol:
                starts.append(v)
        if len(starts) < 3:
            return []
        n_cols = len(starts)
        data_rows = list(range(len(rows)))

        def cell_of(ln: dict[str, Any]) -> int:
            return min(range(len(starts)), key=lambda i: abs(ln["left"] - starts[i]))

    # 行带重算：合并单元格（跨十几行的课程名）会把整图的行粘在一起，换行的
    # 课程名又会让某一列切出双倍行数。所以以「各列行数的中位数」为行基准，
    # 取行数等于中位数、且文本行最多的那一列来定行。
    header_y = (rows[header_r][0] + rows[header_r][1]) / 2 if header_r >= 0 else None
    col_line_count: list[int] = []
    col_bands: dict[int, list[tuple[int, int]]] = {}
    col_text: dict[int, str] = {}
    for c in range(n_cols):
        col_lines = [ln for ln in lines if cell_of(ln) == c]
        col_line_count.append(len(col_lines))
        # 合并单元格取值要排除表头那行文字（否则会拼出「课程PLC原理及应用」）
        body = [ln for ln in col_lines
                if header_y is None or abs((ln["top"] + ln["bottom"]) / 2 - header_y) > line_h]
        col_text[c] = " ".join(
            x["text"] for x in sorted(body, key=lambda z: (z["top"], z["left"])))
        if col_lines:
            col_bands[c] = _runs([(ln["top"], ln["bottom"]) for ln in col_lines], 2.5, max_bottom)
        col_line_count[c] = len(body)
    counts = [n for n in col_line_count if n >= 2]
    if counts:
        med = sorted(counts)[len(counts) // 2]
        cands = [c for c in range(n_cols) if c in col_bands and col_line_count[c] >= 2
                 and len(col_bands[c]) - (1 if header_y is not None else 0) == med]
        if cands:
            bands = col_bands[max(cands, key=lambda c: col_line_count[c])]
            if header_y is not None:
                bands = [b for b in bands if not (b[0] - 2 <= header_y <= b[1] + 2)]
                # 表头行已剔除，表头那几个字也不能再参与数据格拼接
                lines = [ln for ln in lines
                         if abs((ln["top"] + ln["bottom"]) / 2 - header_y) > line_h * 0.9]
            rows = bands
            data_rows = list(range(len(rows)))
            header_y = None  # 表头行已从行带里剔掉，数据行就是全部行
    if header_y is not None:
        data_rows = list(range(_assign(rows, header_y) + 1, len(rows)))
    else:
        data_rows = list(range(len(rows)))

    grid: list[list[list[dict[str, Any]]]] = [[[] for _ in range(n_cols)] for _ in rows]
    for ln in lines:
        r = _assign(rows, (ln["top"] + ln["bottom"]) / 2)
        grid[r][cell_of(ln)].append(ln)

    cell_text: list[list[str]] = []
    for r in range(len(rows)):
        row_texts: list[str] = []
        for c in range(len(grid[r])):
            items = sorted(grid[r][c], key=lambda z: (z["top"], z["left"]))
            row_texts.append(" ".join(x["text"] for x in items).strip())
        cell_text.append(row_texts)

    if header_r >= 0:
        # 剥掉表头文字（有的截图把表头和第一条数据挤在同一格）
        for c, (_a, _b, _t, kind) in enumerate(header_cells):
            head = re.sub(r"\s+", "", cell_text[header_r][c])
            for label, k in labels:
                if k == kind and head.startswith(label):
                    cell_text[header_r][c] = head[len(label):].strip()
                    break
    else:
        col_kind = [_column_kind([cell_text[r][c] for r in range(len(rows))])
                    for c in range(len(cell_text[0]))]
        # 纯数字的星期列（1/3）不好从内容上区分于其它数字列，若同一张表里已经
        # 出现了带「星期X」的列，就不再猜第二个
        if col_kind.count("weekday") > 1:
            first = col_kind.index("weekday")
            for i, k in enumerate(col_kind):
                if k == "weekday" and i != first:
                    col_kind[i] = "ignore"

    # 合并单元格：课程名/教师/实验室整列只有寥寥几行文字、却横跨很多条记录时，
    # 直接取整列文字作为这些记录的共同值（比逐行前向填充更稳，OCR 常把合并格的
    # 文字排在格的垂直中线上，落进中间某一行）。
    merged_vals: dict[int, str] = {}
    for c, kind in enumerate(col_kind):
        if kind in ("name", "teacher", "classroom", "klass", "project") and len(data_rows) >= 3:
            if 0 < col_line_count[c] and col_line_count[c] * 2 < len(data_rows):
                merged_vals[c] = _join_cjk(col_text[c])

    records: list[dict[str, Any]] = []
    carry = {"name": "", "teacher": "", "classroom": "", "klass": "", "code": ""}
    for r in data_rows:
        rec = {"name": "", "teacher": "", "classroom": "", "klass": "",
               "weekday": 0, "startSlot": 0, "endSlot": 0, "weeks": "",
               "weeksNum": [], "_code": "", "_dates": [], "_project": ""}
        for c, kind in enumerate(col_kind):
            if c in merged_vals:
                if kind == "name" and not rec["name"]:
                    m = _CODE_COURSE_LOOSE.search(merged_vals[c])
                    if m:
                        rec["_code"] = re.sub(r"[^0-9]", "", m.group(1))
                    rec["name"] = _clean_course_name(merged_vals[c])
                elif kind == "teacher" and not rec["teacher"]:
                    rec["teacher"] = re.sub(r"[(（][^)）]*[)）]", "", merged_vals[c]).strip()
                elif kind == "classroom" and not rec["classroom"]:
                    rec["classroom"] = re.sub(r"[(（]\s*\d{4,}\s*[)）]", "", merged_vals[c]).strip()
                elif kind == "klass" and not rec["klass"]:
                    rec["klass"] = _clean_class(merged_vals[c])
                elif kind == "project" and not rec["_project"]:
                    rec["_project"] = merged_vals[c]
                continue
            cell = cell_text[r][c] if c < len(cell_text[r]) else ""
            if not cell:
                continue
            if kind == "name" and not rec["name"]:
                m = _CODE_COURSE_LOOSE.search(cell)
                if m:
                    rec["_code"] = re.sub(r"[^0-9]", "", m.group(1))
                rec["name"] = _clean_course_name(cell)
            elif kind == "teacher" and not rec["teacher"]:
                rec["teacher"] = re.sub(r"[(（][^)）]*[)）]", "", cell).strip()
            elif kind == "classroom" and not rec["classroom"]:
                rec["classroom"] = re.sub(r"[(（]\s*\d{4,}\s*[)）]", "", cell).strip()
            elif kind == "klass" and not rec["klass"]:
                rec["klass"] = _clean_class(cell)
            elif kind == "project" and not rec["_project"]:
                rec["_project"] = cell
            elif kind == "weekday" and not rec["weekday"]:
                rec["weekday"] = _cell_weekday(cell)
            elif kind == "date":
                if not rec["weekday"]:
                    rec["weekday"] = _date_weekday(cell)
                # 星期列可能排在日期列前面，这里不能因为 weekday 已有值就跳过
                # 日期采集——学年归属完全靠这个 8 位日期。
                rec["_dates"].extend(_DATE_8.findall(cell))
            elif kind == "slots" and not rec["startSlot"]:
                s = _cell_slots(cell)
                if s:
                    rec["startSlot"], rec["endSlot"] = s
                if not rec["weekday"]:
                    rec["weekday"] = _date_weekday(cell)
                # Excel 截图常把日期和节次粘在同一格：20260914 9-10(...)
                rec["_dates"].extend(_DATE_8.findall(cell))
            elif kind == "weeks" and not rec["weeks"]:
                nums = _cell_weeks(cell)
                rec["weeks"] = _fmt_weeks(nums, cell)
                rec["weeksNum"] = nums
        # 合并单元格：竖向跨行的课程名/教师/实验室/班级沿用上一行的值
        for k in ("name", "teacher", "classroom", "klass"):
            if rec[k]:
                carry[k] = rec[k]
                if k == "name":
                    carry["code"] = rec["_code"]
            else:
                rec[k] = carry[k]
                if k == "name":
                    rec["_code"] = carry["code"]
        # 实验课表常常只有「项目名称」没有「课程名称」：这时把这一节做什么
        # 当成课程名，总比整行丢掉强（用户可以在编辑里改）。
        if not rec["name"] and rec["_project"]:
            rec["name"] = _clean_course_name(rec["_project"])
        if not rec["name"]:
            continue
        # 表头里的「A/B/C/G」这类列字母不能当课程名
        if len(rec["name"]) <= 1 and re.fullmatch(r"[A-Za-z]", rec["name"]):
            continue
        rec["academicYear"], rec["academicTerm"] = _academic_from_dates(rec["_dates"])
        records.append(rec)

    # 实验课表的班级只在表格标题里（「25级电气3班」），表头没有班级列。
    # 如果一条记录都没解析出来就直接退，会让这张表整个丢掉。
    title_class = _title_class(lines, rows, header_r, line_h)
    if title_class:
        for rec in records:
            if not rec["klass"]:
                rec["klass"] = title_class
    if not records:
        return []

    _canonical_course_names(records)

    # 同一门课的多次上课记录合并：键 = 课程+班级+学年/学期+教师+教室+星期+节次，
    # 周次取并集。班级/学年必须进键，否则不同班、不同学年的实验会被并成一条。
    merged: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        key = (rec["name"], rec["klass"], rec["academicYear"],
               rec["academicTerm"], rec["teacher"], rec["classroom"],
               rec["weekday"], rec["startSlot"], rec["endSlot"])
        slot = merged.get(key)
        if slot is None:
            merged[key] = {
                "name": rec["name"], "className": rec["klass"],
                "academicYear": rec["academicYear"],
                "academicTerm": rec["academicTerm"],
                "teacher": rec["teacher"], "classroom": rec["classroom"],
                "weekday": rec["weekday"],
                "startSlot": rec["startSlot"], "endSlot": rec["endSlot"],
                "weeks": rec["weeks"], "_nums": rec["weeksNum"],
            }
        else:
            slot["_nums"] = slot["_nums"] + rec["weeksNum"]
            if rec["weeks"] and not slot["weeks"]:
                slot["weeks"] = rec["weeks"]

    out: list[dict[str, Any]] = []
    for item in merged.values():
        nums = item.pop("_nums")
        item["weeks"] = _fmt_weeks(nums, item["weeks"])
        # 0 保留为「未识别」；不再把缺节次的记录伪装成第 1 节。
        out.append(item)
        if len(out) >= MAX_COURSES:
            break
    return out


# ---------------------------------------------------------------- 单行兜底


def _clean_field(text: str) -> str:
    s = re.sub(r"^[\s)）】\]、,，.。;；:：]+", "", text.strip())
    s = re.sub(r"[\s(（]*(?:undefined|on)[\s)）]*$", "", s, flags=re.I)
    return s.strip(" \t\r\n、,，.。;；:：")


def _strip_class_prefix(text: str) -> str:
    """把 OCR 粘在课程名前面的班级（25微电子1）剥掉。"""
    s = _clean_field(text)
    m = _CLASS_PREFIX.match(s)
    if m:
        s = s[m.end():]
    return _clean_field(s)


def _looks_course(text: str) -> bool:
    t = _join_cjk(_clean_field(text))
    if len(_CJK_CHAR.findall(t)) < 2:
        return False
    if re.match(r"^[,，.。;；\d]", t):
        return False
    if _LAB_WORD.search(t) or _DATE_8.search(t):
        return False
    if _W_DAYS.search(t) or _W_DAYS_NUM.search(t) or _WEEKS_TOKEN.search(t):
        return False
    if _SLOTS_TOKEN.search(t) and ":" in t:
        return False
    if re.match(r"^实验[一二三四五六七八九十0-9]+[、.．]", t):
        return False
    return True


def _looks_person(text: str) -> bool:
    t = _clean_field(text)
    return bool(re.fullmatch(r"[一-龥]{2,4}", t)) and not _NOT_PERSON.search(t)


def _course_score(text: str) -> int:
    """给候选课程名打分；用于单行截图里从一堆碎片里挑出真正的课程名。"""
    t = _strip_class_prefix(text)
    if not _looks_course(t):
        return -1
    score = min(len(_CJK_CHAR.findall(t)), 10)
    if _NAME_HINT.search(t):
        score += 10
    if re.fullmatch(r"[一-龥]{2,4}", t):
        score -= 4  # 更像教师名，不是课程名
    if re.search(r"\d", t):
        score -= 2
    return score


def _looks_like_schedule_table(lines: list[dict[str, Any]]) -> bool:
    """粗判这张图是不是一张「表头 + 多行数据」的课表。

    只看前两行里有多少格以字段名开头：≥3 个就当课表。给 `_recognize_sync`
    的兜底用 —— 是课表却解析失败时，不能再退到「每格文字一门课」的 parse_blocks。
    """
    if len(lines) < 6:
        return False
    heights = sorted(ln["bottom"] - ln["top"] for ln in lines)
    line_h = heights[len(heights) // 2] or 10.0
    rows = _row_bands(lines, line_h)
    if len(rows) < 2:
        return False
    labels = sorted(_HEADER_LABELS.items(), key=lambda kv: -len(kv[0]))
    for r in range(min(2, len(rows))):
        a, b = rows[r]
        texts = [re.sub(r"\s+", "", str(ln.get("text") or ""))
                 for ln in lines if a <= (ln["top"] + ln["bottom"]) / 2 <= b]
        hits = sum(1 for t in texts if t and any(t.startswith(lb) for lb, _k in labels))
        if hits >= 3:
            return True
    return False


def _is_header_only(lines: list[dict[str, Any]]) -> bool:
    texts = [_clean_field(str(ln.get("text") or "")) for ln in lines]
    texts = [t for t in texts if t]
    if len(texts) < 4:
        return False
    hit = 0
    for t in texts:
        h = re.sub(r"\s+", "", t)
        if any(h == label or h.startswith(label) for label in _HEADER_LABELS):
            hit += 1
    return hit >= 4 and hit >= len(texts) * 0.6


def _single_row_courses(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """没有表头、只截到一行数据时的兜底解析。

    这类图不能按列位置猜字段：OCR 连「第14」和「周」都会拆成两行，课程名还可能
    和班级粘在一个框里。这里改成按内容特征认字段，只输出一条记录；认不出课程名
    就返回空，避免把普通截图硬解析成课程。
    """
    if not (4 <= len(lines) <= 30):
        return []

    items: list[tuple[str, dict[str, Any]]] = []
    for ln in lines:
        text = str(ln.get("text") or "").strip()
        if not text or _JUNK_TEXT.match(text):
            continue
        items.append((text, ln))
    if len(items) < 3:
        return []

    joined = " ".join(t for t, _ln in items)
    course = ""
    for i, (text, _ln) in enumerate(items):
        m = _CODE_COURSE.search(text)
        if not m:
            continue
        before = _strip_class_prefix(text[:m.start()])
        if _looks_course(before):
            course = before
        if not course:
            for j in range(i - 1, -1, -1):
                prev = _strip_class_prefix(items[j][0])
                if _looks_course(prev):
                    course = prev
                    break
        if course:
            break

    # 课程编码离课程名很远（OCR 把一条记录拆成上下两半）时，按内容特征重新挑
    cands = [(t, _course_score(t)) for t, _ln in items]
    cands = [(t, s) for t, s in cands if s >= 0]
    if cands:
        best, best_score = max(cands, key=lambda x: x[1])
        if not course or best_score > _course_score(course):
            course = best
    if not course:
        return []

    teacher = ""
    for i, (text, _ln) in enumerate(items):
        m = _CODE_TEACHER.search(text)
        if not m:
            continue
        before = _clean_field(text[:m.start()])
        if _looks_person(before):
            teacher = before
            break
        for j in range(i - 1, -1, -1):
            prev = _clean_field(items[j][0])
            if _looks_person(prev) and not _LAB_WORD.search(prev):
                teacher = prev
                break
        if teacher:
            break

    classroom = ""
    for i, (text, _ln) in enumerate(items):
        if not _LAB_WORD.search(text):
            continue
        c = _clean_field(_CODE_LONG.sub("", text))
        if i + 1 < len(items):
            # 房间号可能被 OCR 排到下一行，且中间还夹着教师名；向后找几个框
            for nxt, _nln in items[i + 1:i + 9]:
                m = re.match(r"^\s*(\d{1,4})\s*[(（]\s*\d{4,}\s*[)）]", nxt)
                if m and not re.search(r"\d$", c):
                    c += m.group(1)
                    break
        if c:
            classroom = c
            break

    nums: list[int] = []
    for m in _WEEKS_TOKEN.finditer(joined):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if 1 <= a <= 30 and 1 <= b <= 30 and b >= a:
            nums.extend(range(a, b + 1))
    # OCR 把「第14」和「周」拆成两行时，上面按整串匹配会漏；单独认「第14」
    for t, _ln in items:
        m = re.fullmatch(r"第\s*(\d{1,2})", t)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 30:
                nums.append(n)
    if not nums:
        # 周次列只写「8」、没有「周」字时，取星期列左边的那个数字
        wd_lefts = [ln["left"] for t, ln in items
                    if _W_DAYS.search(t) or _W_DAYS_NUM.search(t)]
        cands = [(ln["left"], int(t)) for t, ln in items
                 if t.isdigit() and 1 <= int(t) <= 30]
        if wd_lefts:
            before = [n for left, n in cands if left < min(wd_lefts)]
            if len(before) == 1:
                nums = before
        elif len(cands) == 1:
            nums = [cands[0][1]]

    weekday = _cell_weekday(joined)
    if not weekday:
        used = set(nums)
        cand = [int(t) for t, _ln in items
                if t.isdigit() and 1 <= int(t) <= 7 and int(t) not in used]
        if len(cand) == 1:
            weekday = cand[0]

    slots = _cell_slots(joined)
    if not (weekday or slots or nums):
        return []

    course = _join_cjk(_clean_field(_CODE_COURSE.sub("", course))).strip(" -—")
    if not course:
        return []
    dates = _DATE_8.findall(joined)
    academic_year, academic_term = _academic_from_dates(dates)
    return [{
        "name": course,
        "className": _class_from_text(joined),
        "academicYear": academic_year,
        "academicTerm": academic_term,
        "teacher": teacher,
        "classroom": classroom,
        "weekday": weekday,
        "startSlot": slots[0] if slots else 0,
        "endSlot": slots[1] if slots else 0,
        "weeks": _fmt_weeks(nums, ""),
    }]


# ---------------------------------------------------------------- 入口


def _ocr_image(img_bytes: bytes) -> list[dict[str, Any]]:
    """跑一次识别，返回带坐标的文本行。"""
    import cv2  # rapidocr 的依赖
    import numpy as np

    arr = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise ValueError("bad_image")
    # 手机截的课表常常很小（600~800px 宽），10px 上下的数字/汉字笔画细，
    # 检测阶段会整块漏掉（实测小图里「3」一个都没检出）。放大到 1200px 以上
    # 再识别，坐标按同一比例缩放，聚块逻辑不受影响。
    h, w = arr.shape[:2]
    if max(h, w) < 1200:
        scale = min(2.5, 1200 / max(1, max(h, w)))
        arr = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    engine = _get_engine()
    # RapidOCR/onnxruntime 的会话不是线程安全的；OCR 是低并发功能，
    # 串行推理比并发抢同一个 session 更稳。
    with _infer_lock:
        result, _elapse = engine(arr)
    lines: list[dict[str, Any]] = []
    for item in result or []:
        box, text, _score = item[0], item[1], item[2]
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        text = str(text).strip()
        if not text:
            continue
        lines.append(
            {
                "text": text,
                "left": min(xs),
                "right": max(xs),
                "top": min(ys),
                "bottom": max(ys),
            }
        )
    return lines


def _is_single_visual_row(lines: list[dict[str, Any]]) -> bool:
    """整张图只有一行的纵向范围（允许 OCR 把这一行拆成多个文本行）。"""
    if not lines:
        return False
    heights = sorted(ln["bottom"] - ln["top"] for ln in lines)
    line_h = heights[len(heights) // 2] or 10.0
    span = max(ln["bottom"] for ln in lines) - min(ln["top"] for ln in lines)
    return span <= line_h * 2.5


def _recognize_sync(img_bytes: bytes) -> dict[str, Any]:
    lines = _ocr_image(img_bytes)
    # 先按表格还原（教务/实验室课表都是宽表）；表结构不成立再退回单行兜底/卡片式解析。
    courses = _table_courses(lines)
    blocks = _cluster(lines)
    # 单行截图被里面的换行拆成多行时，表格解析可能猜出 2 条垃圾；以单行兜底为准。
    if _is_single_visual_row(lines):
        single = _single_row_courses(lines)
        if single and (not courses or len(courses) <= 2):
            courses = single
    if not courses:
        courses = _single_row_courses(lines)
    # 已经认出这是一张课表（有 ≥3 个表头字段）却解析不出课程，说明缺了课程名/
    # 项目名这类关键列。此时parse_blocks会把每一格文字都当成一门课，吐出一堆
    # 「周次」「日期」「星期」的假课程 —— 宁可不给结果，让用户换张图。
    if not courses and not _is_header_only(lines) and not _looks_like_schedule_table(lines):
        courses = parse_blocks(blocks)
    return {
        "courses": courses,
        "blocks": blocks[:MAX_COURSES],
        "lineCount": len(lines),
    }


def _sniff_image(img: bytes) -> bool:
    """只认 PNG / JPEG / WEBP 三种常见截图格式，别让人把任意二进制喂进来。"""
    if img.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if img.startswith(b"\xff\xd8\xff"):
        return True
    if img[:4] == b"RIFF" and img[8:12] == b"WEBP":
        return True
    return False


@router.post("/api/v1/campus/ocr/course-table")
async def ocr_course_table(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    """课表截图 → 候选课程字段。

    鉴权：与其它内容接口一致，要求 `Authorization: Device <token>`。
    这样匿名脚本不能把 OCR 当免费算力；客户端在请求前先 ensureDevice。
    """
    from .api import require_device

    require_device(request, authorization)
    ip = request.client.host if request.client else "-"
    if not await webproxy._rate_ok(ip):
        return webproxy._fail(429, "rate_limited", "请求过于频繁，请稍后重试")

    try:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return webproxy._fail(413, "body_too_large", "请求体过大，请压缩图片后再试")
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return webproxy._fail(400, "bad_json", "请求体不是合法 JSON")
    if not isinstance(payload, dict):
        return webproxy._fail(400, "bad_json", "请求体必须是 JSON 对象")

    raw = str(payload.get("image_b64") or "")
    if not raw:
        return webproxy._fail(400, "missing_field", "缺少 image_b64")
    if "," in raw[:64]:
        raw = raw.split(",", 1)[1]
    try:
        img = base64.b64decode(raw, validate=False)
    except Exception:  # noqa: BLE001
        return webproxy._fail(400, "bad_image", "图片不是合法的 base64")
    if not img:
        return webproxy._fail(400, "bad_image", "图片为空")
    if len(img) > MAX_IMAGE_BYTES:
        return webproxy._fail(413, "image_too_large", "图片超过 10MB，请压缩后再试")
    if not _sniff_image(img):
        return webproxy._fail(400, "bad_image", "只支持 PNG / JPEG / WEBP 图片")

    try:
        result = await asyncio.to_thread(_recognize_sync, img)
    except ValueError as e:
        if str(e) == "bad_image":
            return webproxy._fail(400, "bad_image", "图片解码失败，请换一张截图")
        return webproxy._fail(400, "bad_image", "图片解码失败，请换一张截图")
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return webproxy._fail(502, "ocr_failed", f"识别失败：{type(e).__name__}")

    courses = result["courses"]
    body: dict[str, Any] = {
        "ok": True,
        "engine": "rapidocr",
        "courses": courses,
        "blocks": result["blocks"],
        "lineCount": result["lineCount"],
    }
    if not courses:
        body["message"] = "未从图片里识别出课程，请检查截图是否完整包含课表"
    return JSONResponse(body)
