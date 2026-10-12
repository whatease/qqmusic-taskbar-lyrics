# -*- coding: utf-8 -*-
"""卡拉OK歌词渲染器(Pillow 直出 RGBA 帧,配合 UpdateLayeredWindow)。

效果:
  * 已播放=粉色、未播放=橘色,柔边过渡;
  * 暗色描边 + 柔和辉光,深浅色任务栏下都清晰;
  * 行宽超出可用空间时先缩字号、再跟随卡拉OK光标平滑滚动;
  * 换行交叉淡入淡出;纯标题模式(无歌词/间奏)用双色点缀。

帧全部为"直 alpha",由窗口层在提交前统一做预乘。
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("renderer")

_FONT_FILES = {
    "microsoft yahei ui": ["msyhbd.ttc", "msyh.ttc"],
    "microsoft yahei": ["msyhbd.ttc", "msyh.ttc"],
    "msyh": ["msyhbd.ttc", "msyh.ttc"],
    "simhei": ["simhei.ttf"],
    "heiti": ["simhei.ttf"],
    "dengxian": ["Dengb.ttf", "Deng.ttf"],
    "segoe ui": ["segoeuib.ttf", "segoeui.ttf"],
    "segoe ui symbol": ["seguisym.ttf"],     # 含 ♪ 等符号字形
}
_SYMBOL_FAMILIES = ["Segoe UI Symbol", "Segoe UI Emoji", "Microsoft YaHei UI"]
_OUTLINE_RGB = (24, 20, 28)
_NOTE_PREFIX = "♪"


def _hex_rgb(value: str | None, fallback):
    try:
        v = (value or "").strip().lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _font_path(family: str) -> Path | None:
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in _FONT_FILES.get(family.strip().lower(), [family]):
        p = fonts_dir / name
        if p.exists():
            return p
    return None


class _Line:
    """当前行的一切缓存(掩膜/辉光/字宽),与窗口宽度无关。"""
    __slots__ = ("text", "font", "size", "canvas_w", "tw", "pad", "glyph",
                 "glow", "outline", "char_x", "scrolled", "static_layer",
                 "orange_layer", "pink_layer")


class KaraokeRenderer:
    def __init__(self, config):
        self.cfg = config
        self.W = self.H = 0
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self._line: _Line | None = None
        self._sig = None
        self._scroll = 0.0
        self._last_t = time.monotonic()

    # ── 对外 ────────────────────────────────────────────
    def set_size(self, w: int, h: int) -> None:
        if h != self.H:
            # 高度变化:字号/掩膜全部重建
            self.W, self.H = w, h
            self._fonts.clear()
            self._line = None
            self._sig = None
            self._scroll = 0.0
        elif w != self.W:
            # 仅宽度变化(平滑滑动动画):掩膜与字体保持,滚动模式在 draw 中复算
            self.W = w
            self._sig = None

    def invalidate(self) -> None:
        self._fonts.clear()
        self._line = None
        self._sig = None

    def draw(self, text: str, cursor_frac: float | None, fade: float = 1.0):
        """返回 RGBA 帧;画面无变化时返回 None(省CPU)。"""
        if not text:
            return None
        if self._line is None or self._line.text != text:
            self._build_line(text)
            self._scroll = 0.0
        line = self._line
        if line is None:
            return None

        now = time.monotonic()
        dt = min(0.1, now - self._last_t)
        self._last_t = now

        W, H, tw, pad = self.W, self.H, line.tw, line.pad
        margin = max(4, int(self.cfg.get("margin_x", 12) * self._scale()))
        usable = max(60, W - 2 * margin)
        if line.scrolled != (tw > usable):
            # 窗口宽度跨过了"放得下/放不下"的阈值,按新宽度重排版
            self._build_line(text)
            line = self._line
            if line is None:
                return None

        # 光标位置与滚动
        if cursor_frac is None:
            cursor_px = None
        else:
            cursor_px = max(0.0, min(tw, tw * cursor_frac))
        if line.scrolled:
            target = 0.0 if cursor_px is None else \
                max(0.0, min(tw - usable, cursor_px - usable * 0.45))
            k = 1.0 - math.exp(-dt * 6.0)
            self._scroll += (target - self._scroll) * k
            dx = margin - int(self._scroll) - pad
        else:
            self._scroll = 0.0
            dx = (W - tw) // 2 - pad

        # 帧签名(亚像素变化不重绘)
        sig = (text, cursor_px, int(self._scroll), round(fade, 2), W, H,
               self.cfg.get("played_color"), self.cfg.get("unplayed_color"))
        if sig == self._sig:
            return None
        self._sig = sig

        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        frame.alpha_composite(line.glow, (dx, 0))
        frame.alpha_composite(line.outline, (dx, 0))

        if cursor_px is None:
            text_layer = line.static_layer
        else:
            cx = cursor_px + pad
            soft = max(2, int(self.cfg.get("soft_edge_px", 6) * self._scale()))
            ramp = self._ramp_mask(line.canvas_w, H, cx, soft)
            pink_alpha = ImageChops.darker(line.glyph, ramp)
            text_layer = line.orange_layer.copy()
            text_layer.alpha_composite(self._tinted(line.pink_layer, pink_alpha))
        if fade < 0.999:
            a = text_layer.getchannel("A").point(lambda v: int(v * fade))
            text_layer = text_layer.copy()
            text_layer.putalpha(a)
        frame.alpha_composite(text_layer, (dx, 0))
        return frame

    # ── 内部 ────────────────────────────────────────────
    def _scale(self) -> float:
        return max(0.8, self.H / 48.0) if self.H else 1.0

    def _font_at(self, size: int):
        f = self._fonts.get(size)
        if f is None:
            path = None
            for family in self.cfg.get("font_family", ["Microsoft YaHei UI"]):
                path = _font_path(family)
                if path:
                    break
            if path is None:
                path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
            try:
                f = ImageFont.truetype(str(path), size, index=0)
            except Exception:
                f = ImageFont.load_default()
            self._fonts[size] = f
        return f

    def _symbol_font_at(self, size: int):
        """含 ♪ 字形的符号字体(雅黑没有该字形,会渲染成方块)。"""
        f = self._fonts.get(("sym", size))
        if f is None:
            f = None
            for family in _SYMBOL_FAMILIES:
                path = _font_path(family)
                if path:
                    try:
                        f = ImageFont.truetype(str(path), size, index=0)
                        break
                    except Exception:
                        f = None
            if f is None:
                f = self._font_at(size)
            self._fonts[("sym", size)] = f
        return f

    def _measure(self, font, size: int, text: str) -> float:
        """文本宽度;♪ 用符号字体量。"""
        if text.startswith(_NOTE_PREFIX):
            return (self._symbol_font_at(size).getlength(_NOTE_PREFIX)
                    + font.getlength(text[len(_NOTE_PREFIX):]))
        return font.getlength(text)

    def _draw_segments(self, draw, x, y, size: int, main_font, text: str, **kw):
        """分段绘制:♪ 用符号字体,其余用主字体。"""
        if text.startswith(_NOTE_PREFIX):
            sym_font = self._symbol_font_at(size)
            draw.text((x, y), _NOTE_PREFIX, font=sym_font, **kw)
            rest = text[len(_NOTE_PREFIX):]
            draw.text((x + sym_font.getlength(_NOTE_PREFIX), y), rest,
                      font=main_font, **kw)
        else:
            draw.text((x, y), text, font=main_font, **kw)

    def _build_line(self, text: str) -> None:
        H = self.H
        margin = max(4, int(self.cfg.get("margin_x", 12) * self._scale()))
        usable = max(60, self.W - 2 * margin)
        base = max(12, int(H * self.cfg.get("font_scale", 0.6)))
        minsz = max(10, int(H * self.cfg.get("min_font_scale", 0.42)))

        size, font = base, self._font_at(base)
        while size > minsz and self._measure(font, size, text) > usable:
            size -= 2
            font = self._font_at(size)

        ln = _Line()
        ln.text = text
        ln.font = font
        ln.size = size
        ln.tw = math.ceil(self._measure(font, size, text))
        ln.pad = int(H * 0.22)
        ln.canvas_w = ln.tw + 2 * ln.pad
        ln.scrolled = ln.tw > usable

        ascent, descent = font.getmetrics()
        baseline = (H - (ascent + descent)) // 2 + ascent + int(self.cfg.get("vertical_offset", 0))
        baseline = max(ascent, min(H - 2, baseline))

        glyph = Image.new("L", (ln.canvas_w, H), 0)
        self._draw_segments(ImageDraw.Draw(glyph), ln.pad, baseline,
                            size, font, text, fill=255, anchor="ls")
        ln.glyph = glyph

        stroke_w = max(1, round(self.cfg.get("outline_width", 1.3) * self._scale()))
        stroke = Image.new("L", glyph.size, 0)
        self._draw_segments(ImageDraw.Draw(stroke), ln.pad, baseline,
                            size, font, text, fill=255, anchor="ls",
                            stroke_width=stroke_w, stroke_fill=255)

        glow_alpha = stroke.filter(ImageFilter.GaussianBlur(H * 0.09))
        strength = float(self.cfg.get("glow_strength", 0.85))
        glow_alpha = glow_alpha.point(lambda v: int(v * strength))
        ln.glow = self._tint_mask((0, 0, 0), glow_alpha)
        ln.outline = self._tint_mask(_OUTLINE_RGB, stroke)

        played = _hex_rgb(self.cfg.get("played_color"), (255, 126, 179))
        unplayed = _hex_rgb(self.cfg.get("unplayed_color"), (255, 163, 71))
        title_rgb = (_hex_rgb(self.cfg.get("title_color"), None)
                     or _hex_rgb(self.cfg.get("unplayed_color"), (255, 163, 71)))
        ln.orange_layer = self._tint_mask(unplayed, glyph)
        ln.pink_layer = self._tint_mask(played, glyph)
        ln.static_layer = self._tint_mask(title_rgb, glyph)
        self._line = ln

    @staticmethod
    def _tint_mask(rgb, mask: Image.Image) -> Image.Image:
        """直alpha着色层:RGB恒定,Alpha=掩膜。"""
        r = Image.new("L", mask.size, rgb[0])
        g = Image.new("L", mask.size, rgb[1])
        b = Image.new("L", mask.size, rgb[2])
        return Image.merge("RGBA", (r, g, b, mask))

    def _tinted(self, layer: Image.Image, new_alpha: Image.Image) -> Image.Image:
        r, g, b, _ = layer.split()
        return Image.merge("RGBA", (r, g, b, new_alpha))

    @staticmethod
    def _ramp_mask(width: int, height: int, center: float, soft: int) -> Image.Image:
        """水平软边阶梯(粉↔橘过渡):1×宽生成,就近放大到全高。"""
        lo, hi = center - soft, center + soft
        row = bytearray(width)
        for x in range(max(0, int(lo))):
            row[x] = 255
        for x in range(max(0, int(lo)), min(width, int(math.ceil(hi)))):
            t = (x - lo) / max(1.0, hi - lo)
            row[x] = int(255 * min(1.0, max(0.0, 1.0 - t)))
        ramp = Image.frombytes("L", (width, 1), bytes(row))
        return ramp.resize((width, height), Image.NEAREST)
