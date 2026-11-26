"""无锡学院统一认证验证码识别（纯标准库，服务端复用安卓端模板）。

与客户端 `lib/services/campus/captcha_solver.dart` 是同一套算法：
验证码是 100×25 固定布局的算式（`7 * 2 =`），按 x 区间切「数字1 / 运算符 /
数字2」三段，各取墨迹外接框最近邻缩放到 12×20，再与模板做汉明距离匹配。

实测（本校样本 7:3 训练/测试）整题准确率 97%~100%，模板仅 3.3KB。
识别不出来返回 None —— 调用方回退人工，绝不猜答案。

⚠️ 学校若改验证码尺寸/字体，这里会直接返回 None（不会误判），需要重新采样本
训练模板并同时替换客户端的 `captcha_templates.g.dart`。
"""

from __future__ import annotations

import zlib

from .captcha_tpl import DIGIT_TEMPLATES, OPERATOR_TEMPLATES, TPL_H, TPL_W

#: 三个有效字段的 x 区间（等号在 ~77-87，不参与答案）
_FIELDS = ((0, 20), (20, 45), (45, 70))
#: 与模板训练时一致的背景判定阈值
_WHITE = 200


def _png_rgb(data: bytes) -> tuple[int, int, bytes] | None:
    """最小 PNG 解码：只支持 8bit、非隔行、RGB/RGBA，其余返回 None。

    验证码图实测落在支持范围内（687~1012 B 的 RGB/RGBA PNG）。
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    width = height = depth = ctype = interlace = 0
    idat: list[bytes] = []
    while pos + 8 <= len(data):
        ln = int.from_bytes(data[pos : pos + 4], "big")
        typ = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            depth = chunk[8]
            ctype = chunk[9]
            interlace = chunk[12]
        elif typ == b"IDAT":
            idat.append(chunk)
        elif typ == b"IEND":
            break
    if depth != 8 or interlace != 0 or ctype not in (2, 6):
        return None
    try:
        raw = zlib.decompress(b"".join(idat))
    except Exception:  # noqa: BLE001
        return None
    bpp = 3 if ctype == 2 else 4
    stride = width * bpp
    if width <= 0 or height <= 0 or len(raw) < (stride + 1) * height:
        return None

    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        f = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa = abs(b - c)
                pb = abs(a - c)
                pc = abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif f != 0:
            return None
        row = y * width * 3
        for x in range(width):
            o = x * bpp
            out[row + x * 3] = line[o]
            out[row + x * 3 + 1] = line[o + 1]
            out[row + x * 3 + 2] = line[o + 2]
        prev = line
    return width, height, bytes(out)


def _bits(width: int, height: int, rgb: bytes, white: int) -> list[int]:
    """三通道都 > white 记为白底（0），否则记为墨迹（1）。"""
    bits = [0] * (width * height)
    for i in range(width * height):
        o = i * 3
        if not (rgb[o] > white and rgb[o + 1] > white and rgb[o + 2] > white):
            bits[i] = 1
    return bits


def _normalize(
    bits: list[int], width: int, height: int, x0: int, x1: int
) -> list[int] | None:
    """取字段内墨迹外接框，最近邻缩放到 12×20；该字段空白返回 None。"""
    min_x, min_y = width, height
    max_x, max_y = -1, -1
    for y in range(height):
        base = y * width
        for x in range(x0, min(x1, width)):
            if bits[base + x]:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return None
    bw = max_x - min_x + 1
    bh = max_y - min_y + 1
    out = [0] * (TPL_W * TPL_H)
    for ny in range(TPL_H):
        sy = min_y + (ny * bh) // TPL_H
        row = sy * width
        for nx in range(TPL_W):
            sx = min_x + (nx * bw) // TPL_W
            out[ny * TPL_W + nx] = bits[row + sx]
    return out


def _match(
    bits: list[int], width: int, height: int, idx: int, templates: dict[str, str]
) -> str | None:
    norm = _normalize(bits, width, height, *_FIELDS[idx])
    if norm is None or not templates:
        return None
    best: str | None = None
    best_d = 1 << 30
    for ch, tpl in templates.items():
        d = 0
        for i, c in enumerate(tpl):
            if norm[i] != (1 if c == "1" else 0):
                d += 1
        if d < best_d:
            best_d = d
            best = ch
    # 没有阈值时，任意噪声图形都会“匹配”到最近邻模板，把一个必错的答案
    # 送进登录请求。超过 1/4 像素不一致就认为不是这个字，交人工兜底。
    max_d = int(TPL_W * TPL_H * 0.25)
    return best if best_d <= max_d else None


def _solve_at(png: bytes, white: int) -> str | None:
    dec = _png_rgb(png)
    if dec is None:
        return None
    width, height, rgb = dec
    if width != 100 or height != 25:
        return None
    bits = _bits(width, height, rgb, white)
    a = _match(bits, width, height, 0, DIGIT_TEMPLATES)
    op = _match(bits, width, height, 1, OPERATOR_TEMPLATES)
    b = _match(bits, width, height, 2, DIGIT_TEMPLATES)
    if a is None or op is None or b is None:
        return None
    try:
        x, y = int(a), int(b)
    except ValueError:
        return None
    if op == "+":
        return str(x + y)
    if op == "*":
        return str(x * y)
    if op == "-":
        return str(x - y)
    if op == "/":
        return "0" if y == 0 else str(x // y)
    return None


def solve(png: bytes) -> str | None:
    """识别算式并算出答案；尺寸不符或识别失败返回 None（调用方回退人工）。

    先用与客户端一致的 200 阈值；识别不出来再降一档 245 —— 学校偶尔会把
    某个字符画成很浅的彩色（实测有整块最暗通道 219 的样本，按 200 会被当成
    纯背景，整题直接放弃）。背景是纯白 255，245 这一档不会引入噪声。
    """
    for white in (_WHITE, 245):
        answer = _solve_at(png, white)
        if answer is not None:
            return answer
    return None
