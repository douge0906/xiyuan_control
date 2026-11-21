"""无表头单行课表截图的解析回归（不依赖 OCR，直接喂文本行）。"""

import sys

sys.path.insert(0, "/opt/xiyuan-control")
from app import course_ocr as M


def ln(text, left, top=10):
    return {"text": text, "left": left, "right": left + 40, "top": top, "bottom": top + 20}


def check(name, lines, expect):
    got = M._single_row_courses(lines)
    assert len(got) == len(expect), f"{name}: {got}"
    for g, e in zip(got, expect):
        for k, v in e.items():
            assert g[k] == v, f"{name}.{k}: {g[k]!r} != {v!r}"
    print("PASS", name, got)


check("实验安排表单行", [
    ln("on", 3), ln("第2周", 41), ln("星期1", 126),
    ln("20260914 9-10(18:45-20:25)", 203), ln("）25微电子1模拟电子技术基础丨实验(131302011)", 478),
    ln("二极管伏安特性的测量", 948), ln("电路与电子技术实验室2(21201032)", 1255),
    ln("巫君杰(860015)", 1594),
], [{"name": "模拟电子技术基础丨实验", "className": "25微电子1",
     "academicYear": "2026-2027", "academicTerm": "第一学期",
     "teacher": "巫君杰", "classroom": "电路与电子技术实验室2",
     "weekday": 1, "startSlot": 9, "endSlot": 10, "weeks": "2周"}])

check("教务列表单行", [
    ln("第14", 55), ln("星期6", 152), ln("3-4（10:10-", 328), ln("25电气1,25电气2.25", 474),
    ln("模拟电子技术基础Ⅱ（平台课）", 627), ln("实验五、集成运算放大器的", 872),
    ln("电路与电子技术实验室", 1070), ln("闫纪源", 1275), ln("20261212", 225),
    ln("周", 54), ln("11:50)", 329), ln("(131302002)", 629),
    ln("基本应用", 872), ln("5(21201035)", 1070), ln("(860251)", 1277),
], [{"name": "模拟电子技术基础Ⅱ（平台课）", "className": "25电气1、25电气2",
     "academicYear": "2026-2027", "academicTerm": "第一学期",
     "teacher": "闫纪源", "classroom": "电路与电子技术实验室5",
     "weekday": 6, "startSlot": 3, "endSlot": 4, "weeks": "14周"}])

def row(text, left, top):
    return ln(text, left, top)


def check_table_class():
    """有表头的表格：班级名称列必须进 course.className，不能只留在 blocks。"""
    lines = [
        row("周次", 20, 10), row("星期", 100, 10), row("时间", 190, 10),
        row("节次/时间", 300, 10), row("班级名称", 470, 10),
        row("课程名称", 700, 10), row("实验室", 1050, 10),
        row("教师", 1400, 10),
        row("第2周", 20, 60), row("星期1", 100, 60),
        row("20260914", 190, 60), row("9-10", 300, 60),
        row("25微电子1", 470, 60),
        row("模拟电子技术基础丨实验(131302011)", 700, 60),
        row("电路与电子技术实验室2(21201032)", 1050, 60),
        row("巫君杰(860015)", 1400, 60),
        row("第6周", 20, 110), row("星期1", 100, 110),
        row("20261012", 190, 110), row("9-10", 300, 110),
        row("25微电子1", 470, 110),
        row("模拟电子技术基础丨实验(131302011)", 700, 110),
        row("电路与电子技术实验室2(21201032)", 1050, 110),
        row("巫君杰(860015)", 1400, 110),
    ]
    got = M._table_courses(lines)
    assert len(got) == 1, got
    assert got[0]["className"] == "25微电子1", got
    assert got[0]["academicYear"] == "2026-2027", got
    assert got[0]["academicTerm"] == "第一学期", got
    print("PASS 表格班级列", got)


check_table_class()

# 无表头宽表：课程列不一定带「实验/原理」关键词，必须靠 8~10 位课程编码
# 把它认成 name，否则整张表会退回到卡片式解析、产出垃圾课程。
def check_headerless_multirow():
    lines = []
    rows = [
        ("第10周", "星期3", "20261111", "9-11(18:45-21:20)", "24自动化2,24自动化3",
         "科技论文写作Ⅱ(171503012)", "科技论文的写作", "计算机基础教学实验室6(10301016)"),
        ("第10周", "星期5", "20261113", "9-11(18:45-21:20)", "24自动化2,24自动化3",
         "科技论文写作Ⅱ(171503012)", "科技论文的写作", "计算机基础教学实验室5(10301015)"),
        ("第11周", "星期3", "20261118", "9-11(18:45-21:20)", "24自动化2,24自动化3",
         "科技论文写作Ⅱ(171503012)", "科研绘图", "计算机基础教学实验室6(10301016)"),
        ("第11周", "星期5", "20261120", "7-8(15:55-17:35)", "24自动化2,24自动化3",
         "科技论文写作Ⅱ(171503012)", "EndNote软件的使用", "计算机基础教学实验室5(10301015)"),
        ("第12周", "星期1", "20261123", "7-8(15:55-17:35)", "24自动化2,24自动化3",
         "科技论文写作Ⅱ(171503012)", "LaTeX软件的使用", "计算机基础教学实验室5(10301015)"),
        ("第12周", "星期3", "20261125", "9-11(18:45-21:20)", "24自动化2,24自动化3",
         "科技论文写作Ⅱ(171503012)", "LaTeX软件的使用", "计算机基础教学实验室5(10301015)"),
    ]
    cols = (20, 110, 210, 300, 500, 680, 980, 1240)
    for ri, r in enumerate(rows):
        for ci, text in enumerate(r):
            lines.append(ln(text, cols[ci], 10 + ri * 50))
    got = M._table_courses(lines)
    assert len(got) >= 1, got
    assert all(g["name"].startswith("科技论文写作") for g in got), got
    assert all(g["className"] == "24自动化2、24自动化3" for g in got), got
    assert all(g["academicYear"] == "2026-2027" for g in got), got
    print("PASS 无表头多行宽表", got)


check_headerless_multirow()

assert M._clean_class("25电科125电科2（25电科2）") == "25电科1、25电科2（25电科2）"
assert M._clean_class("25电气1,25电气2.25 电气3") == "25电气1、25电气2、25电气3"
assert M._column_kind(["科技论文写作Ⅱ（17150301 2)"] * 6) == "name"
print("PASS 班级缺失分隔符/课程编码列")

assert M._single_row_courses([
    ln("周次", 59), ln("星期", 136), ln("时间", 223), ln("节次/时间", 342),
    ln("班级名称", 487), ln("课程名称", 722), ln("项目名称", 1056),
    ln("实验室", 1389), ln("教师", 1646),
]) == []
print("PASS 只有表头")

# Excel 日期+节次粘连时，必须取日期后面的 1-2，而不是 101-2/10-2
assert M._cell_slots("202610101-2(08:00-09:40)") == (1, 2)
assert M._date_weekday("20261011") == 7
print("PASS Excel 节次/日期星期")
