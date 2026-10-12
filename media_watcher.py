# -*- coding: utf-8 -*-
"""媒体监听:Windows SMTC(系统媒体传输控制)→ 正在播放的歌曲与进度。

首选 Python WinRT 投影(winrt-Windows.Media.Control);不可用时退化为
常驻 PowerShell 流式辅助进程;再退化到读取 QQ音乐窗口标题(无进度)。
后线程只维护最新 MediaInfo,渲染线程无锁竞争地读取。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("media")

STATUS_PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus

_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) `
    ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
while ($true) {
    $out = '{"found":false}'
    try {
        $s = $null
        foreach ($x in @($mgr.GetSessions())) {
            $id = $x.SourceAppUserModelId
            if ($id -match '__PREFER__') { $s = $x; break }
        }
        if (-not $s) { foreach ($x in @($mgr.GetSessions())) {
            if ($x.GetPlaybackInfo().PlaybackStatus -eq 'Playing') { $s = $x; break } } }
        if (-not $s) { $all = @($mgr.GetSessions()); if ($all.Count -gt 0) { $s = $all[0] } }
        if ($s) {
            $pi = $s.GetPlaybackInfo()
            $tl = $s.GetTimelineProperties()
            $mp = Await ($s.TryGetMediaPropertiesAsync()) `
                ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
            $playing = ($pi.PlaybackStatus -eq 'Playing')
            $rate = $pi.PlaybackRate
            if (-not $rate -or $rate -le 0) { $rate = 1.0 }
            $pos = $tl.Position.TotalMilliseconds
            if ($playing) {
                $elapsed = ((Get-Date) - $tl.LastUpdatedTime.LocalDateTime).TotalMilliseconds * $rate
                if ($elapsed -gt 0) { $pos += $elapsed }
            }
            $o = @{ found = $true; appid = $s.SourceAppUserModelId; title = $mp.Title
                    artist = $mp.Artist; playing = [bool]$playing
                    position = [math]::Round($pos / 1000.0, 3); rate = $rate }
            $out = ConvertTo-Json -Compress -InputObject $o
        }
    } catch { $out = '{"found":false}' }
    Write-Output $out
    Start-Sleep -Milliseconds 350
}
"""


@dataclass
class MediaInfo:
    app_id: str = ""
    title: str = ""
    artist: str = ""
    playing: bool = False
    position: float = 0.0          # 采样时刻的进度(秒)
    sampled_at: float = 0.0        # time.monotonic()
    rate: float = 1.0
    has_timeline: bool = True      # False=仅有标题(窗口标题回退)

    @property
    def key(self) -> str:
        return f"{self.title.strip().lower()}|{self.artist.strip().lower()}"

    @property
    def display_title(self) -> str:
        t = self.title.strip()
        a = self.artist.strip()
        if not a and " - " in t:
            t, _, a = t.rpartition(" - ")
        return t or "未知歌曲"

    @property
    def display_artist(self) -> str:
        t = self.title.strip()
        a = self.artist.strip()
        if not a and " - " in t:
            t, _, a = t.rpartition(" - ")
        return a

    def current_position(self) -> float | None:
        if not self.has_timeline:
            return None
        if not self.playing:
            return self.position
        return self.position + max(0.0, time.monotonic() - self.sampled_at) * self.rate


class MediaWatcher(threading.Thread):
    """后台轮询 SMTC,维护最新 MediaInfo。"""

    def __init__(self, config):
        super().__init__(daemon=True, name="media-watcher")
        self.config = config
        self._lock = threading.Lock()
        self._info: MediaInfo | None = None
        self._version = 0
        self._none_streak = 0
        self._stop = threading.Event()
        self._use_winrt = True
        try:
            import winrt.windows.media.control  # noqa: F401
        except Exception as exc:
            log.info("WinRT 不可用,改用 PowerShell 辅助进程: %s", exc)
            self._use_winrt = False

    # ── 读取 ────────────────────────────────────────────
    def latest(self) -> MediaInfo | None:
        with self._lock:
            return self._info

    def version(self) -> int:
        with self._lock:
            return self._version

    def _publish(self, info: MediaInfo | None) -> None:
        with self._lock:
            self._info = info
            self._version += 1

    def stop(self) -> None:
        self._stop.set()

    # ── 主循环 ──────────────────────────────────────────
    def run(self) -> None:
        interval = self.config.get("media_poll_sec", 0.35)
        if self._use_winrt:
            try:
                ctypes.windll.combase.RoInitialize(1)
            except Exception:
                pass
            failures = 0
            while not self._stop.is_set():
                try:
                    info = asyncio.run(self._poll_once())
                    if info is None:
                        # 系统忙时 SMTC 可能瞬时返回空会话;连续两次为空
                        # 才认定播放真的停止,避免切换应用时歌词条闪隐
                        self._none_streak += 1
                        if self._none_streak >= 2:
                            self._publish(None)
                    else:
                        self._none_streak = 0
                        self._publish(info)
                    failures = 0
                except Exception as exc:
                    failures += 1
                    log.debug("SMTC 轮询失败: %s", exc)
                    if failures >= 5:        # 偶发失败保留旧值,避免闪烁
                        log.warning("WinRT 连续失败,切换 PowerShell 辅助进程")
                        self._use_winrt = False
                        break
                self._stop.wait(interval)
        if not self._use_winrt:
            self._run_ps()

    # ── WinRT 后端 ──────────────────────────────────────
    async def _poll_once(self) -> MediaInfo | None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Mgr)
        mgr = await Mgr.request_async()
        sessions = list(mgr.get_sessions())
        session, matched = self._pick(sessions)
        if session is None:
            return None
        pi = session.get_playback_info()
        tl = session.get_timeline_properties()
        mp = await session.try_get_media_properties_async()

        playing = pi.playback_status == STATUS_PLAYING
        rate = pi.playback_rate or 1.0
        if rate <= 0:
            rate = 1.0
        pos = tl.position.total_seconds() if tl.position else 0.0
        try:
            lu = tl.last_updated_time  # naive 本地 datetime(pywinrt 3.x 投影)
            if lu:
                elapsed = (datetime.now() - lu).total_seconds()
                if playing and elapsed > 0:
                    pos += elapsed * rate
        except Exception:
            pass
        return MediaInfo(app_id=session.source_app_user_model_id or "",
                         title=mp.title or "", artist=mp.artist or "",
                         playing=playing, position=max(0.0, pos),
                         sampled_at=time.monotonic(), rate=rate)

    def _pick(self, sessions):
        prefer = [p.lower() for p in self.config.get("prefer_app_ids", ["qqmusic"])]
        allow_other = self.config.get("allow_other_media", True)
        matched = None
        for s in sessions:
            low = (s.source_app_user_model_id or "").lower()
            if any(p in low for p in prefer):
                matched = s
                break
        if matched is not None:
            return matched, True
        if not allow_other:
            return None, False
        for s in sessions:
            if s.get_playback_info().playback_status == STATUS_PLAYING:
                return s, False
        return (sessions[0], False) if sessions else (None, False)

    # ── PowerShell 后端(备用) ───────────────────────────
    def _run_ps(self) -> None:
        prefer = "|".join(self.config.get("prefer_app_ids", ["qqmusic"]))
        script = _PS_TEMPLATE.replace("__PREFER__", prefer)
        script_path = Path(__file__).parent / "_smtc_stream.ps1"
        try:
            script_path.write_text(script, encoding="utf-8-sig")
        except OSError:
            pass
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(script_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    if self._stop.is_set():
                        proc.kill()
                        break
                    try:
                        data = json.loads(line.strip())
                    except Exception:
                        continue
                    if not data.get("found"):
                        self._publish(None)
                        continue
                    self._publish(MediaInfo(
                        app_id=data.get("appid", ""), title=data.get("title", ""),
                        artist=data.get("artist", ""), playing=bool(data.get("playing")),
                        position=float(data.get("position", 0.0)),
                        sampled_at=time.monotonic(),
                        rate=float(data.get("rate", 1.0) or 1.0)))
                proc.wait(timeout=5)
            except Exception as exc:
                log.debug("PS 辅助进程异常: %s", exc)
            self._stop.wait(3.0)


def find_qqmusic_window_title() -> MediaInfo | None:
    """最后手段:SMTC 完全不可用时读 QQ音乐窗口标题(无进度)。"""
    try:
        import win32gui
        import win32process

        result = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                import win32api
                handle = win32api.OpenProcess(0x0410, False, pid)  # QUERY|READ
                exe = win32process.GetModuleFileNameEx(handle, 0)
                win32api.CloseHandle(handle)
            except Exception:
                return True
            name = exe.rsplit("\\", 1)[-1].lower()
            if name in ("qqmusic.exe", "qqmusicdaw.exe", "qq music.exe"):
                result.append(title)
            return True

        win32gui.EnumWindows(cb, None)
        if not result:
            return None
        title = result[0]
        for tail in (" - QQ音乐", "QQ音乐", " - 腾讯音乐"):
            title = title.replace(tail, "")
        if " - " not in title:
            return MediaInfo(app_id="window-title", title=title, artist="",
                             playing=True, has_timeline=False)
        song, _, artist = title.rpartition(" - ")
        return MediaInfo(app_id="window-title", title=song.strip(),
                         artist=artist.strip(), playing=True, has_timeline=False)
    except Exception:
        return None
