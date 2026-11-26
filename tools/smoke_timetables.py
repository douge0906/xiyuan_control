"""班级课表在线库冒烟：独立临时数据库，不碰线上数据。

覆盖 `服务端新增实现提示词_班级课表上传与导入_2026-11-25.md` §10 的验收清单：
建表幂等、上传六条、可见性闸门四条、审核与发布九条、收藏两条、一致度两条、收尾三条。

跑法：`.venv/Scripts/python.exe tools/smoke_timetables.py`
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import security  # noqa: E402
from app import timetable_worker  # noqa: E402
from app.config import Config  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def drain(dbx) -> int:
    """把队列排空（worker 一次只领一条，测试里必须循环到空）。"""
    n = 0
    while timetable_worker.run_once(dbx):
        n += 1
        if n > 50:  # 防御：真出现死循环要炸出来，不能挂住
            raise RuntimeError("worker 队列排不空")
    return n


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK  ] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {detail}")


def device_row(serial: str, token: str, cfg: Config) -> dict:
    """插一台设备并返回它的 headers。"""
    now = security.iso()
    with dbmod.connect(cfg.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO devices
               (installation_id, public_key, platform, app_version,
                device_serial, status, created_at, last_seen)
               VALUES (?, 'test-key', 'android', '1.0.10', ?, 'active', ?, ?)""",
            (serial, serial, now, now),
        )
        did = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO device_tokens (device_id, token_hash, created_at)
               VALUES (?, ?, ?)""",
            (did, security.token_hash(token, cfg.token_secret), now),
        )
    return {"Authorization": f"Device {token}"}


COURSES = [
    {"name": "大学物理实验Ⅱ", "teacher": "朱弘桢", "classroom": "C2",
     "weekday": 3, "startSlot": 3, "endSlot": 4, "weeks": "1-16周"},
    {"name": "数字电子技术", "teacher": "王老师", "classroom": "B1-201",
     "weekday": 3, "startSlot": 9, "endSlot": 10, "weeks": "3-15周"},
]


def upload_body(**over) -> dict:
    body = {
        "xnm": "2026", "xqm": "3",
        "jgmc": "智能工程学院", "zymc": "电气工程及其自动化", "bj": "25电气1班",
        "njdmId": "2025", "source": "local_table", "uploaderName": "张同学",
        "courses": [dict(c) for c in COURSES], "note": "",
    }
    body.update(over)
    return body


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="xiyuan-tt-"))
    cfg = Config(root=root)
    dbmod.init(cfg.db_path)
    dbmod.init(cfg.db_path)  # 幂等：第二遍不应报错
    app.state.config = cfg
    app.state.db = dbmod.DB(cfg.db_path)

    client = TestClient(app)
    dev_a = device_row("TEST-TT-AAAA", "tt-token-a", cfg)
    dev_b = device_row("TEST-TT-BBBB", "tt-token-b", cfg)
    admin = {"Authorization": f"Bearer {cfg.admin_key}"}
    KEY = "2026-3|智能工程学院|电气工程及其自动化|25电气1班"

    print("== 基础 ==")
    with dbmod.connect(cfg.db_path) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    check("四张表已建好",
          {"timetable_uploads", "timetable_courses",
           "timetable_favorites", "timetable_publish_jobs"} <= names)
    r = client.get("/admin", headers=admin)
    check("GET /admin 正常（既有后台不受影响）", r.status_code == 200,
          f"status={r.status_code}")

    print("== 上传 ==")
    r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                    json=upload_body())
    check("合法上传 200", r.status_code == 200, r.text[:200])
    d = r.json()["data"]
    uid = d["id"]
    check("返回 pending / version 1",
          d["status"] == "pending" and d["version"] == 1, str(d))
    with dbmod.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT published, course_count FROM timetable_uploads WHERE id=?",
            (uid,)).fetchone()
    check("published=0 且课程数落库", row["published"] == 0
          and row["course_count"] == 2, str(dict(row)))

    r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                    json=upload_body(source="pdf"))
    check("source='pdf' → 400 invalid_source",
          r.status_code == 400
          and r.json()["error"]["code"] == "invalid_source", r.text[:200])

    for name, value in [
        ("空", ""),
        ("17 字", "一" * 17),
        ("手机号", "13812345678"),
        ("8 位数字", "20192335"),
    ]:
        r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                        json=upload_body(uploaderName=value))
        check(f"uploaderName {name} → 400",
              r.status_code == 400
              and r.json()["error"]["code"] == "invalid_uploader_name",
              r.text[:160])

    r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                    json=upload_body(courses=[]))
    check("courses 0 条 → 400 invalid_courses",
          r.status_code == 400
          and r.json()["error"]["code"] == "invalid_courses", r.text[:160])
    many = [dict(COURSES[0], name=f"课{i}") for i in range(201)]
    r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                    json=upload_body(courses=many))
    check("courses 201 条 → 400 invalid_courses",
          r.status_code == 400
          and r.json()["error"]["code"] == "invalid_courses", r.text[:160])

    r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                    json=upload_body())
    check("同设备同班同来源再传 → version 2",
          r.status_code == 200 and r.json()["data"]["version"] == 2, r.text[:200])
    r = client.post("/api/v1/timetables/uploads", headers=dev_b,
                    json=upload_body(uploaderName="李同学"))
    check("另一台设备同班 → version 1",
          r.status_code == 200 and r.json()["data"]["version"] == 1, r.text[:200])
    with dbmod.connect(cfg.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM timetable_uploads WHERE class_key=?",
            (KEY,)).fetchone()["n"]
    check("旧行仍并存（三条）", n == 3, f"n={n}")

    print("== 可见性闸门 ==")
    r = client.post("/api/v1/timetables/search", headers=dev_a,
                    json={"xnm": "2026", "xqm": "3"})
    check("pending 搜不到", r.json()["data"]["count"] == 0, r.text[:200])

    with dbmod.connect(cfg.db_path) as conn:
        conn.execute("UPDATE timetable_uploads SET status='approved', "
                     "published=0 WHERE id=?", (uid,))
    r = client.post("/api/v1/timetables/search", headers=dev_a,
                    json={"xnm": "2026", "xqm": "3"})
    check("approved 但未发布 → 搜不到", r.json()["data"]["count"] == 0,
          r.text[:200])
    r = client.get(f"/api/v1/timetables/class/{KEY}", headers=dev_a)
    check("approved 但未发布 → 班级详情无版本",
          r.json()["data"]["timetable_count"] == 0, r.text[:200])

    # worker 把闸门打开
    dbx = dbmod.DB(cfg.db_path)
    with dbmod.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO timetable_publish_jobs
               (upload_id, action, status, attempts, last_error,
                created_at, updated_at, next_run_at)
               VALUES (?, 'publish', 'pending', 0, '', ?, ?, ?)""",
            (uid, security.iso(), security.iso(), security.iso()))
    worked = timetable_worker.run_once(dbx)
    check("worker 消费了任务", worked)
    with dbmod.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT published, published_at FROM timetable_uploads WHERE id=?",
            (uid,)).fetchone()
    check("worker 发布后 published=1 且有 published_at",
          row["published"] == 1 and bool(row["published_at"]), str(dict(row)))
    r = client.post("/api/v1/timetables/search", headers=dev_a,
                    json={"xnm": "2026", "xqm": "3"})
    check("发布后搜得到", r.json()["data"]["count"] == 1, r.text[:200])
    cls = r.json()["data"]["classes"][0]
    check("search 返回必要字段",
          cls["class_key"] == KEY and cls["timetable_count"] == 1
          and cls["latest_uploader_name"] == "张同学", str(cls))

    r = client.get(f"/api/v1/timetables/class/{KEY}", headers=dev_a)
    data = r.json()["data"]
    check("班级详情含 courses 7 键 snake_case",
          data["timetable_count"] == 1
          and set(data["timetables"][0]["courses"][0]) == {
              "name", "teacher", "classroom", "weekday",
              "start_slot", "end_slot", "weeks"},
          str(data["timetables"][0]["courses"][0] if data["timetables"] else None))
    check("客户端响应里没有 contributor / device_id",
          "contributor" not in r.text and "device_id" not in r.text)

    print("== 审核与发布 ==")
    r = client.get("/admin/timetables?status=bogus", headers=admin)
    check("非法 status → 400 invalid_status",
          r.status_code == 400
          and r.json()["error"]["code"] == "invalid_status", r.text[:160])
    r = client.get("/admin/timetables?status=pending", headers=admin)
    check("pending 过滤正确", r.status_code == 200
          and all(i["status"] == "pending" for i in r.json()["data"]["items"]),
          r.text[:200])

    r = client.patch(f"/admin/timetables/{uid}/status", headers=admin,
                     json={"status": "approved"})
    check("approved → 返回 job_id 且 queued",
          r.status_code == 200 and r.json()["data"]["queued"] is True,
          r.text[:200])
    with dbmod.connect(cfg.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action='timetable_approved'"
        ).fetchone()["n"]
    check("写入了 audit_logs", n >= 1, f"n={n}")

    target = None
    with dbmod.connect(cfg.db_path) as conn:
        target = conn.execute(
            "SELECT id FROM timetable_uploads WHERE status='pending' LIMIT 1"
        ).fetchone()["id"]
    r = client.patch(f"/admin/timetables/{target}/status", headers=admin,
                     json={"status": "rejected"})
    check("驳回不带 reason → 400 invalid_reason",
          r.status_code == 400
          and r.json()["error"]["code"] == "invalid_reason", r.text[:160])
    long_note = "很" * 600
    r = client.patch(f"/admin/timetables/{target}/status", headers=admin,
                     json={"status": "rejected", "reason": "duplicate",
                           "reviewNote": long_note})
    check("带合法 reason 驳回成功", r.status_code == 200, r.text[:200])
    with dbmod.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT status, review_reason, review_note, published "
            "FROM timetable_uploads WHERE id=?", (target,)).fetchone()
    check("review_reason 落库、published=0",
          row["status"] == "rejected" and row["review_reason"] == "duplicate"
          and row["published"] == 0, str(dict(row)))
    check("reviewNote 截到 500 字", len(row["review_note"]) == 500,
          f"len={len(row['review_note'])}")

    r = client.delete(f"/api/v1/timetables/uploads/{uid}", headers=dev_a)
    check("DELETE 一个 approved → 400 cannot_revoke",
          r.status_code == 400
          and r.json()["error"]["code"] == "cannot_revoke", r.text[:160])

    with dbmod.connect(cfg.db_path) as conn:
        before_version = conn.execute(
            "SELECT version FROM timetable_uploads WHERE id=?", (target,)
        ).fetchone()["version"]
    r = client.post(f"/api/v1/timetables/uploads/{target}/resubmit",
                    headers=dev_a, json=upload_body())
    check("resubmit 返回同 id / version+1 / pending",
          r.status_code == 200 and r.json()["data"]["id"] == target
          and r.json()["data"]["version"] == before_version + 1
          and r.json()["data"]["status"] == "pending", r.text[:200])
    with dbmod.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT review_note, review_reason, reviewed_at "
            "FROM timetable_uploads WHERE id=?", (target,)).fetchone()
    check("resubmit 清空了驳回信息",
          row["review_note"] == "" and row["review_reason"] == ""
          and row["reviewed_at"] is None, str(dict(row)))

    # 撤销 pending
    with dbmod.connect(cfg.db_path) as conn:
        conn.execute("UPDATE timetable_uploads SET status='pending' WHERE id=?",
                     (target,))
    r = client.delete(f"/api/v1/timetables/uploads/{target}", headers=dev_a)
    check("撤销 pending 成功", r.status_code == 200, r.text[:200])
    with dbmod.connect(cfg.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM timetable_courses WHERE upload_id=?",
            (target,)).fetchone()["n"]
    check("撤销时课程明细一并删除", n == 0, f"n={n}")

    # 下架已发布的
    r = client.patch(f"/admin/timetables/{uid}/status", headers=admin,
                     json={"status": "removed"})
    check("removed → 生成 unpublish job",
          r.status_code == 200 and r.json()["data"]["queued"] is True,
          r.text[:200])
    drain(dbx)
    with dbmod.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT published FROM timetable_uploads WHERE id=?", (uid,)
        ).fetchone()
    check("unpublish 执行后 published=0", row["published"] == 0, str(dict(row)))
    r = client.post("/api/v1/timetables/search", headers=dev_a,
                    json={"xnm": "2026", "xqm": "3"})
    check("下架后搜不到", r.json()["data"]["count"] == 0, r.text[:200])

    print("== 失败重试 ==")
    with dbmod.connect(cfg.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO timetable_publish_jobs
               (upload_id, action, status, attempts, last_error,
                created_at, updated_at, next_run_at)
               VALUES (999999, 'publish', 'pending', 0, '', ?, ?, ?)""",
            (security.iso(), security.iso(), security.iso()))
        bad_job = int(cur.lastrowid)
    drain(dbx)  # upload 不存在 → 直接 done
    with dbmod.connect(cfg.db_path) as conn:
        st = conn.execute(
            "SELECT status FROM timetable_publish_jobs WHERE id=?", (bad_job,)
        ).fetchone()["status"]
    check("upload 不存在时置 done 而不是死循环", st == "done", st)

    with dbmod.connect(cfg.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO timetable_publish_jobs
               (upload_id, action, status, attempts, last_error,
                created_at, updated_at, next_run_at)
               VALUES (?, 'publish', 'failed', 6, 'boom', ?, ?, ?)""",
            (uid, security.iso(), security.iso(), security.iso()))
        failed_job = int(cur.lastrowid)
    r = client.post(f"/admin/timetables/queue/{failed_job}/retry", headers=admin)
    check("retry 失败任务 → pending", r.status_code == 200, r.text[:200])
    with dbmod.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT status, attempts, last_error FROM timetable_publish_jobs "
            "WHERE id=?", (failed_job,)).fetchone()
    check("retry 把 attempts 归零、保留 last_error",
          row["status"] == "pending" and row["attempts"] == 0
          and row["last_error"] == "boom", str(dict(row)))
    r = client.post(f"/admin/timetables/queue/{failed_job}/retry", headers=admin)
    check("重试非 failed 任务 → 400",
          r.status_code == 400
          and r.json()["error"]["code"] == "invalid_job_status", r.text[:160])
    r = client.get("/admin/timetables/queue", headers=admin)
    q = r.json()["data"]
    check("队列端点含四类计数与任务列表",
          all(k in q for k in ("pending", "running", "done", "failed", "items")),
          str(list(q)))

    print("== 收藏 ==")
    r = client.post(f"/api/v1/timetables/favorites/{KEY}", headers=dev_a)
    check("收藏成功", r.status_code == 200, r.text[:160])
    r = client.get("/api/v1/timetables/favorites", headers=dev_a)
    check("收藏列表里有该 class_key",
          r.json()["data"]["count"] == 1
          and r.json()["data"]["favorites"][0]["class_key"] == KEY, r.text[:200])
    r = client.delete(f"/api/v1/timetables/favorites/{KEY}", headers=dev_a)
    r = client.get("/api/v1/timetables/favorites", headers=dev_a)
    check("取消收藏后消失", r.json()["data"]["count"] == 0, r.text[:200])
    r = client.post("/api/v1/timetables/favorites/2026-3|不存在|不存在|不存在",
                    headers=dev_a)
    check("收藏不存在的班级 → 404",
          r.status_code == 404, r.text[:160])

    print("== 一致度 ==")
    baseline = [dict(c) for c in COURSES]
    # 匹配键含 weekday/start_slot/end_slot，所以「换节次」在四态里是 extra+missing。
    # 要覆盖 diff 这一态，得改同一节次里的 teacher（键不变、字段变）。
    up = [dict(COURSES[0]), dict(COURSES[1], teacher="李老师")]
    body = upload_body(courses=up, campusBaseline=baseline,
                       bj="25电气2班", uploaderName="赵同学")
    r = client.post("/api/v1/timetables/uploads", headers=dev_a, json=body)
    check("带 campusBaseline 上传成功", r.status_code == 200, r.text[:200])
    cid = r.json()["data"]["id"]
    check("一致度 matched=1 / total=2",
          r.json()["data"]["consistency"] == {"matched": 1, "total": 2},
          str(r.json()["data"].get("consistency")))
    r = client.get(f"/admin/timetables/{cid}", headers=admin)
    dd = r.json()["data"]
    check("详情 diff 有 same / diff 两种",
          {i["status"] for i in dd["diff"]["items"]} == {"same", "diff"},
          str([i["status"] for i in dd["diff"]["items"]]))
    check("diff 项标到字段级（uploaded + baseline 都在）",
          all("uploaded" in i and "baseline" in i
              for i in dd["diff"]["items"] if i["status"] == "diff"),
          str(dd["diff"]["items"]))

    r = client.post("/api/v1/timetables/uploads", headers=dev_a,
                    json=upload_body(bj="25电气3班", uploaderName="钱同学"))
    nid = r.json()["data"]["id"]
    r = client.get(f"/admin/timetables/{nid}", headers=admin)
    check("不带 baseline → campus_baseline 为 null / diff 空",
          r.json()["data"]["campus_baseline"] == []
          and r.json()["data"]["diff"]["items"] == [], r.text[:300])
    check("不带 baseline → consistency 两值为 0",
          r.json()["data"]["upload"]["consistency"] == {"matched": 0, "total": 0})

    print("== 隐私 ==")
    body = upload_body(bj="25电气4班", uploaderName="孙同学",
                       courses=[dict(COURSES[0], classroom="13812345678")])
    r = client.post("/api/v1/timetables/uploads", headers=dev_a, json=body)
    pid = r.json()["data"]["id"]
    check("课程里疑似手机号 → 置空并打标记",
          r.json()["data"]["personal_flag"] == 1, r.text[:200])
    r = client.get(f"/admin/timetables/{pid}", headers=admin)
    check("疑似手机号字段入库为空",
          r.json()["data"]["courses"][0]["classroom"] == "",
          str(r.json()["data"]["courses"][0]))
    with dbmod.connect(cfg.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_logs "
            "WHERE action='timetable_personal_flagged'").fetchone()["n"]
    check("写入了 personal_flag 审计", n >= 1, f"n={n}")

    print("== mine ==")
    r = client.get("/api/v1/timetables/uploads/mine", headers=dev_a)
    mine = r.json()["data"]
    check("mine 含非发布状态", mine["count"] >= 1
          and any(u["status"] != "approved" for u in mine["uploads"]),
          str(mine["count"]))
    check("mine 响应里没有 contributor", "contributor" not in r.text)
    r2 = client.get("/api/v1/timetables/uploads/mine", headers=dev_b)
    check("另一台设备看不到别人的上传",
          all(u["uploader_name"] != "钱同学" for u in r2.json()["data"]["uploads"]))

    print("== 错误信封 ==")
    r = client.post("/api/v1/timetables/search", headers=dev_a,
                    json={"xnm": "", "xqm": "3"})
    env = r.json()
    check("错误响应是 error/request_id 信封",
          r.status_code == 400 and "error" in env
          and set(env["error"]) == {"code", "message"}
          and "request_id" in env, str(env))
    check("没有 toolbox 那种 detail 形状", "detail" not in env)

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
