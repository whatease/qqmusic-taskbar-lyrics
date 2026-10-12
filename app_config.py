# -*- coding: utf-8 -*-
"""QQ音乐底栏歌词 — 配置模块(默认值 / 加载 / 保存 / 热重载)。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    # ── 外观 ──────────────────────────────────────────────
    "played_color": "#FF7EB3",      # 已播放:粉色
    "unplayed_color": "#FFA347",    # 未播放:橘色
    "title_color": "#FFC7E0",       # 无歌词时展示歌曲名的颜色(None=跟随未播放色)
    "font_family": ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "DengXian", "Segoe UI"],
    "font_scale": 0.60,             # 字号 = 任务栏高度 * 该系数
    "min_font_scale": 0.42,         # 长句压缩下限
    "glow_strength": 0.85,          # 文字辉光强度 0~1
    "outline_width": 1.3,           # 描边宽度(逻辑像素)
    "margin_x": 12,                 # 与两侧图标的留白(逻辑像素)
    "vertical_offset": 0,           # 垂直微调(像素,正=向下)
    "soft_edge_px": 6,              # 粉/橘过渡柔边宽度(像素)
    "crossfade_ms": 180,            # 换行淡入淡出
    "fps": 30,
    # ── 行为 ──────────────────────────────────────────────
    "min_gap_width": 150,           # 图标与托盘之间最小可用宽度(逻辑像素)
    "geom_poll_sec": 1.5,           # 任务栏几何轮询(图标数量变化自适应)
    "media_poll_sec": 0.35,         # 媒体信息轮询
    "show_when_paused": True,       # 暂停时继续显示(定格)
    "hide_in_fullscreen": True,     # 全屏应用时隐藏
    "allow_other_media": True,      # 找不到QQ音乐会话时允许使用其他媒体会话
    "prefer_app_ids": ["qqmusic", "tencent.qqmusic", "qqmusicfast", "qq音乐"],
    "interlude_grace_sec": 2.0,     # 歌词行结束后停留多久再切回标题
    "media_grace_sec": 8.0,         # 媒体会话瞬时中断时歌词条保留多久(防按Win/切换应用闪隐)
    "lyric_lead_sec": 0.5,          # 歌词比歌声提前多少秒出现(0=同步;跟唱建议0.3~0.8)
    "lyric_retry_sec": 120,         # 歌词获取失败后的重试间隔
    "try_qrc": False,               # 逐字歌词(QRC匿名接口已失效,保留开关)
    # ── MiMo 余额挂件(任务栏右侧,紧靠托盘) ─────────────
    "mimo_widget": True,            # 显示 MiMo 余额/今日用量挂件
    "mimo_width": 160,              # 挂件宽度(逻辑像素,空间不足时自动收窄)
    "mimo_refresh_sec": 180,        # 余额/用量刷新间隔
    "debug": False,
}


class Config:
    """线程安全的配置容器,支持 mtime 热重载。"""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self._mtime = 0.0
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        data = dict(DEFAULTS)
        try:
            if self.path.exists():
                user = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(user, dict):
                    data.update({k: v for k, v in user.items() if k in DEFAULTS})
        except Exception:
            pass
        with self._lock:
            self._data = data
        return True

    def save_defaults(self) -> None:
        try:
            self.path.write_text(
                json.dumps(DEFAULTS, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime != self._mtime
        except OSError:
            return False

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, name, default=None):
        with self._lock:
            return self._data.get(name, default)
