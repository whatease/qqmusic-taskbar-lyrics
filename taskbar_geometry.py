# -*- coding: utf-8 -*-
"""任务栏几何探测:图标簇与系统托盘之间的空档区域。

方案(UIA,与Win10/Win11中文/英文系统无关):
  1. 深度遍历 Shell_TrayWnd 的叶子元素,收集可见包围盒;
  2. 合并相邻元素成"簇",簇与簇之间的水平空隙即候选区;
  3. 取满足最小宽度、最靠右的空隙(即最后一个图标与托盘之间),
     从而天然跟随 图标数量/对齐方式/托盘布局 的变化;
  4. 顺带检测全屏应用(全屏时隐藏歌词条)。

所有坐标均为物理像素(进程已设 per-monitor DPI aware)。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("geometry")

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi

DWMWA_CLOAKED = 14
_EXCLUDE_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
    "XamlExplorerHostIslandWindow", "QQMusicTaskbarLyricsWnd",
}


def set_dpi_awareness() -> None:
    """进程级 per-monitor v2 DPI aware(幂等,须在任何窗口/坐标计算前调用)。"""
    try:
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


def find_taskbar_hwnd() -> int:
    return _user32.FindWindowW("Shell_TrayWnd", None)


@dataclass
class Geometry:
    ok: bool = False
    bar: tuple = (0, 0, 0, 0)          # 任务栏矩形 l,t,r,b
    gap: tuple = (0, 0)                # 可用空隙 (left, right) 物理像素
    height: int = 48
    scale: float = 1.0                 # 依据任务栏高度推算的DPI比例
    fullscreen: bool = False
    stale: bool = False                # True=近似估计(UIA失败时的回退值)


def _rect_of(ctrl) -> tuple | None:
    try:
        r = ctrl.BoundingRectangle
        if r is None:
            return None
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


_OWN_CLASS = "QQMusicTaskbarLyricsWnd"
_OWN_NAMES = {"QQ音乐底栏歌词"}


def _collect_leaf_rects(root, budget=500, max_depth=16) -> list[tuple]:
    """收集叶子元素包围盒(带节点预算,防止UIA树异常膨胀)。

    自动跳过自身歌词条窗口 —— 挂为任务栏子窗口后它会出现在
    Shell_TrayWnd 的 UIA 树里,不排除会把可用空隙"堵死"。
    按 ClassName/Name 排除(跨进程最稳;句柄比较在64位下易被截断)。
    """
    out: list[tuple] = []
    stack = [(root, 0)]
    while stack and budget > 0:
        node, depth = stack.pop()
        budget -= 1
        try:
            if node.ClassName == _OWN_CLASS:
                continue
            if node.Name in _OWN_NAMES:
                continue
        except Exception:
            pass
        try:
            children = node.GetChildren()
        except Exception:
            children = []
        if not children and node is not root:
            r = _rect_of(node)
            if r and r[2] > r[0] and r[3] > r[1]:
                out.append(r)
            continue
        if depth < max_depth:
            for ch in reversed(children):
                stack.append((ch, depth + 1))
    return out


def _compute_gap(bar: tuple, rects: list[tuple], min_gap: int) -> tuple | None:
    l, t, _, b = bar
    band = [r for r in rects
            if t - 4 <= (r[1] + r[3]) / 2 <= b + 4      # 垂直方向属于任务栏
            and r[3] - r[1] <= (b - t) * 1.8]
    if not band:
        return None
    band.sort(key=lambda r: r[0])
    merged: list[list[int]] = []
    for r in band:
        if merged and r[0] <= merged[-1][1] + 4:        # 相邻4px内视为同簇
            merged[-1][1] = max(merged[-1][1], r[2])
        else:
            merged.append([r[0], r[2]])
    merged = [m for m in merged if m[1] > l and m[0] < bar[2]]
    if not merged:
        return None
    candidates = []
    prev_right = l
    for m in merged:
        gap_l, gap_r = max(prev_right, l), min(m[0], bar[2])
        if gap_r - gap_l >= min_gap:
            candidates.append((gap_l, gap_r))
        prev_right = max(prev_right, m[1])
    if bar[2] - prev_right >= min_gap:                  # 末簇之后(一般不会出现)
        candidates.append((prev_right, bar[2]))
    if not candidates:
        return None
    return candidates[-1]                               # 最靠右的可用空隙


def _is_cloaked(hwnd: int) -> bool:
    val = ctypes.c_int(0)
    try:
        _dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return val.value != 0


def detect_fullscreen(monitor: tuple) -> bool:
    """是否存在覆盖整个显示器的全屏窗口(排除壁纸/任务栏/托盘浮层)。"""
    ml, mt, mr, mb = monitor
    result = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        try:
            import win32gui
            cls = win32gui.GetClassName(hwnd)
            if cls in _EXCLUDE_CLASSES:
                return True
            style = _user32.GetWindowLongW(hwnd, -16)   # GWL_STYLE
            exstyle = _user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if style & 0x00C80000:                       # WS_CAPTION|WS_???(标题栏视为非全屏)
                return True
            if exstyle & 0x00000080:                     # WS_EX_TOOLWINDOW
                return True
            r = wt.RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            if r.left <= ml + 2 and r.top <= mt + 2 and r.right >= mr - 2 and r.bottom >= mb - 2:
                if _is_cloaked(hwnd):
                    return True
                result.append(hwnd)
        except Exception:
            pass
        return True

    try:
        import win32gui
        win32gui.EnumWindows(cb, None)
    except Exception:
        return False
    return bool(result)


def get_geometry(min_gap_logical: float = 150.0) -> Geometry:
    """返回任务栏空档几何;UIA失败时退化到 TrayNotifyWnd 估计值。"""
    hwnd = find_taskbar_hwnd()
    geo = Geometry()
    if not hwnd:
        return geo
    r = wt.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return geo
    bar = (r.left, r.top, r.right, r.bottom)
    geo.bar = bar
    geo.height = max(20, r.bottom - r.top)
    geo.scale = geo.height / 48.0
    geo.fullscreen = detect_fullscreen(
        (_user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77),
         _user32.GetSystemMetrics(76) + _user32.GetSystemMetrics(78),
         _user32.GetSystemMetrics(77) + _user32.GetSystemMetrics(79)))

    min_gap = int(min_gap_logical * geo.scale)
    gap = None
    uia_failed = False
    try:
        import uiautomation as auto
        root = auto.ControlFromHandle(hwnd)
        if root is not None:
            rects = _collect_leaf_rects(root)
            gap = _compute_gap(bar, rects, min_gap)
            if gap is None and rects:
                gap = _compute_gap(bar, rects, int(min_gap * 0.5))
    except Exception as exc:
        uia_failed = True
        log.debug("UIA遍历失败: %s", exc)

    if gap is None and uia_failed:
        # 仅在 UIA 硬失败时用托盘区左缘做近似估计;
        # "没有足够空隙"是真实测量结果(任务栏太满),绝不能回退覆盖图标
        trect = wt.RECT()
        tray_hwnd = _user32.FindWindowExW(hwnd, None, "TrayNotifyWnd", None)
        if tray_hwnd and _user32.GetWindowRect(tray_hwnd, ctypes.byref(trect)):
            right = trect.left - 8
            left = bar[0] + int((bar[2] - bar[0]) * 0.25)
            if right - left >= min_gap:
                gap = (left, right)
                geo.stale = True
    if gap:
        geo.gap = gap
        geo.ok = True
    return geo


class GeometryPoller(threading.Thread):
    """后台轮询任务栏几何 + 配置热重载检测。

    UIA 枚举在任务栏布局变化(开/关应用)瞬间可能被阻塞数秒并返回
    不一致快照 —— 这是歌词条左右抖动的根源。因此:
      1. 测量放进独立 worker 线程(single-flight,慢测量不阻塞轮询);
      2. 快照一律交付,去抖完全交给应用侧(双确认 + 死区 + 平滑滑动):
         不一致的快照几乎不会连续两轮完全相同,自然被双确认挡掉。
         曾按"耗时>1.2s 丢弃"过滤,但机器负载高时 UIA 遍历常态就要
         2s+,等于把所有更新都丢光 → 歌词条永远卡着不动(实测复现);
      3. worker 卡死(跨进程 UIA 调用挂起)超 15s 则放弃本轮重建,
         避免 single-flight 永久堵死同样造成"不再动"。
    """

    def __init__(self, hwnd: int, config, on_change, on_config_change):
        super().__init__(daemon=True, name="geom-poller")
        self.hwnd = hwnd
        self.config = config
        self.on_change = on_change        # callable(Geometry)
        self.on_config_change = on_config_change
        self._worker: threading.Thread | None = None
        self._worker_result = (None, 0.0)
        self._worker_t0 = 0.0
        self._gen = 0                     # 结果世代号:被放弃的 worker 结果作废
        self._stop = threading.Event()

    def run(self) -> None:
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        while not self._stop.wait(0.35):
            try:
                w = self._worker
                if w is not None and not w.is_alive():
                    geo, took = self._worker_result
                    self._worker = None
                    if took > 1.2:
                        log.debug("慢速 UIA 快照(耗时 %.1fs),交由应用侧双确认去抖",
                                  took)
                    self.on_change(geo)
                    if self.config.changed_on_disk():
                        if self.config.reload():
                            self.on_config_change()
                if (self._worker is not None
                        and time.monotonic() - self._worker_t0 > 15.0):
                    log.warning("UIA 探测超时(>15s),放弃本轮重建探测")
                    self._gen += 1        # 泄漏线程的结果回来后作废
                    self._worker = None
                if self._worker is None:
                    self._worker = threading.Thread(
                        target=self._probe, daemon=True)
                    self._worker_t0 = time.monotonic()
                    self._worker.start()
            except Exception as exc:
                log.exception("几何轮询异常: %s", exc)

    def _probe(self) -> None:
        gen = self._gen
        t0 = time.monotonic()
        try:
            geo = get_geometry(self.config.get("min_gap_width", 150.0))
        except Exception:
            geo = Geometry()
        if gen == self._gen:              # 未被放弃才交付
            self._worker_result = (geo, time.monotonic() - t0)

    def stop(self) -> None:
        self._stop.set()
