# -*- coding: utf-8 -*-
"""QQ音乐底栏歌词 — 主程序。

在任务栏"最后一个图标 ↔ 系统托盘"的空档处显示正在播放的歌词:
  * 已播放=粉色,未播放=橘色(卡拉OK式柔边推进)
  * 位置随任务栏图标数量变化自动调整,绝不挤占图标
  * 鼠标穿透,不影响任务栏任何操作;全屏应用时自动隐藏
  * SMTC 获取歌曲与进度;QQ音乐接口获取LRC歌词(带缓存)

用法:
  python taskbar_lyrics.py                # 正常运行
  python taskbar_lyrics.py --demo 晴天 周杰伦   # 演示模式(不依赖播放器)
  python taskbar_lyrics.py --debug        # 控制台日志
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path

import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw

import app_config
import karaoke_renderer
import lyrics_client
import media_watcher
import taskbar_geometry
from app_config import Config
from karaoke_renderer import KaraokeRenderer
from lyrics_client import LyricsClient
from media_watcher import MediaWatcher
from mimo_client import MimoWorker
from mimo_widget import MimoRenderer

APP_NAME = "QQ音乐底栏歌词"
CLASS_NAME = "QQMusicTaskbarLyricsWnd"
MUTEX_NAME = "Local\\QQMusicTaskbarLyrics.SingleInstance"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

WM_APP_GEOM = 0x8000 + 1
WM_APP_LYRICS = 0x8000 + 2
WM_APP_TRAY = 0x8000 + 4
WM_APP_CMD = 0x8000 + 5
WM_APP_MIMO = 0x8000 + 6

try:
    TASKBAR_CREATED_MSG = win32gui.RegisterWindowMessage("TaskbarCreated")
except Exception:
    TASKBAR_CREATED_MSG = 0

log = logging.getLogger("main")


# ══════════════════════════ 分层窗口提交 ══════════════════════════
from layered_window import (  # noqa: E402
    LayeredWindow, gdi32 as _gdi32, user32 as _user32)


# ══════════════════════════ 托盘图标 ══════════════════════════
def _make_tray_icon_image(size: int = 32) -> Image.Image:
    """粉橘渐变圆角底 + 白色音符。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    c1, c2 = (255, 126, 179, 255), (255, 163, 71, 255)
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=size // 5, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    s = size / 32.0
    d.ellipse([8 * s, 17 * s, 16 * s, 25 * s], fill=(255, 255, 255, 255))
    d.rectangle([14 * s, 6 * s, 16.4 * s, 21 * s], fill=(255, 255, 255, 255))
    d.polygon([(14 * s, 6 * s), (23 * s, 9 * s), (23 * s, 13 * s), (14 * s, 10 * s)],
              fill=(255, 255, 255, 255))
    return img


def _pil_to_hicon(img: Image.Image) -> int:
    from layered_window import create_dib
    w, h = img.size
    hbmp_color, bits = create_dib((w, h))
    ctypes.memmove(bits, img.tobytes("raw", "BGRA"), w * h * 4)
    hbmp_mask = _gdi32.CreateBitmap(w, h, 1, 1, None)

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                    ("yHotspot", wt.DWORD), ("hbmMask", ctypes.c_void_p),
                    ("hbmColor", ctypes.c_void_p)]

    ici = ICONINFO(True, 0, 0, hbmp_mask, hbmp_color)
    hicon = _user32.CreateIconIndirect(ctypes.byref(ici))
    _gdi32.DeleteObject(hbmp_color)
    _gdi32.DeleteObject(hbmp_mask)
    return hicon


class TrayIcon:
    MENU_TOGGLE = 1
    MENU_AUTOSTART = 2
    MENU_QUIT = 3
    MENU_MIMO = 4

    def __init__(self, hwnd: int, on_toggle, on_autostart, on_quit, on_mimo=None):
        self.hwnd = hwnd
        self.on_toggle, self.on_autostart, self.on_quit = on_toggle, on_autostart, on_quit
        self.on_mimo = on_mimo
        self.hicon = _pil_to_hicon(_make_tray_icon_image(32))
        self.added = False
        self.add()                     # 失败不抛异常:开机时任务栏可能未就绪

    def add(self) -> bool:
        """注册托盘图标;失败(如开机时 explorer 未就绪)返回 False,由心跳重试。"""
        if self.added:
            return True
        args = (self.hwnd, 0,
                win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP,
                WM_APP_TRAY, self.hicon, APP_NAME)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, args)
            self.added = True
            return True
        except Exception as exc:
            log.warning("托盘图标注册失败(将自动重试): %s", exc)
            return False

    def show_menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.MENU_TOGGLE, "显示/隐藏歌词")
        auto_on = get_autostart()
        flags = win32con.MF_STRING | (win32con.MF_CHECKED if auto_on else 0)
        win32gui.AppendMenu(menu, flags, self.MENU_AUTOSTART, "开机自启")
        if self.on_mimo is not None:
            win32gui.AppendMenu(menu, win32con.MF_STRING, self.MENU_MIMO,
                                "登录 MiMo 账号(余额挂件)")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.MENU_QUIT, "退出")
        win32gui.SetForegroundWindow(self.hwnd)
        pos = win32gui.GetCursorPos()
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON,
                                pos[0], pos[1], 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def handle_command(self, cmd: int) -> None:
        if cmd == self.MENU_TOGGLE:
            self.on_toggle()
        elif cmd == self.MENU_AUTOSTART:
            self.on_autostart()
        elif cmd == self.MENU_QUIT:
            self.on_quit()
        elif cmd == self.MENU_MIMO and self.on_mimo:
            self.on_mimo()

    def remove(self) -> None:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass

    def readd(self) -> None:
        """explorer 重启后托盘图标随旧任务栏一起消失,需重新注册。"""
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass
        self.added = False
        if self.add():
            log.info("托盘图标已重新注册")


def get_autostart() -> bool:
    try:
        key = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    win32con.KEY_QUERY_VALUE)
        try:
            win32api.RegQueryValueEx(key, APP_NAME)
            return True
        finally:
            win32api.RegCloseKey(key)
    except Exception:
        return False


def set_autostart(enable: bool) -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    cmd = f'"{exe}" "{Path(__file__).resolve()}"'
    try:
        key = win32api.RegOpenKeyEx(win32con.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    win32con.KEY_SET_VALUE)
        try:
            if enable:
                win32api.RegSetValueEx(key, APP_NAME, 0, win32con.REG_SZ, cmd)
            else:
                win32api.RegDeleteValue(key, APP_NAME)
        finally:
            win32api.RegCloseKey(key)
        log.info("开机自启 %s", "开启" if enable else "关闭")
    except Exception as exc:
        log.warning("设置开机自启失败: %s", exc)


# ══════════════════════════ 歌词后台线程 ══════════════════════════
class LyricsWorker(threading.Thread):
    def __init__(self, client: LyricsClient, notify_hwnd: int):
        super().__init__(daemon=True, name="lyrics-worker")
        self.client = client
        self.notify_hwnd = notify_hwnd
        self._queue: "list[tuple[str, str, str]]" = []
        self._lock = threading.Lock()
        self._pending: str | None = None
        self.result_key: str | None = None
        self.result_lyrics = None
        self.result_time = 0.0

    def request(self, key: str, title: str, artist: str) -> None:
        with self._lock:
            if self._pending == key:
                return
            if self.result_key == key:
                if self.result_lyrics is not None:
                    return
                # 拉取失败的结果60s后允许重试 —— 一次限流抖动不该让整首歌无歌词
                if time.monotonic() - self.result_time < 60:
                    return
            self._pending = key
        self._queue.append((key, title, artist))

    def get(self, key: str):
        with self._lock:
            return self.result_lyrics if self.result_key == key else None

    def run(self) -> None:
        while True:
            if not self._queue:
                time.sleep(0.1)
                continue
            key, title, artist = self._queue.pop(0)
            lyrics = self.client.fetch(key, title, artist)
            with self._lock:
                self.result_key, self.result_lyrics = key, lyrics
                self.result_time = time.monotonic()
                self._pending = None
            try:
                win32gui.PostMessage(self.notify_hwnd, WM_APP_LYRICS, 0, 0)
            except Exception:
                pass


# ══════════════════════════ 演示模式 ══════════════════════════
class DemoSource:
    """--demo "晴天 周杰伦":模拟播放,便于无播放器时验证效果。"""

    def __init__(self, query: str, client: LyricsClient):
        parts = query.split()
        if len(parts) >= 2:
            title, artist = parts[0], " ".join(parts[1:])
        else:
            title, artist = query, ""
        self.info = media_watcher.MediaInfo(app_id="demo", title=title,
                                            artist=artist, playing=True,
                                            position=0.0, sampled_at=time.monotonic())
        self.lyrics = client.fetch(f"demo|{title}|{artist}", title, artist)
        if not self.lyrics:
            from lyrics_client import LyricLine, Lyrics
            lines = ["演示模式:未联网或未匹配到歌词",
                     "但卡拉OK效果照常展示",
                     "粉色为已播放,橘色为未播放",
                     "打开QQ音乐后自动切换为真实歌词"]
            t = 1.0
            ls = []
            for txt in lines:
                ls.append(LyricLine(start=t, end=t + 3.0, text=txt))
                t += 3.0
            self.lyrics = Lyrics(key="demo", lines=ls)

    def latest(self):
        elapsed = time.monotonic() - self.info.sampled_at
        total = (self.lyrics.lines[-1].end + 3.0) if (self.lyrics and self.lyrics.lines) else 60.0
        if elapsed > total:                       # 演示循环
            self.info.sampled_at = time.monotonic()
        return self.info


# ══════════════════════════ 应用编排 ══════════════════════════
class App:
    def __init__(self, config: Config, demo_query: str | None, no_tray: bool):
        self.cfg = config
        self.no_tray = no_tray
        self.demo = None
        self.watcher = MediaWatcher(config)
        self.lyrics_client = LyricsClient(app_config.ROOT / "lyrics_cache",
                                          try_qrc=config.get("try_qrc", False))
        if demo_query:
            self.demo = DemoSource(demo_query, self.lyrics_client)
        self.renderer = KaraokeRenderer(config)
        self.hwnd = None
        self.layered = None
        self.tray = None
        self.tray_hwnd = None
        self.geom = None
        self.visible = False
        self.paused_display = False
        self.quit_requested = False
        self._cur_text = ""
        self._text_since = 0.0
        self._last_topmost = 0.0
        self._target_rect = None
        self._cur_rect = None
        self._geo_cand = None
        self._geo_cand_n = 0
        self._last_geo_ok = 0.0
        self._last_media_ok = 0.0
        self._last_frame_t = 0.0
        self._fs_state = False            # 去抖后的全屏状态
        self._fs_cand = False
        self._fs_cand_n = 0
        self._child_of_taskbar = False
        self.lyrics_worker = None
        # MiMo 余额挂件(独立子窗口,贴托盘)
        self.mimo_worker: MimoWorker | None = None
        self.mimo_hwnd = None
        self.mimo_layered = None
        self.mimo_renderer = MimoRenderer()
        self.mimo_visible = False
        self._mimo_target = None       # 屏幕坐标 rect(几何线程/on_geometry 写)
        self._mimo_drawn = -1          # 已绘制的 worker.version

    # ── 窗口过程 ────────────────────────────────────────
    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_TIMER:
            self.tick()
        elif msg == WM_APP_GEOM:
            self.on_geometry()
        elif msg == WM_APP_LYRICS:
            self.tick()  # 歌词到达,立即刷新
        elif msg == WM_APP_MIMO:
            self.tick()  # MiMo 数据更新,立即刷新
        elif msg == WM_APP_TRAY:
            if lparam == win32con.WM_RBUTTONUP or lparam == win32con.WM_CONTEXTMENU:
                self.tray.show_menu()
            elif lparam == win32con.WM_LBUTTONUP:
                self.tray.on_toggle()
        elif msg == win32con.WM_COMMAND:
            self.tray.handle_command(wparam & 0xFFFF)
        elif msg == win32con.WM_CLOSE and not self.quit_requested:
            # 外力关闭(如explorer重启连带销毁)不退出进程,由心跳自愈重建;
            # 用户退出走 quit():先置标志,再关窗
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ── 托盘宿主(独立隐藏窗口) ──────────────────────────
    def _create_tray_host(self) -> None:
        """Shell_NotifyIcon 只接受顶层窗口句柄,而歌词条已挂为任务栏
        子窗口 —— 故用独立隐藏窗口承载托盘图标与菜单,互不影响。"""
        name = CLASS_NAME + "Tray"
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = name
        wc.lpfnWndProc = self._tray_proc
        wc.hInstance = win32gui.GetModuleHandle(None)
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass
        self.tray_hwnd = win32gui.CreateWindowEx(
            0, name, "", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)

    def _tray_proc(self, hwnd, msg, wparam, lparam):
        if TASKBAR_CREATED_MSG and msg == TASKBAR_CREATED_MSG:
            # 任务栏(explorer)重启广播:重注册托盘图标 + 重挂载歌词子窗口。
            # 此刻新任务栏可能尚未就绪,失败也无妨 —— tick 每2秒兜底重试。
            if self.tray:
                self.tray.readd()
            self.ensure_parent()
        elif msg == WM_APP_TRAY:
            if lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                if self.tray:
                    self.tray.show_menu()
            elif lparam == win32con.WM_LBUTTONUP:
                self.toggle_display()
        elif msg == win32con.WM_COMMAND:
            if self.tray:
                self.tray.handle_command(wparam & 0xFFFF)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def ensure_parent(self) -> None:
        """确保歌词/挂件窗口始终挂载在当前任务栏下(explorer重启自愈核心)。

        挂载关系失效(旧任务栏销毁)时重挂,并把屏幕坐标换算成
        任务栏客户区坐标重新落位。"""
        self._ensure_child(self.hwnd, main=True)
        self._ensure_child(self.mimo_hwnd, main=False)

    def _ensure_child(self, hwnd: int | None, main: bool) -> None:
        if hwnd is None or not win32gui.IsWindow(hwnd):
            return
        if self._is_mounted_on_taskbar(hwnd):
            if main:
                self._child_of_taskbar = True
            return
        self._adopt_taskbar_parent(hwnd)
        mounted = self._is_mounted_on_taskbar(hwnd)
        if mounted:
            if main:
                self._apply_rect()      # 坐标系变化,需要重新落位
            else:
                self._apply_mimo_rect()

    # ── 窗口创建/自愈 ────────────────────────────────────
    def _create_main_window(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = self.wnd_proc
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass                                    # 已注册(explorer重启重建)
        exstyle = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST |
                   win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE |
                   win32con.WS_EX_TRANSPARENT)
        self.hwnd = win32gui.CreateWindowEx(
            exstyle, CLASS_NAME, APP_NAME, win32con.WS_POPUP,
            0, 0, 10, 10, 0, 0, wc.hInstance, None)
        self.layered = LayeredWindow(self.hwnd)
        # 注意:SetParent 必须等首次几何探测之后再做(见 run() 注释)

    def _create_mimo_window(self) -> None:
        """MiMo 余额挂件窗口:与歌词条同 class(几何探测自动排除),独立 hwnd。"""
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = self.wnd_proc
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass
        exstyle = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST |
                   win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE |
                   win32con.WS_EX_TRANSPARENT)
        self.mimo_hwnd = win32gui.CreateWindowEx(
            exstyle, CLASS_NAME, "MiMo余额", win32con.WS_POPUP,
            0, 0, 10, 10, 0, 0, wc.hInstance, None)
        self.mimo_layered = LayeredWindow(self.mimo_hwnd)

    def _is_mounted_on_taskbar(self, hwnd: int | None = None) -> bool:
        """可靠地判断窗口是否挂在任务栏下。

        不能用 GetParent —— 对本窗口(WS_POPUP)它返回 owner 而非父;
        SetParent 的返回值也不行(成功时返回旧父,顶层窗口的旧父是桌面)。
        用 GetAncestor(GA_PARENT): 顶层窗口返回桌面句柄,挂载后返回任务栏。
        """
        hwnd = hwnd or self.hwnd
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        tray = taskbar_geometry.find_taskbar_hwnd()
        if not tray:
            return False
        _user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        _user32.GetAncestor.restype = wt.HWND
        try:
            return _user32.GetAncestor(hwnd, 1) == tray   # GA_PARENT
        except Exception:
            return False

    def _adopt_taskbar_parent(self, hwnd: int | None = None) -> None:
        """挂为任务栏子窗口:子窗口永远绘制在任务栏表面之上,
        彻底避开"开始菜单/窗口切换时任务栏被系统提升、盖住歌词条"的Z序竞争
        (实测按Win键后任务栏会插到浮层歌词条之上且抢占周期性置顶)。"""
        hwnd = hwnd or self.hwnd
        is_main = hwnd == self.hwnd
        try:
            tray = taskbar_geometry.find_taskbar_hwnd()
            if not tray:
                if is_main:
                    self._child_of_taskbar = False
                return
            _user32.SetParent.argtypes = [wt.HWND, wt.HWND]
            _user32.SetParent.restype = wt.HWND
            _user32.SetParent(hwnd, tray)
            if self._is_mounted_on_taskbar(hwnd):
                if is_main and not self._child_of_taskbar:
                    log.info("已挂载为任务栏子窗口")
                if is_main:
                    self._child_of_taskbar = True
            elif is_main:
                self._child_of_taskbar = False
                log.warning("任务栏子窗口挂载未生效,回退浮顶层")
        except Exception as exc:
            if is_main:
                self._child_of_taskbar = False
            log.warning("挂载任务栏子窗口失败,回退浮顶层: %s", exc)

    def _recover_window(self) -> None:
        """explorer 重启会连带销毁挂载其下的子窗口:自动重建全部链路。

        顺序:建窗口(暂不挂载) → 重注册托盘图标 → 再挂载为子窗口。
        挂载后 Shell_NotifyIcon 会拒绝本进程调用,故托盘必须在前。
        """
        log.warning("歌词窗口丢失(任务栏重启?),自动重建")
        try:
            if self.layered:
                self.layered.close()
        except Exception:
            pass
        self.layered = None
        self.hwnd = None
        self.visible = False                  # 否则 set_visible(True) 会被短路
        self._create_main_window()
        self._recover_mimo_window()
        if self.tray:
            self.tray.readd()
        self._adopt_taskbar_parent()
        self.renderer.invalidate()
        if self._target_rect:
            self._cur_rect = self._target_rect
            self._apply_rect()
            self.set_visible(True)

    def _recover_mimo_window(self) -> None:
        """挂件窗口随任务栏一起被销毁时单独重建(可能先于主窗口丢失被发现)。"""
        if self.mimo_hwnd is not None and win32gui.IsWindow(self.mimo_hwnd):
            return
        try:
            if self.mimo_layered:
                self.mimo_layered.close()
        except Exception:
            pass
        self.mimo_layered = None
        self.mimo_hwnd = None
        self.mimo_visible = False
        self._create_mimo_window()
        self._adopt_taskbar_parent(self.mimo_hwnd)
        if self._mimo_target:
            self._apply_mimo_rect()
            self._set_mimo_visible(True)

    # ── 几何变化(去抖 + 平滑) ───────────────────────────
    def _split_gap(self, geo):
        """把"图标↔托盘"空隙分成 (歌词条rect, 挂件rect)。

        挂件贴托盘(空隙右端);空间不足时挂件收窄,窄到放不下
        (逻辑 <110px)就整体让位给歌词条 —— 歌词是核心功能。
        """
        if geo is None or not geo.ok:
            return None, None
        l, t, r, b = geo.bar
        gl, gr = geo.gap
        gap_w = gr - gl
        mw = 0
        if self.cfg.get("mimo_widget", True):
            wanted = int(self.cfg.get("mimo_width", 160) * geo.scale)
            mw = min(wanted, gap_w - 102)     # 至少给歌词留 96 + 6 间隔
            if mw < 110:
                mw = 0
        widget = (gr - mw, t, mw, b - t) if mw else None
        lyric_w = gap_w - mw - (6 if mw else 0)
        lyric = (gl, t, lyric_w, b - t) if lyric_w >= 96 else None
        return lyric, widget

    def on_geometry(self) -> None:
        geo = self.geom
        now = time.monotonic()
        geo_ok = geo is not None and geo.ok

        if geo_ok:
            lyric_rect, widget_rect = self._split_gap(geo)
            # 挂件位置贴托盘、极少变化:直接落位,不参与滑动去抖
            if widget_rect != self._mimo_target:
                self._mimo_target = widget_rect
                self._apply_mimo_rect()
        else:
            lyric_rect = None
            # 瞬时探测失败:挂件与歌词一样保持原地,持续>10s才隐藏
            if (self._last_geo_ok and now - self._last_geo_ok > 10.0
                    and self._mimo_target is not None):
                self._mimo_target = None
                self._apply_mimo_rect()

        if not geo_ok:
            # 短暂探测失败:歌词条保持原地;持续失败超10s才隐藏
            if self._last_geo_ok and now - self._last_geo_ok > 10.0:
                self._target_rect = None
                self.set_visible(False)
            return
        self._last_geo_ok = now
        if geo.stale and self._target_rect is not None:
            return  # 近似估计:已有真实几何时不回退
        rect = lyric_rect
        if rect is None:
            self._target_rect = None
            self.set_visible(False)
            return

        if self._target_rect is None:                 # 首次:直接落位
            self._target_rect = rect
            self._cur_rect = rect
            self._apply_rect()
            return
        if rect == self._target_rect:
            self._geo_cand, self._geo_cand_n = None, 0
            return
        t = self._target_rect
        if (abs(rect[0] - t[0]) < 4 and abs(rect[2] - t[2]) < 4
                and rect[1] == t[1] and rect[3] == t[3]):
            return                                    # 死区:亚图标级抖动忽略
        if self._geo_cand == rect:                    # 连续两轮一致才应用
            self._geo_cand_n += 1
            if self._geo_cand_n >= 2:
                self._target_rect = rect
                self._geo_cand, self._geo_cand_n = None, 0
        else:
            self._geo_cand, self._geo_cand_n = rect, 1

    def _apply_rect(self) -> None:
        x, y, w, h = self._cur_rect
        mounted = self._is_mounted_on_taskbar()
        if mounted and self.geom is not None:
            x -= self.geom.bar[0]               # 子窗口坐标相对任务栏客户区
            y -= self.geom.bar[1]
        z = win32con.HWND_TOP if mounted else win32con.HWND_TOPMOST
        win32gui.SetWindowPos(self.hwnd, z, x, y, w, h,
                              win32con.SWP_NOACTIVATE)
        self.renderer.set_size(w, h)

    def _apply_mimo_rect(self) -> None:
        """落位挂件窗口;目标为空(空间不足/几何持续失败)则隐藏。"""
        if self.mimo_hwnd is None or not win32gui.IsWindow(self.mimo_hwnd):
            return
        if self._mimo_target is None:
            self._set_mimo_visible(False)
            return
        x, y, w, h = self._mimo_target
        mounted = self._is_mounted_on_taskbar(self.mimo_hwnd)
        if mounted and self.geom is not None:
            x -= self.geom.bar[0]               # 子窗口坐标相对任务栏客户区
            y -= self.geom.bar[1]
        z = win32con.HWND_TOP if mounted else win32con.HWND_TOPMOST
        win32gui.SetWindowPos(self.mimo_hwnd, z, x, y, w, h,
                              win32con.SWP_NOACTIVATE)
        self.mimo_renderer.set_size(w, h)
        self._mimo_drawn = -1                   # 尺寸变化,强制重绘

    def _set_mimo_visible(self, show: bool) -> None:
        if self.mimo_hwnd is None or show == self.mimo_visible:
            return
        win32gui.ShowWindow(self.mimo_hwnd,
                            win32con.SW_SHOWNOACTIVATE if show else win32con.SW_HIDE)
        self.mimo_visible = show
        if show:
            self._mimo_drawn = -1

    def _animate_rect(self, dt: float) -> None:
        """当前矩形向目标矩形平滑滑动(约150ms收敛)。"""
        if self._target_rect is None or self._cur_rect is None:
            return
        if self._cur_rect == self._target_rect:
            return
        k = min(1.0, 1.0 - pow(0.0001, dt))          # 指数趋近
        cur, tgt = self._cur_rect, self._target_rect
        new = tuple(int(c + (t - c) * k) for c, t in zip(cur, tgt))
        if all(abs(n - t) <= 1 for n, t in zip(new, tgt)):
            new = tgt
        if new != self._cur_rect:
            self._cur_rect = new
            self._apply_rect()

    def set_visible(self, show: bool) -> None:
        if self.hwnd is None or show == self.visible:
            return
        win32gui.ShowWindow(self.hwnd,
                            win32con.SW_SHOWNOACTIVATE if show else win32con.SW_HIDE)
        self.visible = show

    # ── 每帧编排 ────────────────────────────────────────
    def tick(self) -> None:
        cfg = self.cfg
        if self.hwnd is None or not win32gui.IsWindow(self.hwnd):
            self._recover_window()
        now = time.monotonic()
        dt = min(0.15, now - self._last_frame_t) if self._last_frame_t else 0.033
        self._last_frame_t = now

        # 周期维护(每2秒,与几何状态无关):托盘图标重注册(开机时任务栏可能
        # 未就绪)、任务栏重挂载(explorer重启兜底)、置顶保持
        if now - self._last_topmost > 2.0:
            self._last_topmost = now
            if self.tray and not self.tray.added:
                self.tray.add()
            self._recover_mimo_window()          # 挂件窗口单独丢失时兜底重建
            self.ensure_parent()
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                z = (win32con.HWND_TOP if self._child_of_taskbar
                     else win32con.HWND_TOPMOST)
                win32gui.SetWindowPos(self.hwnd, z, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                                      win32con.SWP_NOACTIVATE)
            if self.mimo_hwnd and win32gui.IsWindow(self.mimo_hwnd):
                z = (win32con.HWND_TOP
                     if self._is_mounted_on_taskbar(self.mimo_hwnd)
                     else win32con.HWND_TOPMOST)
                win32gui.SetWindowPos(self.mimo_hwnd, z, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                                      win32con.SWP_NOACTIVATE)

        media = self.demo.latest() if self.demo else self.watcher.latest()
        if media is None and not self.demo:
            media = media_watcher.find_qqmusic_window_title()

        hide_fullscreen = cfg.get("hide_in_fullscreen", True)
        # 全屏判定去抖:连续两轮全屏才隐藏,退出立即恢复 —— 防切换应用瞬间误判
        if self.geom is not None and self.geom.ok:
            fs_now = bool(self.geom.fullscreen)
            if fs_now != self._fs_state:
                if fs_now:
                    if self._fs_cand == fs_now:
                        self._fs_cand_n += 1
                    else:
                        self._fs_cand, self._fs_cand_n = fs_now, 1
                    if self._fs_cand_n >= 2:
                        self._fs_state = True
                        self._fs_cand, self._fs_cand_n = False, 0
                else:
                    self._fs_state = False
                    self._fs_cand, self._fs_cand_n = False, 0
        # 媒体会话宽限:SMTC 在系统忙(按Win/切换应用/explorer重启)时会瞬时
        # 返回空会话,若立即隐藏就是用户看到的"歌词闪退"。宽限期内保持
        # 最后一帧画面不动,超时才认为播放真的结束。
        if media is not None:
            self._last_media_ok = now
        media_grace = float(cfg.get("media_grace_sec", 8.0))
        media_alive = media is not None or (now - self._last_media_ok < media_grace)

        # 显示条件与"瞬时探测失败"解耦:只要还有有效的目标几何就保持显示,
        # 探测短暂失败/任务栏忙碌期不闪隐(持续失败>10s才由 on_geometry 隐藏)
        want_show = (media_alive and self._target_rect is not None
                     and not self.paused_display
                     and not (hide_fullscreen and self._fs_state))
        if want_show and not self.visible and self._target_rect:
            # 重新显示:直接落位到最新目标,不播放旧滑动
            self._cur_rect = self._target_rect
            self._apply_rect()
        self.set_visible(want_show)

        # MiMo 挂件:与播放状态无关,只受几何/全屏开关控制
        mimo_show = (self._mimo_target is not None
                     and not (hide_fullscreen and self._fs_state))
        self._set_mimo_visible(mimo_show)
        if mimo_show:
            self._draw_mimo()

        if not want_show:
            return
        self._animate_rect(dt)
        if media is None:
            return          # 宽限期内保留当前画面,不重绘

        # 歌词请求:每帧都交给 worker 去重(失败结果60s后自动重试),
        # 不再用 _req_key 一次性挡板 —— 否则一次失败整首歌都没歌词
        key = media.key
        lyrics = None
        if self.demo:
            lyrics = self.demo.lyrics
        else:
            if self.lyrics_worker:
                self.lyrics_worker.request(key, media.title, media.artist)
            lyrics = self.lyrics_worker.get(key)
        pos = media.current_position()
        if pos is not None:
            # 时轴整体微调:正数=歌词与粉字延后出现(听到的声音慢于系统
            # 进度,如蓝牙耳机延迟),负数=提前;config.json 热重载
            pos -= float(cfg.get("sync_offset_sec", 0.0))

        # 选行与文本(行提前 lead 秒出现,粉色推进仍与真实演唱同步)
        fade = 1.0
        if lyrics and pos is not None:
            grace = cfg.get("interlude_grace_sec", 2.0)
            lead = max(0.0, float(cfg.get("lyric_lead_sec", 0.5)))
            line, frac = lyrics.line_at(pos, grace, lead)
            if line is not None:
                text = line.text
                cursor = frac if media.has_timeline else None
            else:
                text, cursor = self._title_text(media), None
        else:
            text, cursor = self._title_text(media), None

        now = time.monotonic()
        if text != self._cur_text:
            self._cur_text = text
            self._text_since = now
        fade_span = max(0.05, cfg.get("crossfade_ms", 180) / 1000.0)
        fade = min(1.0, (now - self._text_since) / fade_span)

        frame = self.renderer.draw(text, cursor, fade)
        if frame is not None:
            self.layered.update(frame)

    def _draw_mimo(self) -> None:
        w = self.mimo_worker
        if w is None or self.mimo_layered is None:
            return
        if w.version == self._mimo_drawn:
            return                                  # 内容没变,不重画
        frame = self.mimo_renderer.draw(w.state, w.data, w.message)
        if frame is not None:
            self.mimo_layered.update(frame)
            self._mimo_drawn = w.version

    @staticmethod
    def _title_text(media) -> str:
        t, a = media.display_title, media.display_artist
        if a and a.lower() not in t.lower():
            return f"♪ {t} - {a}"
        return f"♪ {t}" if t else "♪"

    # ── 托盘动作 ────────────────────────────────────────
    def toggle_display(self) -> None:
        self.paused_display = not self.paused_display
        if self.paused_display:
            self.set_visible(False)

    def toggle_autostart(self) -> None:
        set_autostart(not get_autostart())

    def mimo_login(self) -> None:
        """托盘菜单:打开浏览器让用户登录小米账号(挂件会话初始化/重登)。"""
        worker = self._ensure_mimo_worker()
        if worker:
            worker.request_login()

    def _ensure_mimo_worker(self):
        """启动(或热启用后补建)MiMo 取数线程。"""
        if not self.cfg.get("mimo_widget", True):
            return self.mimo_worker
        if self.mimo_worker is None:
            self.mimo_worker = MimoWorker(
                self._mimo_notify,
                refresh_sec=float(self.cfg.get("mimo_refresh_sec", 180)))
            self.mimo_worker.start()
        return self.mimo_worker

    def _mimo_notify(self) -> None:
        """MimoWorker 回调(任意线程)→ 唤醒 UI 线程刷新。"""
        try:
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                win32gui.PostMessage(self.hwnd, WM_APP_MIMO, 0, 0)
        except Exception:
            pass

    def quit(self) -> None:
        self.quit_requested = True          # 豁免 WM_CLOSE 拦截,允许真正销毁
        if self.tray:
            self.tray.remove()
        if self.mimo_worker:
            self.mimo_worker.stop()
        try:
            win32gui.DestroyWindow(self.hwnd)
        except Exception:
            pass
        if self.mimo_hwnd:
            try:
                win32gui.DestroyWindow(self.mimo_hwnd)
            except Exception:
                pass
        try:
            win32gui.DestroyWindow(self.tray_hwnd)
        except Exception:
            pass
        win32gui.PostQuitMessage(0)

    # ── 启动 ────────────────────────────────────────────
    def run(self) -> None:
        # 顺序很关键,三步都不能换:
        #   ① 先注册托盘图标 —— SetParent 到 explorer 任务栏后
        #      Shell_NotifyIcon 会拒绝本进程所有 NIM_ADD;
        #   ② 再探初始几何 —— 必须在挂载子窗口之前!挂载后歌词条进入
        #      任务栏 UIA 树,若此时在 UI 线程同步遍历任务栏,UIA 还要
        #      回头查询我们自己这个子窗口,而 UI 线程正阻塞等待遍历
        #      结果 → 自我死锁(实测卡15秒并返回空几何);
        #   ③ 最后挂载为任务栏子窗口(子窗口永远画在任务栏表面之上,
        #      避免开始菜单/切换窗口时任务栏被系统提升盖住歌词条)。
        self._create_tray_host()
        if not self.no_tray:
            self.tray = TrayIcon(self.tray_hwnd, self.toggle_display,
                                 self.toggle_autostart, self.quit,
                                 on_mimo=self.mimo_login)
        self._create_main_window()
        self._create_mimo_window()

        self.geom = taskbar_geometry.get_geometry(
            self.cfg.get("min_gap_width", 150.0))

        self._adopt_taskbar_parent()
        self._adopt_taskbar_parent(self.mimo_hwnd)
        self.on_geometry()

        self.lyrics_worker = LyricsWorker(self.lyrics_client, self.hwnd)
        self.lyrics_worker.start()
        self._ensure_mimo_worker()
        self.watcher.start()
        if not self.demo:
            taskbar_geometry.GeometryPoller(
                self.hwnd, self.cfg,
                on_change=self._geom_from_thread,
                on_config_change=self._config_changed).start()

        # 线程级心跳:不依赖窗口存活 —— 任务栏重启连带销毁窗口后,
        # tick 仍能运行并自动重建窗口(窗口级 timer 会随窗口一起消失)
        fps = max(10, int(self.cfg.get("fps", 30)))
        _user32.SetTimer.restype = wt.BOOL
        _user32.SetTimer.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT, ctypes.c_void_p]
        _user32.SetTimer(None, 1, int(1000 / fps), None)
        log.info("已启动 dpi_scale=%.2f gap=%s 子窗口模式=%s",
                 self.geom.scale, self.geom.gap, self._child_of_taskbar)
        pump(self)
        _user32.KillTimer(None, 1)

    def _geom_from_thread(self, geo) -> None:
        self.geom = geo
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, WM_APP_GEOM, 0, 0)
            except Exception:
                pass

    def _config_changed(self) -> None:
        self.renderer.invalidate()
        self.mimo_renderer.invalidate()
        self._ensure_mimo_worker()      # 运行中打开 mimo_widget 也能生效
        self._req_key = None


def pump(app: "App") -> None:
    msg = wt.MSG()
    pmsg = ctypes.byref(msg)
    while True:
        r = _user32.GetMessageW(pmsg, None, 0, 0)
        if r in (0, -1):
            break
        if msg.message == win32con.WM_TIMER and not msg.hWnd:
            app.tick()                       # 线程级心跳(窗口无关)
            continue
        _user32.TranslateMessage(pmsg)
        _user32.DispatchMessageW(pmsg)


# ══════════════════════════ 入口 ══════════════════════════
def setup_logging(debug: bool) -> None:
    log_dir = app_config.ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "taskbar_lyrics.log", maxBytes=512 * 1024,
        backupCount=2, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if debug:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--demo", nargs="?", const="晴天 周杰伦", default=None,
                        help="演示模式,例如: --demo 晴天 周杰伦")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    taskbar_geometry.set_dpi_awareness()
    setup_logging(args.debug or bool(args.demo))

    try:
        return _run_app(args)
    except Exception:
        # pythonw 无控制台,异常必须落盘,否则开机自启静默消失无从排查
        log.exception("程序异常退出")
        return 1


def _run_app(args) -> int:
    # 单实例
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:
        log.info("已有实例在运行,退出")
        return 0

    cfg = Config(Path(args.config) if args.config else app_config.CONFIG_PATH)
    if not cfg.path.exists():
        cfg.save_defaults()

    app = App(cfg, args.demo, args.no_tray)
    try:
        app.run()
    finally:
        app.watcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
