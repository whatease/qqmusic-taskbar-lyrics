# -*- coding: utf-8 -*-
"""诊断工具:一键排查 环境 / 任务栏几何 / SMTC媒体 / 歌词接口。

用法:
  python tools/diagnose.py            # 全部检查
  python tools/diagnose.py --screenshot  # 顺便截取任务栏存到 logs/taskbar.png
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import taskbar_geometry  # noqa: E402


def check_env():
    print("== 环境 ==")
    print("python:", sys.version.split()[0])
    mods = ["win32gui", "PIL", "uiautomation", "requests",
            "Crypto", "winrt.windows.media.control"]
    for m in mods:
        try:
            __import__(m)
            print(f"  {m}: OK")
        except Exception as e:
            print(f"  {m}: 缺失({e.__class__.__name__})")


def check_taskbar():
    print("== 任务栏几何 ==")
    taskbar_geometry.set_dpi_awareness()
    geo = taskbar_geometry.get_geometry(150.0)
    if not geo.ok:
        print("  获取失败!bar=", geo.bar)
        return
    l, t, r, b = geo.bar
    print(f"  任务栏: ({l},{t})-({r},{b}) 高={geo.height}px scale={geo.scale:.2f}")
    print(f"  可用空隙: x=[{geo.gap[0]}, {geo.gap[1]}] 宽={geo.gap[1]-geo.gap[0]}px")
    print(f"  全屏应用: {'是' if geo.fullscreen else '否'}")


def check_media():
    print("== SMTC 媒体会话 ==")
    try:
        import asyncio
        from media_watcher import MediaWatcher
        from app_config import Config
        cfg = Config()
        w = MediaWatcher(cfg)
        info = asyncio.run(w._poll_once())
        if info is None:
            print("  无会话(打开QQ音乐播放一首歌再试)")
            return
        print(f"  应用: {info.app_id}")
        print(f"  歌曲: {info.title} - {info.artist}")
        print(f"  状态: {'播放中' if info.playing else '已暂停'}  进度≈{info.position:.1f}s")
        print(f"  歌词键: {info.key}")
    except Exception as e:
        print("  失败:", e)


def check_lyrics():
    print("== 歌词接口 ==")
    from app_config import Config
    from lyrics_client import LyricsClient
    cfg = Config()
    client = LyricsClient(Path(__file__).parent.parent / "lyrics_cache",
                          try_qrc=cfg.get("try_qrc", False))
    lyrics = client.fetch("diag|晴天|周杰伦", "晴天", "周杰伦")
    if lyrics is None:
        print("  获取失败(检查网络)")
        return
    print(f"  OK mid={lyrics.song_mid} 行数={len(lyrics.lines)}")
    for ln in lyrics.lines[4:7]:
        print(f"    [{ln.start:7.2f}s] {ln.text[:24]}")


def screenshot():
    print("== 任务栏截图 ==")
    from PIL import ImageGrab
    taskbar_geometry.set_dpi_awareness()
    hwnd = taskbar_geometry.find_taskbar_hwnd()
    import ctypes.wintypes as wt
    r = wt.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=False)
    out = Path(__file__).parent.parent / "logs" / "taskbar.png"
    out.parent.mkdir(exist_ok=True)
    img.save(out)
    print("  已保存:", out, img.size)


if __name__ == "__main__":
    check_env()
    check_taskbar()
    check_media()
    check_lyrics()
    if "--screenshot" in sys.argv:
        screenshot()
