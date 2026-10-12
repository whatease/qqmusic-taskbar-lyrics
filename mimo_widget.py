# -*- coding: utf-8 -*-
"""MiMo 余额/今日用量 任务栏挂件 — 渲染。

窗口生命周期由主程序管理(与歌词条同样挂为 Shell_TrayWnd 子窗口),
本模块只负责把 MimoWorker 的状态画成 RGBA 帧:
  行1  余额 ¥57.43            (绿,主体信息)
  行2  今日 17.3M · ¥2.15     (灰,当日 Token 与消费)
未登录/异常时显示引导文案。左侧一条细分隔线与歌词条视觉分区。
"""
from __future__ import annotations

import logging

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("mimo.render")

_COLOR_BALANCE = (139, 233, 160)     # 余额绿
_COLOR_SUB = (208, 208, 208)         # 次要信息灰
_COLOR_WARN = (255, 176, 96)         # 需登录/异常
_COLOR_ERR = (255, 128, 128)
_OUTLINE = (16, 16, 16)

_FONT_CANDIDATES = [
    ("msyh.ttc", 0),      # Microsoft YaHei
    ("msyhbd.ttc", 0),
    ("segoeui.ttf", None),
    ("simhei.ttf", None),
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for name, idx in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size,
                                      index=idx if idx is not None else 0)
        except Exception:
            continue
    return ImageFont.load_default()


def fmt_tokens(n: int) -> str:
    """17297288 -> '17.3M'"""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class MimoRenderer:
    """按 (状态, 数据, 尺寸) 缓存渲染结果;内容没变就不重画。"""

    def __init__(self):
        self.size = (0, 0)
        self._cache_key = None
        self._frame: Image.Image | None = None

    def set_size(self, w: int, h: int) -> None:
        if (w, h) != self.size:
            self.size = (w, h)
            self.invalidate()

    def invalidate(self) -> None:
        self._cache_key = None
        self._frame = None

    def draw(self, state: str, data: dict | None, message: str = "") -> Image.Image | None:
        w, h = self.size
        if w < 24 or h < 16:
            return None
        key = (state, None if data is None else
               (data.get("balance"), data.get("today_tokens"),
                data.get("today_cost")), message, (w, h))
        if key == self._cache_key and self._frame is not None:
            return self._frame
        img = self._render(state, data, message)
        self._cache_key, self._frame = key, img
        return img

    # ── 实际绘制 ──
    def _render(self, state: str, data: dict | None, message: str) -> Image.Image:
        w, h = self.size
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 与左侧歌词条的分隔线
        draw.rectangle([0, int(h * 0.22), 0, int(h * 0.78)],
                       fill=(255, 255, 255, 60))

        f_main = _load_font(max(11, int(h * 0.36)))
        f_sub = _load_font(max(9, int(h * 0.27)))

        if state == "ok" and data:
            line1 = f"¥{data['balance']:.2f}"
            if data.get("gift_balance", 0) > 0:
                line1 += f" (+¥{data['gift_balance']:.2f})"
            line2 = (f"今日 {fmt_tokens(int(data.get('today_tokens') or 0))}"
                     f" · ¥{data.get('today_cost') or 0:.2f}")
            c1, c2 = _COLOR_BALANCE, _COLOR_SUB
        elif state in ("need_login", "fetching", "init"):
            line1 = "MiMo " + ("未登录" if state == "need_login" else "...")
            line2 = ("右键托盘图标登录" if state == "need_login"
                     else (message or "获取中"))
            c1, c2 = _COLOR_WARN, _COLOR_SUB
        else:  # error
            line1 = "MiMo --"
            line2 = message or "取数失败"
            c1, c2 = _COLOR_ERR, _COLOR_SUB

        # 挂件被收窄时自动缩字号,保证一行放得下(下限8px)
        limit = w - 10
        f_main = self._fit_font(draw, line1, f_main, limit)
        f_sub = self._fit_font(draw, line2, f_sub, limit)

        self._center_text(draw, w, h, line1, f_main, c1, y_frac=0.30)
        self._center_text(draw, w, h, line2, f_sub, c2, y_frac=0.68)
        return img

    @staticmethod
    def _fit_font(draw: ImageDraw.ImageDraw, text: str, font, limit: int):
        while getattr(font, "size", 8) > 8 and MimoRenderer._text_w(draw, font, text) > limit:
            font = _load_font(font.size - 1)
        return font

    @staticmethod
    def _text_w(draw: ImageDraw.ImageDraw, font, text: str) -> int:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0]
        except Exception:
            return len(text) * getattr(font, "size", 10)

    @staticmethod
    def _center_text(draw: ImageDraw.ImageDraw, w: int, h: int, text: str,
                     font, color, y_frac: float) -> None:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            return
        x = max(4, (w - tw) // 2 - bbox[0])
        y = int(h * y_frac) - th // 2
        # 描边保证浅色任务栏上也可读
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    draw.text((x + dx, y + dy), text, font=font,
                              fill=(*_OUTLINE, 230))
        draw.text((x, y), text, font=font, fill=(*color, 255))
