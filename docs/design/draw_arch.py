# -*- coding: utf-8 -*-
"""渗透测试智能体（secpt）系统架构图 —— 参考 Hermes Agent 架构图版式绘制。"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 2000, 2860
S = 2  # supersampling
FONT_DIR = r"C:\Windows\Fonts"


def font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), int(size * S))


F_TITLE = font("msyhbd.ttc", 62)
F_SUB = font("msyh.ttc", 25)
F_BOX = font("msyhbd.ttc", 32)
F_LABEL = font("msyhbd.ttc", 28)
F_ITEM = font("msyh.ttc", 26)
F_SMALL = font("msyh.ttc", 22)
F_TINY = font("msyh.ttc", 20)
F_EDGE = font("msyhbd.ttc", 24)
F_ZOOM = font("msyh.ttc", 19)
F_EN = font("msyhbd.ttc", 24)

# ---------------- palette ----------------
BLUE_D, BLUE_M, BLUE_L = (36, 84, 200), (59, 130, 246), (147, 197, 253)
TEAL_D, TEAL_M, TEAL_L = (13, 120, 120), (20, 160, 160), (204, 251, 241)
GREEN_D, GREEN_L = (22, 140, 100), (66, 200, 150)
ORANGE_D, ORANGE_L = (234, 120, 20), (253, 186, 116)
RED_D, RED_L = (190, 45, 75), (244, 114, 182)
PURPLE_D, PURPLE_L = (110, 60, 190), (167, 139, 250)
INDIGO_D, INDIGO_L = (60, 70, 150), (129, 140, 248)
SLATE_D, SLATE_M = (45, 60, 90), (100, 116, 139)
WHITE = (255, 255, 255)
INK = (20, 33, 61)


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def vgrad(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=mix(top, bottom, y / max(1, h - 1)))
    return img


class Canvas:
    def __init__(self, w, h):
        bg = vgrad((w, h), (240, 247, 255), (227, 240, 255))
        bd = ImageDraw.Draw(bg)
        for i in range(0, w + h, 46):
            bd.line([(i, 0), (i - h, h)], fill=(232, 241, 253), width=S)
        self.img = bg
        self.d = ImageDraw.Draw(self.img)

    # --- grid helper ---
    def g(self, v):
        return int(v * S)

    def rrect(self, box, radius=18, fill=None, outline=None, width=2):
        b = [self.g(v) for v in box]
        outline = tuple(outline) + (255,) if outline and len(outline) == 3 else outline
        self.d.rounded_rectangle(
            b, radius=self.g(radius), fill=fill, outline=outline, width=int(width * S)
        )

    def text(self, xy, s, f, fill, anchor="la"):
        self.d.text((self.g(xy[0]), self.g(xy[1])), s, font=f, fill=fill, anchor=anchor)

    def tlen(self, s, f):
        return self.d.textlength(s, font=f) / S

    def lines(self, x, y, items, f, fill, lh):
        for i, s in enumerate(items):
            self.text((x, y + i * lh), s, f, fill)

    def wrap(self, s, f, maxw):
        out, cur = [], ""
        for ch in s:
            if self.tlen(cur + ch, f) > maxw and cur:
                out.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            out.append(cur)
        return out

    def polygon(self, pts, fill):
        self.d.polygon(
            [(self.g(x), self.g(y)) for x, y in pts],
            fill=fill + (255,) if len(fill) == 3 else fill,
        )


C = Canvas(W, H)


def card_rect(box, radius, base):
    """凸起卡片: 阴影 + 立体面 + 高光 + 描边"""
    x1, y1, x2, y2 = box
    sh = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rounded_rectangle(
        [C.g(x1 + 3), C.g(y1 + 6), C.g(x2 + 3), C.g(y2 + 6)],
        radius=C.g(radius),
        fill=(30, 50, 100, 55),
    )
    sh = sh.filter(ImageFilter.GaussianBlur(6 * S))
    C.img.alpha_composite(sh) if C.img.mode == "RGBA" else C.img.paste(sh, (0, 0), sh)
    C.d = ImageDraw.Draw(C.img)

    face = vgrad(
        (C.g(x2 - x1), C.g(y2 - y1)), mix(base, WHITE, 0.82), mix(base, WHITE, 0.55)
    )
    mask = Image.new("L", face.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, face.size[0] - 1, face.size[1] - 1], radius=C.g(radius), fill=255
    )
    # 顶部高光条
    hd = ImageDraw.Draw(face)
    hd.rounded_rectangle(
        [0, 0, face.size[0], C.g(radius + 8)],
        radius=C.g(radius),
        fill=mix(base, WHITE, 0.92),
    )
    C.img.paste(face, (C.g(x1), C.g(y1)), mask)
    C.d = ImageDraw.Draw(C.img)
    C.rrect(box, radius, None, mix(base, WHITE, 0.1), 2.5)


def icon(x, y, kind, color, scale=1.0):
    """x,y 为图标左上角，默认尺寸 46*scale。"""
    s = scale
    k = color
    if kind == "monitor":
        C.rrect((x, y, x + 46 * s, y + 30 * s), 5 * s, mix(k, WHITE, 0.85), k, 2.5)
        C.d.rectangle(
            [C.g(x + 16 * s), C.g(y + 30 * s), C.g(x + 30 * s), C.g(y + 36 * s)], fill=k
        )
        C.d.rectangle(
            [C.g(x + 9 * s), C.g(y + 36 * s), C.g(x + 37 * s), C.g(y + 40 * s)], fill=k
        )
    elif kind == "globe":
        cx, cy, r = x + 23 * s, y + 23 * s, 22 * s
        C.d.ellipse(
            [C.g(cx - r), C.g(cy - r), C.g(cx + r), C.g(cy + r)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.ellipse(
            [C.g(cx - r * 0.45), C.g(cy - r), C.g(cx + r * 0.45), C.g(cy + r)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.line(
            [C.g(cx - r), C.g(cy), C.g(cx + r), C.g(cy)], fill=k, width=int(2.5 * S)
        )
    elif kind == "terminal":
        C.rrect((x, y, x + 46 * s, y + 38 * s), 6 * s, mix(k, WHITE, 0.9), k, 2.5)
        C.text(
            (x + 10 * s, y + 8 * s),
            ">_",
            font("simhei.ttf" if False else "msyhbd.ttc", 17 * s),
            k,
        )
    elif kind == "code":
        C.text((x + 4 * s, y + 2 * s), "</>", F_SMALL, k)
    elif kind == "phone":
        C.rrect(
            (x + 10 * s, y, x + 36 * s, y + 44 * s), 6 * s, mix(k, WHITE, 0.9), k, 2.5
        )
        C.d.ellipse(
            [C.g(x + 19 * s), C.g(y + 35 * s), C.g(x + 27 * s), C.g(y + 40 * s)], fill=k
        )
    elif kind == "chat":
        C.rrect(
            (x, y + 2 * s, x + 46 * s, y + 32 * s), 10 * s, mix(k, WHITE, 0.88), k, 2.5
        )
        C.polygon(
            [
                (x + 10 * s, y + 32 * s),
                (x + 8 * s, y + 42 * s),
                (x + 20 * s, y + 32 * s),
            ],
            k,
        )
        for i in range(3):
            cx = x + 14 * s + i * 9 * s
            C.d.ellipse(
                [C.g(cx - 2 * s), C.g(y + 15 * s), C.g(cx + 2 * s), C.g(y + 19 * s)],
                fill=k,
            )
    elif kind == "clock":
        cx, cy, r = x + 14 * s, y + 14 * s, 12 * s
        C.d.ellipse(
            [C.g(cx - r), C.g(cy - r), C.g(cx + r), C.g(cy + r)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.line(
            [
                C.g(cx),
                C.g(cy - 7 * s),
                C.g(cx),
                C.g(cy),
                C.g(cx + 6 * s),
                C.g(cy + 3 * s),
            ],
            fill=k,
            width=int(2.5 * S),
        )
    elif kind == "db":
        C.d.ellipse(
            [C.g(x + 2 * s), C.g(y + 2 * s), C.g(x + 34 * s), C.g(y + 16 * s)], fill=k
        )
        C.d.rectangle(
            [C.g(x + 2 * s), C.g(y + 9 * s), C.g(x + 34 * s), C.g(y + 30 * s)], fill=k
        )
        C.d.ellipse(
            [C.g(x + 2 * s), C.g(y + 24 * s), C.g(x + 34 * s), C.g(y + 38 * s)], fill=k
        )
        for yy in (22, 30):
            C.d.ellipse(
                [
                    C.g(x + 2 * s),
                    C.g(y + (yy - 6) * s),
                    C.g(x + 34 * s),
                    C.g(y + (yy + 6) * s),
                ],
                outline=mix(k, WHITE, 0.75),
                width=int(2 * S),
            )
    elif kind == "crosshair":
        cx, cy, r = x + 22 * s, y + 22 * s, 19 * s
        C.d.ellipse(
            [C.g(cx - r), C.g(cy - r), C.g(cx + r), C.g(cy + r)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.ellipse(
            [C.g(cx - 6 * s), C.g(cy - 6 * s), C.g(cx + 6 * s), C.g(cy + 6 * s)], fill=k
        )
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            C.d.line(
                [
                    C.g(cx + dx * 9 * s),
                    C.g(cy + dy * 9 * s),
                    C.g(cx + dx * 23 * s),
                    C.g(cy + dy * 23 * s),
                ],
                fill=k,
                width=int(2.5 * S),
            )
    elif kind == "route":
        C.d.ellipse(
            [C.g(x + 2 * s), C.g(y + 2 * s), C.g(x + 12 * s), C.g(y + 12 * s)], fill=k
        )
        C.d.ellipse(
            [C.g(x + 32 * s), C.g(y + 32 * s), C.g(x + 42 * s), C.g(y + 42 * s)], fill=k
        )
        C.d.line(
            [
                C.g(x + 7 * s),
                C.g(y + 14 * s),
                C.g(x + 7 * s),
                C.g(y + 37 * s),
                C.g(x + 32 * s),
                C.g(y + 37 * s),
            ],
            fill=k,
            width=int(2.5 * S),
        )
    elif kind == "folder":
        C.rrect((x, y + 6 * s, x + 44 * s, y + 38 * s), 6 * s, k, k, 2)
        C.rrect((x, y + 2 * s, x + 20 * s, y + 14 * s), 4 * s, k, k, 2)
    elif kind == "clock2":
        cx, cy, r = x + 22 * s, y + 22 * s, 19 * s
        C.d.ellipse(
            [C.g(cx - r), C.g(cy - r), C.g(cx + r), C.g(cy + r)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.line(
            [
                C.g(cx),
                C.g(cy - 10 * s),
                C.g(cx),
                C.g(cy),
                C.g(cx + 9 * s),
                C.g(cy + 5 * s),
            ],
            fill=k,
            width=int(2.5 * S),
        )
    elif kind == "memory":
        C.rrect(
            (x + 2 * s, y + 6 * s, x + 42 * s, y + 38 * s),
            4 * s,
            mix(k, WHITE, 0.9),
            k,
            2.5,
        )
        for i in range(4):
            yy = y + 13 * s + i * 7 * s
            C.d.line(
                [C.g(x + 8 * s), C.g(yy), C.g(x + 36 * s), C.g(yy)],
                fill=k,
                width=int(2 * S),
            )
    elif kind == "gear":
        C.d.ellipse(
            [C.g(x + 6 * s), C.g(y + 6 * s), C.g(x + 38 * s), C.g(y + 38 * s)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.ellipse(
            [C.g(x + 16 * s), C.g(y + 16 * s), C.g(x + 28 * s), C.g(y + 28 * s)], fill=k
        )
        for a in range(0, 360, 45):
            r1, r2 = 18 * s, 22 * s
            cx, cy = x + 22 * s, y + 22 * s
            C.d.line(
                [
                    C.g(cx + r1 * math.cos(math.radians(a))),
                    C.g(cy + r1 * math.sin(math.radians(a))),
                    C.g(cx + r2 * math.cos(math.radians(a))),
                    C.g(cy + r2 * math.sin(math.radians(a))),
                ],
                fill=k,
                width=int(2.5 * S),
            )
    elif kind == "plug":
        C.rrect(
            (x + 4 * s, y + 8 * s, x + 40 * s, y + 36 * s),
            6 * s,
            mix(k, WHITE, 0.9),
            k,
            2.5,
        )
        for i in range(3):
            xx = x + 14 * s + i * 8 * s
            C.d.line(
                [C.g(xx), C.g(y + 2 * s), C.g(xx), C.g(y + 8 * s)],
                fill=k,
                width=int(2.5 * S),
            )
    elif kind == "bot":
        C.rrect(
            (x + 2 * s, y + 8 * s, x + 42 * s, y + 38 * s),
            8 * s,
            mix(k, WHITE, 0.88),
            k,
            2.5,
        )
        C.d.ellipse(
            [C.g(x + 14 * s), C.g(y + 18 * s), C.g(x + 20 * s), C.g(y + 24 * s)], fill=k
        )
        C.d.ellipse(
            [C.g(x + 25 * s), C.g(y + 18 * s), C.g(x + 31 * s), C.g(y + 24 * s)], fill=k
        )
    elif kind == "net":
        cx, cy = x + 22 * s, y + 22 * s
        for a in range(0, 360, 60):
            r = 17 * s
            px, py = (
                cx + r * math.cos(math.radians(a)),
                cy + r * math.sin(math.radians(a)),
            )
            C.d.line([C.g(cx), C.g(cy), C.g(px), C.g(py)], fill=k, width=int(2 * S))
            C.d.ellipse(
                [C.g(px - 5 * s), C.g(py - 5 * s), C.g(px + 5 * s), C.g(py + 5 * s)],
                fill=k,
            )
        C.d.ellipse(
            [C.g(cx - 5 * s), C.g(cy - 5 * s), C.g(cx + 5 * s), C.g(cy + 5 * s)], fill=k
        )
    elif kind == "shield":
        C.polygon(
            [
                (x + 22 * s, y + 2 * s),
                (x + 41 * s, y + 10 * s),
                (x + 39 * s, y + 27 * s),
                (x + 22 * s, y + 43 * s),
                (x + 5 * s, y + 27 * s),
                (x + 3 * s, y + 10 * s),
            ],
            mix(k, WHITE, 0.85),
        )
        C.polygon(
            [
                (x + 22 * s, y + 2 * s),
                (x + 41 * s, y + 10 * s),
                (x + 39 * s, y + 27 * s),
                (x + 22 * s, y + 43 * s),
                (x + 5 * s, y + 27 * s),
                (x + 3 * s, y + 10 * s),
            ],
            None,
        ) if False else None
        C.d.line(
            [
                (C.g(x + 12 * s), C.g(y + 22 * s)),
                (C.g(x + 19 * s), C.g(y + 29 * s)),
                (C.g(x + 32 * s), C.g(y + 14 * s)),
            ],
            fill=k,
            width=int(3 * S),
        )
    elif kind == "report":
        C.rrect(
            (x + 8 * s, y, x + 38 * s, y + 44 * s), 4 * s, mix(k, WHITE, 0.9), k, 2.5
        )
        for i in range(4):
            yy = y + 10 * s + i * 8 * s
            C.d.line(
                [C.g(x + 14 * s), C.g(yy), C.g(x + 32 * s), C.g(yy)],
                fill=k,
                width=int(2 * S),
            )
    elif kind == "target":
        for r, wd in ((19, 2.5), (11, 2.5)):
            C.d.ellipse(
                [
                    C.g(x + 22 * s - r * s),
                    C.g(y + 22 * s - r * s),
                    C.g(x + 22 * s + r * s),
                    C.g(y + 22 * s + r * s),
                ],
                outline=k,
                width=int(wd * S),
            )
        C.d.ellipse(
            [C.g(x + 18 * s), C.g(y + 18 * s), C.g(x + 26 * s), C.g(y + 26 * s)], fill=k
        )
        C.d.line(
            [C.g(x + 34 * s), C.g(y + 34 * s), C.g(x + 43 * s), C.g(y + 43 * s)],
            fill=k,
            width=int(3 * S),
        )
    elif kind == "file":
        C.rrect(
            (x + 8 * s, y, x + 38 * s, y + 44 * s), 3 * s, mix(k, WHITE, 0.9), k, 2.5
        )
        C.polygon(
            [(x + 26 * s, y), (x + 38 * s, y + 12 * s), (x + 26 * s, y + 12 * s)], k
        )
    elif kind == "play":
        C.d.ellipse(
            [C.g(x + 2 * s), C.g(y + 2 * s), C.g(x + 42 * s), C.g(y + 42 * s)],
            outline=k,
            width=int(2.5 * S),
        )
        C.polygon(
            [
                (x + 17 * s, y + 12 * s),
                (x + 32 * s, y + 22 * s),
                (x + 17 * s, y + 32 * s),
            ],
            k,
        )
    elif kind == "home":
        C.polygon(
            [
                (x + 22 * s, y + 2 * s),
                (x + 42 * s, y + 20 * s),
                (x + 36 * s, y + 20 * s),
                (x + 36 * s, y + 42 * s),
                (x + 8 * s, y + 42 * s),
                (x + 8 * s, y + 20 * s),
                (x + 2 * s, y + 20 * s),
            ],
            k,
        )
    elif kind == "link":
        C.d.arc(
            [C.g(x + 2 * s), C.g(y + 10 * s), C.g(x + 26 * s), C.g(y + 34 * s)],
            90,
            270,
            fill=k,
            width=int(3 * S),
        )
        C.d.arc(
            [C.g(x + 18 * s), C.g(y + 10 * s), C.g(x + 42 * s), C.g(y + 34 * s)],
            270,
            450,
            fill=k,
            width=int(3 * S),
        )
        C.d.line(
            [C.g(x + 14 * s), C.g(y + 22 * s), C.g(x + 30 * s), C.g(y + 22 * s)],
            fill=k,
            width=int(3 * S),
        )
    elif kind == "human":
        C.d.ellipse(
            [C.g(x + 6 * s), C.g(y + 2 * s), C.g(x + 18 * s), C.g(y + 14 * s)], fill=k
        )
        C.d.pieslice(
            [C.g(x + 2 * s), C.g(y + 14 * s), C.g(x + 22 * s), C.g(y + 34 * s)],
            180,
            360,
            fill=k,
        )
        C.d.ellipse(
            [C.g(x + 28 * s), C.g(y + 2 * s), C.g(x + 40 * s), C.g(y + 14 * s)], fill=k
        )
        C.d.pieslice(
            [C.g(x + 24 * s), C.g(y + 14 * s), C.g(x + 44 * s), C.g(y + 34 * s)],
            180,
            360,
            fill=k,
        )
    elif kind == "seal":
        C.d.ellipse(
            [C.g(x + 2 * s), C.g(y + 2 * s), C.g(x + 42 * s), C.g(y + 42 * s)], fill=k
        )
        C.d.ellipse(
            [C.g(x + 14 * s), C.g(y + 14 * s), C.g(x + 30 * s), C.g(y + 30 * s)],
            fill=mix(k, WHITE, 0.55),
        )
    elif kind == "rocket":
        C.polygon(
            [
                (x + 22 * s, y),
                (x + 32 * s, y + 20 * s),
                (x + 32 * s, y + 34 * s),
                (x + 12 * s, y + 34 * s),
                (x + 12 * s, y + 20 * s),
            ],
            k,
        )
        C.polygon(
            [
                (x + 12 * s, y + 34 * s),
                (x + 22 * s, y + 46 * s),
                (x + 32 * s, y + 34 * s),
            ],
            mix(k, WHITE, 0.7),
        )
    elif kind == "scope":
        cx, cy, r = x + 22 * s, y + 22 * s, 17 * s
        C.d.ellipse(
            [C.g(cx - r), C.g(cy - r), C.g(cx + r), C.g(cy + r)],
            outline=k,
            width=int(2.5 * S),
        )
        C.d.pieslice(
            [C.g(cx - r), C.g(cy - r), C.g(cx + r), C.g(cy + r)],
            270,
            360,
            fill=mix(k, WHITE, 0.45),
        )
        C.d.line([C.g(cx), C.g(cy), C.g(cx), C.g(cy - r)], fill=k, width=int(2.5 * S))
        C.d.line(
            [C.g(cx - r - 4 * s), C.g(cy), C.g(cx - r + 4 * s), C.g(cy)],
            fill=k,
            width=int(2.5 * S),
        )
        C.d.line(
            [C.g(cx + r - 4 * s), C.g(cy), C.g(cx + r + 4 * s), C.g(cy)],
            fill=k,
            width=int(2.5 * S),
        )


def arrow(
    pts,
    color,
    width=3,
    dashed=False,
    head=True,
    label=None,
    lpos=0.5,
    label_bg=WHITE,
    label_offset=(0, 0),
    radius=0,
    font_=F_EDGE,
    tag=None,
    tag_side=1,
    arrows=None,
):
    """折线箭头；pts 至少两点。arrows: [(x, y, 'up'|'down'|'left'|'right')] 在线上叠加热力箭头。"""
    pts_s = [(C.g(x), C.g(y)) for x, y in pts]
    pts_s = [pts_s[0]]
    for p in pts[1:]:
        q = (int(p[0] * S), int(p[1] * S))
        if q != pts_s[-1]:
            pts_s.append(q)
    path = [pts_s[0]]
    for i in range(1, len(pts_s) - 1):
        p, q, r = pts_s[i - 1], pts_s[i], pts_s[i + 1]
        # 圆角
        r1 = min(radius * S, math.hypot(p[0] - q[0], p[1] - q[1]) / 2)
        r2 = min(radius * S, math.hypot(r[0] - q[0], r[1] - q[1]) / 2)
        rr = max(1, min(r1, r2))

        def towards(a, b, d):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy) or 1
            return (a[0] + dx / L * d, a[1] + dy / L * d)

        a1 = towards(q, p, rr)
        a2 = towards(q, r, rr)
        path += [a1, q, a2]
    path.append(pts_s[-1])

    flat = [p for p in path]
    if head and len(flat) > 1:
        # 末端缩短留出箭头
        (x0, y0), (x1, y1) = flat[-2], flat[-1]
        L = math.hypot(x1 - x0, y1 - y0) or 1
        tip = (x1 - (x1 - x0) / L * 4 * S, y1 - (y1 - y0) / L * 4 * S)
        flat[-1] = tip

    def seg_draw(draw):
        if dashed:
            # 逐段虚线
            for a, b in zip(flat[:-1], flat[1:]):
                L = math.hypot(b[0] - a[0], b[1] - a[1])
                if L == 0:
                    continue
                n = max(1, int(L // (12 * S)))
                for k in range(n):
                    t0, t1 = k / n, (k + 0.55) / n
                    draw.line(
                        [
                            (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                            (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1),
                        ],
                        fill=color + (255,),
                        width=int(width * S),
                    )
        else:
            draw.line(flat, fill=color + (255,), width=int(width * S), joint="curve")

    if len(pts_s) > 2:
        # 圆角处补画小圆弧的简单替代：用较粗圆点填充拐角
        pass
    seg_draw(C.d)
    if head:
        (x0, y0), (x1, y1) = flat[-2], flat[-1]
        ang = math.atan2(y1 - y0, x1 - x0)
        hl, hw = 15 * S, 8 * S
        p1 = (x1, y1)
        p2 = (
            x1 - hl * math.cos(ang) + hw * math.sin(ang),
            y1 - hl * math.sin(ang) - hw * math.cos(ang),
        )
        p3 = (
            x1 - hl * math.cos(ang) - hw * math.sin(ang),
            y1 - hl * math.sin(ang) + hw * math.cos(ang),
        )
        C.d.polygon([p1, p2, p3], fill=color + (255,))
    if arrows:
        base = (
            math.atan2(flat[-1][1] - flat[-2][1], flat[-1][0] - flat[-2][0])
            if head
            else 0
        )
        for ax, ay, d in arrows:
            a = {"right": 0, "down": 90, "left": 180, "up": 270}[d] + base
            a = math.radians(a)
            cx, cy = C.g(ax), C.g(ay)
            hl, hw = 18 * S, 12 * S
            tip = (cx + hl * math.cos(a), cy + hl * math.sin(a))
            p2 = (
                cx - hl * 0.4 * math.cos(a) + hw * math.sin(a),
                cy - hl * 0.4 * math.sin(a) - hw * math.cos(a),
            )
            p3 = (
                cx - hl * 0.4 * math.cos(a) - hw * math.sin(a),
                cy - hl * 0.4 * math.sin(a) + hw * math.cos(a),
            )
            C.d.polygon([tip, p2, p3], fill=color + (255,))
    if label:
        # 沿线取点
        total = sum(
            math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(flat[:-1], flat[1:])
        )
        tgt = total * lpos
        acc = 0
        lx, ly = flat[0]
        for a, b in zip(flat[:-1], flat[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if acc + L >= tgt:
                f = (tgt - acc) / (L or 1)
                lx, ly = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                break
            acc += L
        lx = lx / S + label_offset[0]
        ly = ly / S + label_offset[1]
        lines = label.split("\n")
        wmax = max(C.tlen(s, font_) for s in lines)
        lh = font_.size / S + 6
        hh = lh * len(lines) + 10
        if tag == "right":
            pass
        C.rrect(
            (lx - wmax / 2 - 8, ly - hh / 2, lx + wmax / 2 + 8, ly + hh / 2),
            8,
            label_bg,
            None,
        )
        for i, s in enumerate(lines):
            C.text(
                (lx, ly - hh / 2 + 5 + lh * i + lh / 2 - 2),
                s,
                font_,
                color,
                anchor="mm",
            )


# ============================== 标题 ==============================
C.text(
    (W / 2 + 5, 100), "secpt 渗透智能体系统架构", F_TITLE, (158, 176, 222), anchor="mm"
)
C.text((W / 2, 94), "secpt 渗透智能体系统架构", F_TITLE, INK, anchor="mm")
C.text(
    (W / 2, 150),
    "多客户端 · Agentic Loop · 战术知识记忆 · SQLite 状态库 · 授权守卫与复盘自进化",
    F_SUB,
    (66, 92, 138),
    anchor="mm",
)


def deco_slash(x, y, color):
    for i in range(3):
        C.polygon(
            [
                (x + i * 17, y),
                (x + i * 17 + 8, y),
                (x + i * 17 - 11, y + 42),
                (x + i * 17 - 19, y + 42),
            ],
            color,
        )


deco_slash(120, 42, BLUE_M)
deco_slash(1740, 42, BLUE_M)

# ============================== 顶部：多客户端（左） / Agentic Loop（中） / 支撑（右） ==============================
card_rect((60, 180, 720, 590), 26, BLUE_L)
C.rrect((50, 172, 300, 224), 14, BLUE_M, None)
C.text((70, 198), "多客户端", F_LABEL, WHITE, anchor="lm")

clients = [
    ("TUI", "monitor", "终端交互 / 批量任务"),
    ("Web", "globe", "本地或内网 Web 控制台"),
    ("桌面端", "monitor", "Electron 工作台"),
    ("SDK / API", "code", "CI、评测、SOC 编排"),
    ("Bot 通道", "chat", "钉钉 / 飞书 / 企业微信"),
    ("远程遥控", "phone", "手机接管会话"),
]
cw, chh = 308, 124
for i, (name, ic, desc) in enumerate(clients):
    col, row = i % 2, i // 2
    x = 82 + col * (cw + 16)
    y = 244 + row * (chh + 16)
    card_rect((x, y, x + cw, y + chh), 16, BLUE_M)
    icon(x + 14, y + 16, ic, BLUE_D, 0.78)
    C.text((x + 62, y + 32), name, F_BOX, INK, anchor="lm")
    C.text((x + 16, y + 92), desc, F_TINY, SLATE_M)

# Loop 大框
card_rect((780, 180, 1550, 590), 26, ORANGE_L)
C.text((806, 214), "Loop", F_BOX, (214, 84, 10), anchor="lm")
C.text((796, 252), "Agentic Loop：侦察→利用→提权→横向→取证", F_TINY, (196, 100, 30))

loop_items = [
    ("Terminal", "terminal", "Shell / SSH / 反弹会话"),
    ("Browser", "globe", "Playwright / Web 验证"),
    ("任务分发", "route", "Planner 拆 DAG 派子智能体"),
    ("定时任务", "clock2", "cron 攻击窗口 / 回归"),
    ("记忆管理", "memory", "战术记忆读写沉淀"),
    ("MCP 工具池", "plug", "nmap/nuclei/sqlmap/fscan"),
]
iw, ih = 232, 96
for i, (name, ic, desc) in enumerate(loop_items):
    col, row = i % 3, i // 3
    x = 802 + col * (iw + 10)
    y = 284 + row * (ih + 14)
    card_rect((x, y, x + iw, y + ih), 14, ORANGE_L)
    icon(x + 12, y + 10, ic, ORANGE_D, 0.54)
    C.text((x + 44, y + 22), name, F_SMALL, INK, anchor="lm")
    C.text((x + 12, y + 72), desc, F_ZOOM, (150, 85, 30))

# 定时任务下方的 cron 入口：小钟 → 定时任务卡片
icon(822, 494, "clock", ORANGE_D, 0.5)
C.text((846, 505), "cron", F_ZOOM, ORANGE_D, anchor="lm")
arrow([(830, 492), (830, 480), (862, 480), (862, 386)], ORANGE_D, 2.5)

# 任务分发 → Sub Agent（热力箭头）
for sy, sub in [(286, "侦察 / 枚举"), (402, "利用 / 横向")]:
    card_rect((1456, sy, 1644, sy + 88), 44, ORANGE_L)
    C.text((1550, sy + 30), "Sub Agent", F_SMALL, (190, 85, 10), anchor="mm")
    C.text((1550, sy + 60), sub, F_ZOOM, (170, 95, 40), anchor="mm")
arrow([(1368, 332), (1452, 332)], ORANGE_D, 2.5)
arrow([(1368, 448), (1452, 448)], ORANGE_D, 2.5)

# tool calls / rag（Loop → LLM / 记忆）
C.text((1155, 236), "tool calls / rag ↑", F_ZOOM, (190, 95, 25), anchor="mm")

# 右侧支撑列：授权网关 / 目标环境
card_rect((1740, 180, 1940, 590), 20, RED_L)
icon(1756, 200, "shield", RED_D, 0.66)
C.text((1800, 220), "授权与安全网关", F_ZOOM, RED_D, anchor="lm")
C.text((1756, 264), "范围守卫：域名/IP/端口", F_TINY, (110, 40, 70))
C.text((1756, 296), "速率窗口：qps / 并发", F_TINY, (110, 40, 70))
C.text((1756, 328), "高风险确认：凭据/关停", F_TINY, (110, 40, 70))
C.text((1756, 360), "隐蔽度闸门：噪音分档", F_TINY, (110, 40, 70))
C.text((1756, 392), "全量审计：命令可回溯", F_TINY, (110, 40, 70))
C.d.line(
    [C.g(1756), C.g(422), C.g(1928), C.g(422)],
    fill=mix(RED_D, WHITE, 0.55),
    width=int(2 * S),
)
icon(1756, 436, "net", TEAL_D, 0.56)
C.text((1792, 452), "目标环境", F_ZOOM, TEAL_D, anchor="lm")
C.text((1756, 492), "靶场 / SRC / 云与容器", F_TINY, (30, 110, 110))
C.text((1756, 520), "SSH · WSL · Docker 工作区", F_TINY, (30, 110, 110))
C.text((1756, 548), "只读取证 · 证据快照", F_TINY, (30, 110, 110))
arrow([(1560, 530), (1600, 530), (1610, 560), (1750, 560)], TEAL_D, 2.5)

# 客户端 → Loop 的 session 箭头
arrow([(330, 592), (330, 652), (1260, 652), (1260, 614)], BLUE_D, 5)
C.text((282, 668), "session", F_EDGE, BLUE_D, anchor="lm")

# ============================== 中段：上下文 / LLM（虚线框） ==============================
C.d.rounded_rectangle(
    [C.g(90), C.g(644), C.g(1940), C.g(1100)],
    radius=C.g(26),
    outline=BLUE_L + (255,),
    width=int(3 * S),
)
C.d.rounded_rectangle(
    [C.g(94), C.g(648), C.g(1936), C.g(1096)],
    radius=C.g(24),
    outline=(236, 245, 255, 255),
    width=int(6 * S),
)

prompt_items = [
    ("user prompt", "目标 / 授权范围", "human", 700),
    ("sys prompt", "角色 / ROE / 禁止清单", "code", 800),
    ("chat history", "历史对话 / 命令回显", "chat", 870),
]
for i, (name, desc, ic, tar) in enumerate(prompt_items):
    y = 676 + i * 132
    card_rect((120, y, 380, y + 112), 14, GREEN_L)
    icon(138, y + 58, ic, GREEN_D, 0.38)
    C.text((164, y + 30), name, F_EN, GREEN_D, anchor="lm")
    C.text((190, y + 74), desc, F_ZOOM, (40, 120, 90), anchor="lm")
    tx = 480
    arrow([(384, y + 56), (tx, y + 56), (tx, tar), (496, tar)], GREEN_D, 3)

# working memory
card_rect((500, 720, 770, 910), 18, BLUE_L)
icon(522, 756, "db", BLUE_D, 1.4)
C.text((530, 858), "working", F_BOX, BLUE_D, anchor="lm")
C.text((530, 892), "memory", F_BOX, BLUE_D, anchor="lm")

# LLM
card_rect((860, 662, 1350, 966), 22, BLUE_L)
icon(888, 676, "bot", BLUE_D, 0.95)
C.text((948, 700), "LLM", F_BOX, BLUE_D, anchor="lm")
C.text((888, 744), "推理强度可调（低 / 高 / 最高）", F_ZOOM, SLATE_M, anchor="lm")
models = [
    ("GLM-5.3", (40, 110, 180)),
    ("DeepSeek", (40, 120, 190)),
    ("Claude", (186, 108, 70)),
    ("Kimi", (30, 30, 30)),
    ("Qwen", (90, 60, 180)),
    ("MiniMax", (170, 60, 140)),
    ("私有化模型", (60, 120, 90)),
]
for i, (name, col) in enumerate(models):
    r, c_ = i // 3, i % 3
    x = 886 + c_ * 148
    y = 780 + r * 58
    card_rect((x, y, x + 138, y + 46), 10, (225, 232, 245))
    C.text((x + 69, y + 23), name, F_ZOOM, col, anchor="mm")

# 回复
card_rect((1370, 740, 1560, 880), 16, TEAL_L)
icon(1392, 774, "db", TEAL_D, 1.0)
C.text((1446, 808), "回复", F_LABEL, TEAL_D, anchor="lm")
C.text((1446, 842), "报告 / POC", F_ZOOM, (30, 110, 110), anchor="lm")

arrow([(774, 815), (856, 815)], BLUE_D, 4)
arrow([(1354, 815), (1366, 815)], BLUE_D, 4)
arrow([(1564, 812), (1680, 812), (1680, 652)], BLUE_D, 1, head=False)

# rag 双向（Loop ↔ LLM）
arrow([(1100, 606), (1100, 658)], ORANGE_D, 3)
arrow([(1160, 658), (1160, 606)], GREEN_D, 3)
C.text(
    (1130, 632),
    "RAG",
    F_EDGE,
    (40, 60, 90),
    anchor="mm",
)
# RAG / sql（working memory → LLM）
arrow([(774, 778), (856, 778)], PURPLE_D, 3)
C.rrect((784, 738, 846, 768), 5, WHITE, None)
C.text((815, 769), "RAG", F_ZOOM, PURPLE_D, anchor="mm")
arrow([(774, 852), (856, 852)], PURPLE_D, 3)
C.rrect((784, 888, 846, 918), 5, WHITE, None)
C.text((815, 903), "sql", F_ZOOM, PURPLE_D, anchor="mm")

# ============================== 记忆层 ==============================
mem_cards = [
    (
        100,
        "程序记忆",
        "memory",
        ["技能与攻击习惯（SKILL）", "工具链偏好 / 跳步经验"],
        "~/.pentest/skills/*.md",
        BLUE_D,
        BLUE_L,
    ),
    (
        600,
        "语义记忆",
        "memory",
        ["漏洞语义 / ATT&CK 映射", "指纹 → 利用链知识库"],
        "~/.pentest/memory/MEMORY.md",
        TEAL_D,
        TEAL_L,
    ),
    (
        1100,
        "情景记忆",
        "clock2",
        ["历史渗透事件与战果", "时间序列 / 目标档案"],
        "SQLite episodes（FTS5 索引）",
        INDIGO_D,
        INDIGO_L,
    ),
]
for x, name, ic, bullets, path, cold, coll in mem_cards:
    card_rect((x, 1180, x + 460, 1490), 18, coll)
    icon(x + 28, 1200, ic, cold, 0.78)
    C.text((x + 84, 1224), name, F_BOX, INK, anchor="lm")
    C.lines(x + 30, 1280, ["· " + b for b in bullets], F_SMALL, (60, 75, 105), 40)
    C.text((x + 30, 1416), path, F_TINY, cold)
    C.d.line(
        [C.g(x + 22), C.g(1452), C.g(x + 438), C.g(1452)],
        fill=mix(cold, WHITE, 0.55),
        width=int(2 * S),
    )

# working memory ⇄ 记忆层（关键字 / topic）
arrow([(576, 1176), (576, 950), (576, 914)], BLUE_D, 3)
C.rrect((528, 1040, 636, 1096), 8, WHITE, None)
C.text((582, 1054), "关键字", F_ZOOM, BLUE_D, anchor="mm")
C.text((582, 1080), "topic", F_ZOOM, BLUE_D, anchor="mm")
C.text((648, 1102), "语义检索 / 相关性筛选", F_ZOOM, SLATE_M, anchor="lm")
# skill.md 注入
arrow([(780, 914), (780, 962), (440, 962), (440, 1176)], BLUE_D, 3)
C.text((848, 962), "skill.md 注入", F_ZOOM, BLUE_D, anchor="lm")

# ============================== SQLite 状态库 ==============================
card_rect((1600, 1180, 1940, 1340), 18, BLUE_L)
icon(1624, 1198, "db", BLUE_D, 1.1)
C.text((1680, 1236), "SQLite", F_BOX, BLUE_D, anchor="lm")
C.text((1624, 1282), "state.db（FTS5 全文索引）", F_ZOOM, SLATE_M)
C.text((1624, 1310), "目标 · 凭证 · 会话 · 攻击图谱 · 证据", F_ZOOM, SLATE_M)

# 回复 → 历史保存 → SQLite
arrow(
    [(1660, 884), (1660, 1176)],
    BLUE_D,
    3,
    label="历史保存",
    lpos=0.35,
    label_offset=(60, 0),
    font_=F_ZOOM,
)
# 情景记忆 → SQLite
arrow([(1564, 1305), (1596, 1305)], INDIGO_D, 3)
# SQLite → 知识整合门控
arrow([(1740, 1344), (1740, 1842)], BLUE_D, 3)

# ============================== 知识整合门控 ==============================
gx, gy = 1740, 1860
C.polygon(
    [(gx, gy), (gx + 185, gy + 118), (gx, gy + 236), (gx - 185, gy + 118)], PURPLE_L
)
C.polygon(
    [(gx, gy + 14), (gx + 169, gy + 118), (gx, gy + 222), (gx - 169, gy + 118)],
    (237, 230, 255),
)
C.text((gx, gy + 102), "知识整合门控", F_LABEL, PURPLE_D, anchor="mm")
C.text((gx, gy + 142), "去重 · 冲突检测", F_TINY, PURPLE_D, anchor="mm")
C.text((gx, gy + 168), "证据校验 · 人工审核", F_TINY, PURPLE_D, anchor="mm")

# ============================== 核心 Agent ==============================
card_rect((660, 1590, 1100, 1750), 26, BLUE_D)
C.text((700, 1632), "Pentest Agent", F_BOX, WHITE, anchor="lm")
C.text(
    (700, 1674),
    "核心推理引擎：Planner + Executor + Critic",
    F_ZOOM,
    (215, 230, 255),
    anchor="lm",
)
C.text(
    (700, 1706),
    "任务拆解 · 工具调度 · 自我反思 · 停止条件",
    F_ZOOM,
    (215, 230, 255),
    anchor="lm",
)
# 核心 Agent ⇄ 上下文 / 记忆（沿 y=1550 通道，在门控与写入线下方）
arrow([(1040, 1590), (1040, 1550), (510, 1550), (510, 1113)], BLUE_D, 3)
C.text((540, 1532), "调度记忆 / 选模型", F_ZOOM, SLATE_M, anchor="lm")

# 核心 Agent → 授权与安全网关 → 工具执行
card_rect((1140, 1600, 1500, 1940), 18, RED_L)
icon(1162, 1616, "shield", RED_D, 0.7)
C.text((1214, 1638), "授权与安全网关", F_SMALL, RED_D, anchor="lm")
gate_items = [
    ("范围守卫", "scope", "域名 / IP / 端口白名单"),
    ("速率与窗口", "clock", "每目标 qps、并发上限"),
    ("高风险确认", "seal", "凭据窃取 / 关停需人工"),
    ("隐蔽度闸门", "gear", "噪音等级与免杀强度分档"),
    ("全量审计", "report", "逐条命令留存，可回溯"),
]
for i, (name, ic, desc) in enumerate(gate_items):
    y = 1666 + i * 52
    C.rrect((1154, y, 1486, y + 44), 9, WHITE, mix(RED_D, WHITE, 0.6), 1.5)
    icon(1162, y + 6, ic, RED_D, 0.33)
    C.text((1194, y + 22), name, F_ZOOM, RED_D, anchor="lm")
    C.text((1286, y + 22), desc, F_TINY, (90, 60, 80), anchor="lm")

arrow([(1104, 1662), (1136, 1662)], RED_D, 3)
C.text((1120, 1638), "guard", F_ZOOM, RED_D, anchor="mm")
arrow(
    [
        (1504, 1662),
        (1600, 1662),
        (1600, 700),
        (1560, 700),
        (1560, 580),
        (1700, 560),
        (1726, 520),
    ],
    RED_D,
    2.5,
)

# ============================== 底部：复盘与自进化 ==============================
# 判断清单
C.rrect((90, 2010, 570, 2240), 18, WHITE, mix(PURPLE_D, WHITE, 0.5), 2)
C.text((110, 2038), "判断本轮是否产生了：", F_SMALL, INK)
checks = [
    "新攻击链与绕过技巧",
    "可复用的漏洞指纹规则",
    "失败的尝试与误报教训",
    "对旧 SKILL 的修正",
]
for i, s in enumerate(checks):
    yy = 2088 + i * 36
    C.d.line(
        [(C.g(114), C.g(yy + 3)), (C.g(121), C.g(yy + 11)), (C.g(134), C.g(yy))],
        fill=GREEN_D,
        width=int(3 * S),
    )
    C.text((148, yy), s, F_SMALL, (60, 75, 105), anchor="lm")

# Background Review Agent
card_rect((600, 2020, 1040, 2150), 22, (233, 213, 255))
icon(622, 2042, "target", PURPLE_D, 0.66)
C.text((672, 2062), "Background Review Agent", F_SMALL, PURPLE_D, anchor="lm")
C.text((622, 2104), "复盘：得手路径 / 失败原因 / 误报剔除", F_ZOOM, (90, 60, 140))

arrow([(574, 2125), (596, 2125)], PURPLE_D, 3)
# 会话日志 → Review Agent（SQLite 右侧长回路）
arrow(
    [(1900, 1344), (1900, 2250), (1060, 2250), (1060, 2154)],
    PURPLE_D,
    2.5,
    label="会话日志 / 回显 / 证据",
    lpos=0.3,
    label_offset=(0, -20),
    font_=F_ZOOM,
)
# Review Agent ⇄ 核心 Agent
arrow([(820, 2016), (820, 1752)], PURPLE_D, 3)
C.text((838, 1890), "自我反思 / 重规划", F_ZOOM, PURPLE_D)
# 提炼事实 → 门控
arrow(
    [(1044, 2085), (1290, 2085), (1290, 2020), (1670, 2020), (1670, 1996)],
    PURPLE_D,
    3,
    label="提炼事实 / 规则",
    lpos=0.5,
    label_offset=(0, -20),
    font_=F_ZOOM,
)
# 门控 → 记忆（写入），走左侧 x=1606 通道
arrow(
    [(1675, 1880), (1606, 1880), (1606, 1536), (1533, 1536), (1533, 1496)],
    PURPLE_D,
    3,
    label="写入 / 更新",
    lpos=0.5,
    label_offset=(58, 0),
    font_=F_ZOOM,
)
# 自进化回流：Review Agent → SKILL 层
arrow([(640, 2154), (640, 2300), (130, 2300), (130, 1496)], GREEN_D, 4)
C.rrect((250, 2262, 470, 2318), 8, WHITE, None)
C.text((360, 2290), "自行创建 / 改进 SKILL", F_ZOOM, GREEN_D, anchor="mm")
C.text(
    (873, 2205), "→ 复盘结论回流 Loop，进入下一轮迭代", F_ZOOM, ORANGE_D, anchor="mm"
)

# 图例
C.rrect((1180, 2320, 1950, 2440), 14, WHITE, mix(SLATE_M, WHITE, 0.5), 1.5)
legend = [
    ("客户端", BLUE_D),
    ("Agentic Loop / 工具", ORANGE_D),
    ("上下文与 LLM", GREEN_D),
    ("记忆与状态", INDIGO_D),
    ("复盘自进化", PURPLE_D),
    ("安全约束", RED_D),
]
for i, (s, col) in enumerate(legend):
    x = 1200 + (i % 2) * 360
    y = 2352 + (i // 2) * 40
    C.d.rounded_rectangle(
        [C.g(x), C.g(y - 8), C.g(x + 30), C.g(y + 8)], radius=C.g(4), fill=col
    )
    C.text((x + 40, y), s, F_ZOOM, INK, anchor="lm")
C.text(
    (1200, 2422),
    "核心原则：一切攻击动作先过授权与安全网关；全程留痕，可复现、可审计。",
    F_TINY,
    SLATE_M,
)

C.text(
    (W - 30, H - 18),
    "secpt Architecture · 仅限授权范围内的安全测试",
    F_TINY,
    (140, 155, 180),
    anchor="rs",
)

out = C.img.convert("RGB")
out = out.resize((W, H), Image.LANCZOS)
os.makedirs(os.path.dirname(os.path.abspath(__file__)), exist_ok=True)
target = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pentest_agent_arch.png"
    )
)
out.save(target, "PNG")
print("saved", target, out.size)
