# -*- coding: utf-8 -*-
"""分层窗口公共设施:PIL RGBA 帧 → 每像素透明窗口(UpdateLayeredWindow)。

供主歌词条与 QQ音乐"桌"按钮两个窗口复用。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging

from PIL import Image, ImageChops

log = logging.getLogger("layered")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


# ── 64 位句柄安全:显式 restype/argtypes ─────────────────────
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, ctypes.c_void_p]
user32.CreateIconIndirect.restype = ctypes.c_void_p
user32.CreateIconIndirect.argtypes = [ctypes.c_void_p]
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, ctypes.c_void_p, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    ctypes.c_void_p, ctypes.POINTER(POINT), wt.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wt.UINT]
user32.SetTimer.restype = wt.BOOL
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.CreateBitmap.restype = ctypes.c_void_p
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT,
                               ctypes.c_void_p]


def create_dib(size: tuple[int, int]) -> tuple[int, int]:
    """创建 32bpp 顶朝下 DIB,返回 (hbmp, bits指针)。"""
    w, h = size
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(user32.GetDC(0), ctypes.byref(bmi), 0,
                                  ctypes.byref(bits), None, 0)
    if not hbmp or not bits:
        raise OSError("CreateDIBSection 失败")
    return hbmp, bits


class LayeredWindow:
    """把 PIL RGBA 帧提交为分层窗口(自动建面/预乘/BGRA 转序)。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.screen_dc = user32.GetDC(0)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        self._bmp = None
        self._bits = None
        self._size = (0, 0)

    def update(self, img: Image.Image) -> None:
        w, h = img.size
        if (w, h) != self._size:
            self._create_surface(w, h)
        # 预乘 alpha 并转为 DIB 的 BGRA 字节序
        r, g, b, a = img.split()
        prem = Image.merge("RGBA", (ImageChops.multiply(r, a),
                                    ImageChops.multiply(g, a),
                                    ImageChops.multiply(b, a), a))
        ctypes.memmove(self._bits, prem.tobytes("raw", "BGRA"), w * h * 4)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        size = SIZE(w, h)
        src = POINT(0, 0)
        if not user32.UpdateLayeredWindow(
                self.hwnd, self.screen_dc, None, ctypes.byref(size),
                self.mem_dc, ctypes.byref(src), 0,
                ctypes.byref(blend), ULW_ALPHA):
            log.debug("UpdateLayeredWindow 失败 err=%d", ctypes.GetLastError())

    def _create_surface(self, w: int, h: int) -> None:
        hbmp, bits = create_dib((w, h))
        old = gdi32.SelectObject(self.mem_dc, hbmp)
        if self._bmp:
            gdi32.DeleteObject(self._bmp)
        self._bmp, self._bits, self._size = hbmp, bits, (w, h)

    def close(self) -> None:
        if self._bmp:
            gdi32.DeleteObject(self._bmp)
            self._bmp = None
        gdi32.DeleteDC(self.mem_dc)
        user32.ReleaseDC(0, self.screen_dc)
