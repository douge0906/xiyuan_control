"""班级课表在线库：同学上传本班课表 → 审核 → 发布 → 别人直接导入。

设计取舍（与 `docs` 里的两份方案文档对齐，具体偏差记录在本段末尾）：

1. **不存附件**。客户端已经把自己的课表解析成结构化课程，服务端只收课程数组。
   这样磁盘上没有任何「可以越权下载的原件」，隐私面和存储面都最小。
2. **可见性是一道硬闸门**：`status='approved' AND published=1`。审核通过不等于
   可见 —— 要等 worker 把 `published` 打开。这不是多此一举，而是让「审核通过
   但发布失败」变成后台能看见、能重试的状态，而不是用户搜不到的幽灵数据。
3. **同一班级允许多份课表并存**。同名班级只保留一份会让「谁是对的」变成平台
   替用户判断；多份并列 + 一致度提示，选择权留给导入的人。
4. **上传者只填昵称**。`uploader_name` 是唯一对外可见的贡献者标识；
   `contributor` 是设备级不透明键（`sha256(installation_id)` 前 16 位），
   只用于「同一台设备同一班级同一来源再传一次时版本号 +1」和撤销归属，
   **从不出现在任何客户端可见响应里**。
5. **没有学号明文，也没有真实姓名**。课表接口用的是设备令牌，本来就不带学号；
   课程字段里若出现疑似手机号/学号串，入库时置空并打 `personal_flag` 标记，
   让审核员看得见，而不是静默改掉用户数据。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, Header, Query, Request

from . import security, timetable_jobs
from .admin import audit, require_admin
from .api import config, db, fail, require_device
from .db import DB

router = APIRouter()

# ---------------------------------------------------------------- 常量

_STATUSES = ("pending", "approved", "rejected", "removed")
_SOURCES = ("local_table", "local_file")
_REASONS = (
    "mismatch_with_campus",
    "incomplete_info",
    "duplicate",
    "personal_info",
    "wrong_class",
    "other",
)
_REASON_TEXT = {
    "mismatch_with_campus": "与教务课表差异过大",
    "incomplete_info": "课程信息不完整",
    "duplicate": "与已有课表重复",
    "personal_info": "疑似包含个人信息",
    "wrong_class": "归属班级有误",
    "other": "其它原因",
}
_VALID_XQM = ("3", "12", "16")

_SIX_PLUS_DIGITS = re.compile(r"\d{6,}")
_PHONE = re.compile(r"1[3-9]\d{9}")
# 括号里的内容（中英文全角半角）在比对课程名时一律忽略：
# 「大学物理实验Ⅱ（2）」跟「大学物理实验Ⅱ」是同一门课。
_PAREN = re.compile(r"[（(][^）)]*[）)]")
_SPACES = re.compile(r"\s+")


# ---------------------------------------------------------------- 小工具


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _class_key(xnm: str, xqm: str, jgmc: str, zymc: str, bj: str) -> str:
    """班级主键。用 `|` 分隔是因为班级名里本身可能带 `-`（如「25电气-1班」）。"""
    return f"{xnm}-{xqm}|{jgmc}|{zymc}|{bj}"


def _split_class_key(key: str) -> tuple[str, str, str, str, str] | None:
    try:
        term, rest = key.split("|", 1)
        jgmc, zymc, bj = rest.split("|", 2)
        xnm, xqm = term.split("-", 1)
    except ValueError:
        return None
    return xnm, xqm, jgmc, zymc, bj


def _norm_name(name: str) -> str:
    """课程名归一化：去空白、全角转半角、去掉括注内容。

    全角转半角只用 unicodedata 的 NFKC —— 它会把「Ⅱ」这种罗马数字也一起
    归一化，两端都走同一条路，所以比对结果仍然一致。
    """
    import unicodedata

    text = unicodedata.normalize("NFKC", _s(name))
    text = _PAREN.sub("", text)
    return _SPACES.sub("", text).strip().lower()


def _has_personal(text: str) -> bool:
    return bool(_SIX_PLUS_DIGITS.search(text)) or bool(_PHONE.search(text))


def _clean_course_text(value: Any) -> tuple[str, bool]:
    """课程里的文本字段：命中疑似个人信息就置空，并回报「命中过」。"""
    text = _s(value).strip()
    if not text:
        return "", False
    if _has_personal(text):
        return "", True
    return text, False


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _course_match_key(row: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        _norm_name(_s(row.get("name"))),
        _as_int(row.get("weekday")),
        _as_int(row.get("start_slot")),
        _as_int(row.get("end_slot")),
    )


def _course_body(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _s(row.get("name")),
        "teacher": _s(row.get("teacher")),
        "classroom": _s(row.get("classroom")),
        "weekday": _as_int(row.get("weekday")),
        "start_slot": _as_int(row.get("start_slot")),
        "end_slot": _as_int(row.get("end_slot")),
        "weeks": _s(row.get("weeks")),
    }


def _consistency(uploaded: list[dict[str, Any]],
                 baseline: list[dict[str, Any]]) -> tuple[int, int]:
    """返回 (matched, total)。

    匹配键 = (课程名归一化, weekday, start_slot, end_slot)；命中且 weeks /
    teacher / classroom 也全同才算 matched。没有基准时两者都是 0（客户端显示
    「未比对」）。
    """
    if not baseline:
        return 0, 0
    index = {_course_match_key(c): c for c in baseline}
    matched = 0
    for course in uploaded:
        hit = index.get(_course_match_key(course))
        if hit is None:
            continue
        if (
            _s(hit.get("weeks")) == _s(course.get("weeks"))
            and _s(hit.get("teacher")) == _s(course.get("teacher"))
            and _s(hit.get("classroom")) == _s(course.get("classroom"))
        ):
            matched += 1
    return matched, len(baseline)


def _diff_items(uploaded: list[dict[str, Any]],
                baseline: list[dict[str, Any]]) -> dict[str, Any]:
    """审核详情用的四态差异：same / diff / extra / missing。"""
    uploaded_keys = {_course_match_key(c) for c in uploaded}
    baseline_index = {_course_match_key(c): c for c in baseline}
    uploaded_index = {_course_match_key(c): c for c in uploaded}

    items: list[dict[str, Any]] = []
    for key, base in baseline_index.items():
        up = uploaded_index.get(key)
        if up is None:
            items.append({"name": _s(base.get("name")),
                          "status": "missing", "baseline": _course_body(base)})
            continue
        same = (
            _s(up.get("weeks")) == _s(base.get("weeks"))
            and _s(up.get("teacher")) == _s(base.get("teacher"))
            and _s(up.get("classroom")) == _s(base.get("classroom"))
        )
        items.append({
            "name": _s(base.get("name")),
            "status": "same" if same else "diff",
            "uploaded": _course_body(up),
            "baseline": _course_body(base),
        })
    for key, up in uploaded_index.items():
        if key in baseline_index:
            continue
        items.append({"name": _s(up.get("name")),
                      "status": "extra", "uploaded": _course_body(up)})
    matched = sum(1 for i in items if i["status"] == "same")
    return {"total": len(baseline), "matched": matched, "items": items}


def _contributor_of(device: dict[str, Any]) -> str:
    """设备级不透明归属键：同一台设备上传多次能认出来，但反推不到任何人。"""
    seed = _s(device.get("installation_id")) or _s(device.get("device_serial"))
    return hashlib.sha256(f"timetable|{seed}".encode()).hexdigest()[:16]


def _upload_row(row: Any) -> dict[str, Any]:
    """单条上传的对外形状（mine / 后台共用）。绝不含 contributor / device_id。"""
    status = _s(row["status"])
    published = int(row["published"] or 0)
    return {
        "id": int(row["id"]),
        "version": int(row["version"]),
        "xnm": _s(row["xnm"]),
        "xqm": _s(row["xqm"]),
        "jgmc": _s(row["jgmc"]),
        "zymc": _s(row["zymc"]),
        "bj": _s(row["bj"]),
        "class_key": _s(row["class_key"]),
        "njdm_id": _s(row["njdm_id"]),
        "source": _s(row["source"]),
        "uploader_name": _s(row["uploader_name"]),
        "course_count": int(row["course_count"] or 0),
        "status": status,
        "published": published,
        "created_at": _s(row["created_at"]),
        "reviewed_at": row["reviewed_at"],
        "published_at": row["published_at"],
        "review_reason": _s(row["review_reason"]),
        "review_reason_text": _REASON_TEXT.get(_s(row["review_reason"]), ""),
        "review_note": _s(row["review_note"]),
        "note": _s(row["note"]),
        "personal_flag": int(row["personal_flag"] or 0),
        "consistency": {
            "matched": int(row["diff_matched"] or 0),
            "total": int(row["diff_total"] or 0),
        },
    }


def _load_courses(conn: Any, upload_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT name, teacher, classroom, weekday, start_slot, end_slot, weeks
           FROM timetable_courses WHERE upload_id = ?
           ORDER BY id""",
        (upload_id,),
    ).fetchall()
    return [_course_body(dict(r)) for r in rows]


def _validate_body(body: dict[str, Any],
                   *, require_class: bool = True) -> dict[str, Any]:
    """校验上传请求体（camelCase），返回normalize 后的字段。"""
    xnm = _s(body.get("xnm")).strip()
    xqm = _s(body.get("xqm")).strip()
    if not xnm or xqm not in _VALID_XQM:
        fail(400, "invalid_term", "学年学期不正确，请回到课表页重新进入")

    jgmc = _s(body.get("jgmc")).strip()
    zymc = _s(body.get("zymc")).strip()
    bj = _s(body.get("bj")).strip()
    if require_class and (not jgmc or not zymc or not bj):
        fail(400, "invalid_class", "学院、专业、班级都要填")

    source = _s(body.get("source")).strip()
    if source not in _SOURCES:
        fail(400, "invalid_source", "来源只能是「从本机课程表导入」或「从本机导出的文件导入」")

    uploader = _s(body.get("uploaderName")).strip()
    if not uploader:
        fail(400, "invalid_uploader_name", "请填一个上传者名称")
    if len(uploader) > 16:
        fail(400, "invalid_uploader_name", "上传者名称最多 16 个字")
    if _has_personal(uploader):
        # 这里不静默替换：用户填的是他自己的学号/手机号，必须让他自己改，
        # 否则他会以为「填了也没事」。课程字段放宽到置空，是因为那是从教务
        # 导出的数据，不该因为一个教室名里的数字就整份传不上去。
        fail(400, "invalid_uploader_name",
             "上传者名称请填昵称，不要填学号、手机号这类能定位到人的信息")

    raw_courses = body.get("courses")
    if not isinstance(raw_courses, list):
        fail(400, "invalid_courses", "课程列表格式不对")
    if not raw_courses or len(raw_courses) > 200:
        fail(400, "invalid_courses", "一份课表要有 1 到 200 门课")

    courses: list[dict[str, Any]] = []
    personal_hit = False
    for item in raw_courses:
        if not isinstance(item, dict):
            fail(400, "invalid_courses", "课程列表里有一项格式不对")
        name, hit = _clean_course_text(item.get("name"))
        personal_hit = personal_hit or hit
        if not name:
            fail(400, "invalid_courses", "有课程没有名字")
        teacher, hit = _clean_course_text(item.get("teacher"))
        personal_hit = personal_hit or hit
        classroom, hit = _clean_course_text(item.get("classroom"))
        personal_hit = personal_hit or hit
        weekday = _as_int(item.get("weekday"))
        start_slot = _as_int(item.get("startSlot"))
        end_slot = _as_int(item.get("endSlot"))
        if not 1 <= weekday <= 7:
            fail(400, "invalid_courses", f"「{name}」的上课星期不对")
        if start_slot < 1:
            fail(400, "invalid_courses", f"「{name}」的起始节次不对")
        if end_slot < start_slot:
            fail(400, "invalid_courses", f"「{name}」的结束节次比开始还早")
        weeks = _s(item.get("weeks")).strip()
        courses.append({
            "name": name, "teacher": teacher, "classroom": classroom,
            "weekday": weekday, "start_slot": start_slot,
            "end_slot": end_slot, "weeks": weeks,
        })

    baseline_raw = body.get("campusBaseline")
    baseline: list[dict[str, Any]] = []
    if baseline_raw is not None:
        if not isinstance(baseline_raw, list):
            fail(400, "invalid_courses", "教务基准格式不对")
        if len(baseline_raw) > 200:
            fail(400, "invalid_courses", "教务基准课程太多")
        for item in baseline_raw:
            if not isinstance(item, dict):
                continue
            baseline.append({
                "name": _s(item.get("name")).strip(),
                "teacher": _s(item.get("teacher")).strip(),
                "classroom": _s(item.get("classroom")).strip(),
                "weekday": _as_int(item.get("weekday")),
                "start_slot": _as_int(item.get("startSlot")),
                "end_slot": _as_int(item.get("endSlot")),
                "weeks": _s(item.get("weeks")).strip(),
            })

    note = _s(body.get("note")).strip()
    if len(note) > 500:
        # 选「截断」而不是 400：备注是补充说明，不是必填字段，
        # 因为写多了就整份传不上去，用户只会觉得莫名其妙。
        note = note[:500]

    return {
        "xnm": xnm, "xqm": xqm,
        "jgmc": jgmc, "zymc": zymc, "bj": bj,
        "njdm_id": _s(body.get("njdmId")).strip(),
        "source": source,
        "uploader_name": uploader,
        "courses": courses,
        "note": note,
        "campus_baseline": baseline,
        "personal_flag": 1 if personal_hit else 0,
    }


def _write_courses(conn: Any, upload_id: int,
                   courses: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM timetable_courses WHERE upload_id = ?", (upload_id,))
    conn.executemany(
        """INSERT INTO timetable_courses
           (upload_id, name, teacher, classroom, weekday,
            start_slot, end_slot, weeks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (upload_id, c["name"], c["teacher"], c["classroom"], c["weekday"],
             c["start_slot"], c["end_slot"], c["weeks"])
            for c in courses
        ],
    )


def _class_info(conn: Any, class_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT xnm, xqm, jgmc, zymc, bj, njdm_id
           FROM timetable_uploads WHERE class_key = ?
           ORDER BY id DESC LIMIT 1""",
        (class_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "xnm": _s(row["xnm"]), "xqm": _s(row["xqm"]),
        "jgmc": _s(row["jgmc"]), "zymc": _s(row["zymc"]),
        "bj": _s(row["bj"]), "njdm_id": _s(row["njdm_id"]),
    }


# ---------------------------------------------------------------- 客户端端点


@router.post("/api/v1/timetables/search")
async def search_timetables(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """按学年学期 + 可选筛选检索班级。

    keyword 走 SQL LIKE 兜底（班名/专业/学院），但客户端本来就会本地再过滤一遍
    —— 这里的 LIKE 只是为了让「只搜一个词」也能拿到结果。
    """
    require_device(request, authorization)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        fail(400, "invalid_json", "请求体不是合法 JSON")

    xnm = _s(body.get("xnm")).strip()
    xqm = _s(body.get("xqm")).strip()
    if not xnm or xqm not in _VALID_XQM:
        fail(400, "invalid_term", "请先选学年和学期")

    jgmc = _s(body.get("jgmc")).strip()
    zymc = _s(body.get("zymc")).strip()
    bj = _s(body.get("bj")).strip()
    keyword = _s(body.get("keyword")).strip()

    sql = [
        """SELECT u.class_key, u.jgmc, u.zymc, u.bj, u.njdm_id,
                  COUNT(*) AS timetable_count,
                  MAX(u.course_count) AS course_count,
                  MAX(u.published_at) AS latest_uploaded_at,
                  MAX(u.diff_matched) AS diff_matched,
                  MAX(u.diff_total) AS diff_total
           FROM timetable_uploads u
           WHERE u.xnm = ? AND u.xqm = ?
             AND u.status = 'approved' AND u.published = 1"""
    ]
    params: list[Any] = [xnm, xqm]
    if jgmc:
        sql.append("AND u.jgmc = ?")
        params.append(jgmc)
    if zymc:
        sql.append("AND u.zymc = ?")
        params.append(zymc)
    if bj:
        sql.append("AND u.bj = ?")
        params.append(bj)
    if keyword:
        sql.append("AND (u.bj LIKE ? OR u.zymc LIKE ? OR u.jgmc LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    sql.append("GROUP BY u.class_key ORDER BY u.bj")

    with db(request) as conn:
        rows = conn.execute(" ".join(sql), tuple(params)).fetchall()
        classes: list[dict[str, Any]] = []
        for row in rows:
            class_key = _s(row["class_key"])
            # 最新一份上传者的名字：按 published_at 倒序取第一条。
            latest = conn.execute(
                """SELECT uploader_name FROM timetable_uploads
                   WHERE class_key = ? AND status = 'approved' AND published = 1
                   ORDER BY COALESCE(published_at, created_at) DESC, id DESC
                   LIMIT 1""",
                (class_key,),
            ).fetchone()
            classes.append({
                "class_key": class_key,
                "bj": _s(row["bj"]),
                "zymc": _s(row["zymc"]),
                "jgmc": _s(row["jgmc"]),
                "njdm_id": _s(row["njdm_id"]),
                "timetable_count": int(row["timetable_count"] or 0),
                "course_count": int(row["course_count"] or 0),
                "latest_uploaded_at": row["latest_uploaded_at"],
                "latest_uploader_name": _s(latest["uploader_name"]) if latest else "",
                "consistency": {
                    "matched": int(row["diff_matched"] or 0),
                    "total": int(row["diff_total"] or 0),
                },
            })

    return {"success": True, "data": {
        "xnm": xnm, "xqm": xqm,
        "total": len(classes), "count": len(classes),
        "classes": classes,
    }}


@router.get("/api/v1/timetables/class/{class_key:path}")
def class_timetables(
    request: Request,
    class_key: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """某班级下全部**已发布**课表，按提交时间倒序。"""
    require_device(request, authorization)
    with db(request) as conn:
        info = _class_info(conn, class_key)
        if info is None:
            fail(404, "not_found", "没有找到这个班级")
        rows = conn.execute(
            """SELECT * FROM timetable_uploads
               WHERE class_key = ? AND status = 'approved' AND published = 1
               ORDER BY COALESCE(published_at, created_at) DESC, id DESC""",
            (class_key,),
        ).fetchall()
        timetables: list[dict[str, Any]] = []
        for row in rows:
            item = _upload_row(row)
            item["courses"] = _load_courses(conn, int(row["id"]))
            timetables.append(item)

    data = {"class_key": class_key}
    data.update(info)
    data["timetable_count"] = len(timetables)
    data["timetables"] = timetables
    return {"success": True, "data": data}


@router.post("/api/v1/timetables/uploads")
async def create_upload(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """提交一份课表，固定进 pending、published=0。"""
    device = require_device(request, authorization)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        fail(400, "invalid_json", "请求体不是合法 JSON")

    data = _validate_body(body)
    class_key = _class_key(data["xnm"], data["xqm"], data["jgmc"],
                           data["zymc"], data["bj"])
    contributor = _contributor_of(device)
    matched, total = _consistency(data["courses"], data["campus_baseline"])
    stamp = security.iso()

    with db(request) as conn:
        # 版本号只在「同设备 + 同班 + 同来源」这条线上递增；
        # 换一台设备或换一种来源就是新的一条线，从 1 开始。
        row = conn.execute(
            """SELECT MAX(version) AS v FROM timetable_uploads
               WHERE contributor = ? AND class_key = ? AND source = ?""",
            (contributor, class_key, data["source"]),
        ).fetchone()
        version = int(row["v"] or 0) + 1

        cur = conn.execute(
            """INSERT INTO timetable_uploads
               (device_id, contributor, uploader_name, class_key,
                xnm, xqm, jgmc, zymc, bj, njdm_id, source, version,
                course_count, diff_matched, diff_total, note,
                campus_baseline, personal_flag, status, published,
                review_note, review_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       'pending', 0, '', '', ?)""",
            (
                int(device["id"]), contributor, data["uploader_name"], class_key,
                data["xnm"], data["xqm"], data["jgmc"], data["zymc"], data["bj"],
                data["njdm_id"], data["source"], version,
                len(data["courses"]), matched, total, data["note"],
                json.dumps(data["campus_baseline"], ensure_ascii=False)
                if data["campus_baseline"] else "",
                data["personal_flag"], stamp,
            ),
        )
        upload_id = int(cur.lastrowid)
        _write_courses(conn, upload_id, data["courses"])
        if data["personal_flag"]:
            # 只记「这次上传里有疑似个人信息」，不把命中的原文写进审计日志
            # （写进去就等于把这些信息又存了一份）。
            audit(conn, "timetable_personal_flagged", f"upload_id={upload_id}")

    return {"success": True, "data": {
        "id": upload_id,
        "version": version,
        "status": "pending",
        "created_at": stamp,
        "personal_flag": data["personal_flag"],
        "consistency": {"matched": matched, "total": total},
    }}


@router.get("/api/v1/timetables/uploads/mine")
def my_uploads(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """当前设备提交过的全部课表，**含未发布**（与班级详情页相反）。"""
    device = require_device(request, authorization)
    contributor = _contributor_of(device)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT * FROM timetable_uploads
               WHERE contributor = ? ORDER BY id DESC LIMIT 200""",
            (contributor,),
        ).fetchall()
        uploads = [_upload_row(r) for r in rows]
    return {"success": True, "data": {
        "count": len(uploads), "uploads": uploads,
    }}


@router.delete("/api/v1/timetables/uploads/{upload_id}")
def revoke_upload(
    request: Request,
    upload_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """撤销自己**待审核**的上传。已审核的不能撤（要走下架）。"""
    device = require_device(request, authorization)
    contributor = _contributor_of(device)
    with db(request) as conn:
        row = conn.execute(
            """SELECT id, contributor, status FROM timetable_uploads
               WHERE id = ?""",
            (upload_id,),
        ).fetchone()
        if row is None or _s(row["contributor"]) != contributor:
            fail(404, "not_found", "没有找到这条上传记录")
        if _s(row["status"]) != "pending":
            fail(400, "cannot_revoke", "只有待审核的上传可以撤销")
        conn.execute("DELETE FROM timetable_courses WHERE upload_id = ?",
                     (upload_id,))
        conn.execute("DELETE FROM timetable_uploads WHERE id = ?", (upload_id,))
    return {"success": True, "data": {"id": upload_id, "deleted": True}}


@router.post("/api/v1/timetables/uploads/{upload_id}/resubmit")
async def resubmit_upload(
    request: Request,
    upload_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """驳回后按意见改完重提：**同一行**，version+1，回到 pending。"""
    device = require_device(request, authorization)
    contributor = _contributor_of(device)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        fail(400, "invalid_json", "请求体不是合法 JSON")

    data = _validate_body(body)
    matched, total = _consistency(data["courses"], data["campus_baseline"])
    stamp = security.iso()

    with db(request) as conn:
        row = conn.execute(
            """SELECT id, contributor, status FROM timetable_uploads
               WHERE id = ?""",
            (upload_id,),
        ).fetchone()
        if row is None or _s(row["contributor"]) != contributor:
            fail(404, "not_found", "没有找到这条上传记录")
        if _s(row["status"]) != "rejected":
            fail(400, "invalid_status", "只有被驳回的课表可以修改后重提")

        conn.execute(
            """UPDATE timetable_uploads
               SET uploader_name = ?, jgmc = ?, zymc = ?, bj = ?, njdm_id = ?,
                   note = ?, diff_matched = ?, diff_total = ?,
                   campus_baseline = ?, personal_flag = ?,
                   version = version + 1, status = 'pending', published = 0,
                   review_note = '', review_reason = '', reviewed_at = NULL,
                   published_at = NULL
               WHERE id = ?""",
            (
                data["uploader_name"], data["jgmc"], data["zymc"], data["bj"],
                data["njdm_id"], data["note"], matched, total,
                json.dumps(data["campus_baseline"], ensure_ascii=False)
                if data["campus_baseline"] else "",
                data["personal_flag"], upload_id,
            ),
        )
        _write_courses(conn, upload_id, data["courses"])
        version = int(conn.execute(
            "SELECT version FROM timetable_uploads WHERE id = ?", (upload_id,)
        ).fetchone()["version"])

    return {"success": True, "data": {
        "id": upload_id, "version": version, "status": "pending",
        "created_at": stamp,
    }}


@router.get("/api/v1/timetables/favorites")
def list_favorites(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = require_device(request, authorization)
    with db(request) as conn:
        rows = conn.execute(
            """SELECT f.class_key, f.created_at,
                      u.bj, u.jgmc, u.zymc, u.xnm, u.xqm
               FROM timetable_favorites f
               LEFT JOIN timetable_uploads u ON u.class_key = f.class_key
               WHERE f.device_id = ?
               GROUP BY f.class_key
               ORDER BY f.created_at DESC""",
            (int(device["id"]),),
        ).fetchall()
        favorites = [{
            "class_key": _s(r["class_key"]),
            "bj": _s(r["bj"]), "jgmc": _s(r["jgmc"]), "zymc": _s(r["zymc"]),
            "xnm": _s(r["xnm"]), "xqm": _s(r["xqm"]),
            "created_at": _s(r["created_at"]),
        } for r in rows]
    return {"success": True, "data": {
        "count": len(favorites), "favorites": favorites,
    }}


@router.post("/api/v1/timetables/favorites/{class_key:path}")
def add_favorite(
    request: Request,
    class_key: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = require_device(request, authorization)
    with db(request) as conn:
        if _class_info(conn, class_key) is None:
            fail(404, "not_found", "没有找到这个班级")
        conn.execute(
            """INSERT OR IGNORE INTO timetable_favorites
               (device_id, class_key, created_at) VALUES (?, ?, ?)""",
            (int(device["id"]), class_key, security.iso()),
        )
    return {"success": True, "data": {"class_key": class_key, "favorited": True}}


@router.delete("/api/v1/timetables/favorites/{class_key:path}")
def remove_favorite(
    request: Request,
    class_key: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = require_device(request, authorization)
    with db(request) as conn:
        conn.execute(
            "DELETE FROM timetable_favorites WHERE device_id = ? AND class_key = ?",
            (int(device["id"]), class_key),
        )
    return {"success": True, "data": {"class_key": class_key, "favorited": False}}


# ---------------------------------------------------------------- 后台端点
#
# 注意顺序：`/admin/timetables/queue`、`/admin/timetables/classes` 必须写在
# `/admin/timetables/{upload_id}` 之前，否则 FastAPI 会把 "queue" 当成
# upload_id 去解析，直接 422。


@router.get("/admin/timetables")
def admin_list(
    request: Request,
    authorization: str | None = Header(default=None),
    status: str = Query(default=""),
    xnm: str = Query(default=""),
    xqm: str = Query(default=""),
    jgmc: str = Query(default=""),
    keyword: str = Query(default=""),
) -> dict[str, Any]:
    require_admin(request, authorization)
    if status and status not in _STATUSES:
        fail(400, "invalid_status",
             "状态只能是 pending / approved / rejected / removed")

    sql = ["""SELECT * FROM timetable_uploads WHERE 1 = 1"""]
    params: list[Any] = []
    if status:
        sql.append("AND status = ?")
        params.append(status)
    if xnm:
        sql.append("AND xnm = ?")
        params.append(xnm)
    if xqm:
        sql.append("AND xqm = ?")
        params.append(xqm)
    if jgmc:
        sql.append("AND jgmc = ?")
        params.append(jgmc)
    if keyword:
        sql.append("AND (bj LIKE ? OR zymc LIKE ? OR jgmc LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    sql.append("ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC LIMIT 500")

    with db(request) as conn:
        rows = conn.execute(" ".join(sql), tuple(params)).fetchall()
        items = [_upload_row(r) for r in rows]
    return {"success": True, "data": {"count": len(items), "items": items}}


@router.get("/admin/timetables/queue")
def admin_queue(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        counts = timetable_jobs.counts(conn)
        jobs = timetable_jobs.recent(conn, limit=50)
    return {"success": True, "data": {
        "pending": counts["pending"],
        "running": counts["running"],
        "done": counts["done"],
        "failed": counts["failed"],
        "items": jobs,
    }}


@router.post("/admin/timetables/queue/{job_id}/retry")
def admin_queue_retry(
    request: Request,
    job_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    stamp = security.iso()
    with db(request) as conn:
        row = conn.execute(
            "SELECT status FROM timetable_publish_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            fail(404, "not_found", "没有找到这个任务")
        if _s(row["status"]) != "failed":
            fail(400, "invalid_job_status", "只有失败的任务可以重试")
        # last_error 保留：重试成功之后它就是「上一次为什么失败」的唯一线索。
        conn.execute(
            """UPDATE timetable_publish_jobs
               SET status = 'pending', attempts = 0, updated_at = ?, next_run_at = ?
               WHERE id = ?""",
            (stamp, stamp, job_id),
        )
        audit(conn, "timetable_job_retry", f"job_id={job_id}")
    return {"success": True, "data": {"id": job_id, "status": "pending"}}


@router.get("/admin/timetables/classes")
def admin_classes(
    request: Request,
    authorization: str | None = Header(default=None),
    xnm: str = Query(default=""),
    xqm: str = Query(default=""),
    keyword: str = Query(default=""),
) -> dict[str, Any]:
    """班级维度聚合：跨上传者、**含未发布**，用来查重。"""
    require_admin(request, authorization)
    sql = ["SELECT * FROM timetable_uploads WHERE 1 = 1"]
    params: list[Any] = []
    if xnm:
        sql.append("AND xnm = ?")
        params.append(xnm)
    if xqm:
        sql.append("AND xqm = ?")
        params.append(xqm)
    if keyword:
        sql.append("AND (bj LIKE ? OR zymc LIKE ? OR jgmc LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    sql.append("ORDER BY class_key, id DESC")

    with db(request) as conn:
        rows = conn.execute(" ".join(sql), tuple(params)).fetchall()
        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = _s(r["class_key"])
            bucket = buckets.setdefault(key, {
                "class_key": key,
                "xnm": _s(r["xnm"]), "xqm": _s(r["xqm"]),
                "jgmc": _s(r["jgmc"]), "zymc": _s(r["zymc"]),
                "bj": _s(r["bj"]), "njdm_id": _s(r["njdm_id"]),
                "upload_count": 0, "published_count": 0, "versions": [],
            })
            bucket["upload_count"] += 1
            if int(r["published"] or 0) == 1:
                bucket["published_count"] += 1
            bucket["versions"].append({
                "id": int(r["id"]),
                "version": int(r["version"]),
                "status": _s(r["status"]),
                "published": int(r["published"] or 0),
                "uploader_name": _s(r["uploader_name"]),
                "created_at": _s(r["created_at"]),
            })
    items = list(buckets.values())
    return {"success": True, "data": {"count": len(items), "classes": items}}


@router.get("/admin/timetables/{upload_id}")
def admin_detail(
    request: Request,
    upload_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    with db(request) as conn:
        row = conn.execute(
            "SELECT * FROM timetable_uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        if row is None:
            fail(404, "not_found", "没有找到这条上传记录")
        courses = _load_courses(conn, upload_id)
        try:
            baseline = json.loads(_s(row["campus_baseline"]) or "[]")
        except json.JSONDecodeError:
            baseline = []
        if not isinstance(baseline, list):
            baseline = []
        diff = _diff_items(courses, baseline) if baseline else {
            "total": 0, "matched": 0, "items": [],
        }
        upload = _upload_row(row)
        # 后台能看到「同一台设备还传了哪些」，但看到的是不透明键，不是学号。
        siblings = conn.execute(
            """SELECT COUNT(*) AS n FROM timetable_uploads
               WHERE contributor = ? AND class_key = ?""",
            (_s(row["contributor"]), _s(row["class_key"])),
        ).fetchone()

    return {"success": True, "data": {
        "upload": upload,
        "courses": courses,
        "campus_baseline": baseline,
        "diff": diff,
        "same_submitter_uploads": int(siblings["n"] or 0),
    }}


@router.patch("/admin/timetables/{upload_id}/status")
async def admin_set_status(
    request: Request,
    upload_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(request, authorization)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        fail(400, "invalid_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        fail(400, "invalid_json", "请求体不是合法 JSON")

    status = _s(body.get("status")).strip()
    if status not in ("approved", "rejected", "removed"):
        fail(400, "invalid_status", "状态只能是 approved / rejected / removed")
    review_note = _s(body.get("reviewNote")).strip()[:500]
    reason = _s(body.get("reason")).strip()
    if status == "rejected":
        if reason not in _REASONS:
            fail(400, "invalid_reason",
                 "驳回原因只能是：" + " / ".join(_REASONS))
    else:
        reason = ""

    stamp = security.iso()
    job_id: int | None = None
    with db(request) as conn:
        row = conn.execute(
            "SELECT id, status, published FROM timetable_uploads WHERE id = ?",
            (upload_id,),
        ).fetchone()
        if row is None:
            fail(404, "not_found", "没有找到这条上传记录")
        was_published = int(row["published"] or 0) == 1

        if status == "approved":
            conn.execute(
                """UPDATE timetable_uploads
                   SET status = 'approved', review_note = ?, review_reason = '',
                       reviewed_at = ?
                   WHERE id = ?""",
                (review_note, stamp, upload_id),
            )
            # 通过不等于可见：交给 worker 打开闸门，失败会在队列里留痕。
            job_id = timetable_jobs.enqueue_publish(conn, upload_id)
        elif status == "rejected":
            conn.execute(
                """UPDATE timetable_uploads
                   SET status = 'rejected', review_note = ?, review_reason = ?,
                       published = 0, reviewed_at = ?
                   WHERE id = ?""",
                (review_note, reason, stamp, upload_id),
            )
        else:  # removed
            conn.execute(
                """UPDATE timetable_uploads
                   SET status = 'removed', review_note = ?, published = 0,
                       reviewed_at = ?
                   WHERE id = ?""",
                (review_note, stamp, upload_id),
            )
            if was_published:
                job_id = timetable_jobs.enqueue_unpublish(conn, upload_id)

        audit(conn, f"timetable_{status}",
              f"upload_id={upload_id} status={status} reason={reason} "
              f"note={review_note}")
        published = int(conn.execute(
            "SELECT published FROM timetable_uploads WHERE id = ?", (upload_id,)
        ).fetchone()["published"] or 0)

    return {"success": True, "data": {
        "id": upload_id,
        "status": status,
        "published": bool(published),
        "job_id": job_id,
        "queued": job_id is not None,
    }}
