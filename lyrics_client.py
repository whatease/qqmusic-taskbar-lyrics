# -*- coding: utf-8 -*-
"""QQ音乐歌词获取:搜索定位歌曲 → 下载LRC → 解析为带时间轴的行。

来源均为QQ音乐公开 Web 接口,无需登录:
  搜索  https://u.y.qq.com/cgi-bin/musicu.fcg  (music.search.SearchCgiService)
  歌词  https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg
带本地缓存与负缓存,失败不阻塞渲染线程。
"""
from __future__ import annotations

import base64
import bisect
import difflib
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("lyrics")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://y.qq.com/"

_SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_LYRIC_URL = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
              "?songmid={mid}&g_tk=5381&loginUin=0&hostUin=0&format=json"
              "&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0")

_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_TAG = re.compile(r"^\[(ti|ar|al|by|offset|kana|hash|total|ve|re):(.*)\]$", re.I)

NEG_TTL = 600  # 负缓存(搜不到)有效期,秒


def _http_get(url: str, timeout: float = 6.0) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _normalize(s: str) -> str:
    s = s or ""
    s = re.sub(r"[(\[【（].*?[)\]】）]", " ", s)     # 去括注 (Live) 等
    s = re.sub(r"[\W_]+", "", s, flags=re.UNICODE)  # 去空白与符号
    return s.lower()


def _clean_search_text(title: str, artist: str) -> str:
    """去掉标题里混入的歌手/尾缀,拼出干净搜索词。"""
    t = (title or "").strip()
    a = (artist or "").strip()
    if not a and " - " in t:
        # 某些播放器把歌手塞进标题
        t, _, tail = t.rpartition(" - ")
        if len(tail) <= 24:
            a = tail
    t = re.sub(r"[(\[【（][^)\]】）]*(Live|live|现场|Remix|翻自|Cover|cover)[^)\]】）]*[)\]】）]", " ", t)
    return f"{t} {a}".strip() or t


@dataclass
class LyricLine:
    start: float                      # 秒
    end: float = 0.0
    text: str = ""
    words: list | None = None         # [(t0, t1, text)],逐字时轴(预留)


@dataclass
class Lyrics:
    key: str
    song_mid: str = ""
    lines: list[LyricLine] = field(default_factory=list)
    source: str = "lrc"

    def line_at(self, t: float, grace: float = 2.0, lead: float = 0.0):
        """返回 (line, frac)。

        行选择按 t+lead 提前(歌词先于歌声出现,方便跟唱);
        卡拉OK光标进度按真实 t 计算 —— 提前期内整句保持橘色,
        唱到哪粉到哪,不会"未唱先粉"。行间休止超过 grace 秒
        返回 (None, 0) 切回标题模式。
        """
        if not self.lines:
            return None, 0.0
        tl = t + lead
        i = bisect.bisect_right([ln.start for ln in self.lines], tl) - 1
        if i < 0:
            return None, 0.0
        ln = self.lines[i]
        if tl > ln.end + grace:
            return None, 0.0
        dur = max(0.25, ln.end - ln.start)
        frac = (t - ln.start) / dur            # 提前期内为负,钳到0
        return ln, min(1.0, max(0.0, frac))


def parse_lrc(text: str) -> list[LyricLine]:
    offset_ms = 0.0
    for m in re.finditer(r"^\[offset:\s*([+-]?\d+)\s*\]", text, re.M | re.I):
        offset_ms = float(m.group(1))
    raw: list[tuple[float, str]] = []
    for line in text.splitlines():
        tags = _TIME_TAG.findall(line)
        if not tags:
            continue
        content = _TIME_TAG.sub("", line).strip()
        for mm, ss, ff in tags:
            frac = float(ff) / (10 ** len(ff)) if ff else 0.0
            t = int(mm) * 60 + int(ss) + frac - offset_ms / 1000.0
            raw.append((max(0.0, t), content))
    raw.sort(key=lambda x: x[0])

    lines: list[LyricLine] = []
    for t, content in raw:
        text_clean = re.sub(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>", "", content).strip()
        if not text_clean:
            # 空行仅作为上一行的结束标记
            if lines:
                lines[-1].end = t
            continue
        if lines and abs(t - lines[-1].start) < 0.05 and lines[-1].text == text_clean:
            continue  # 去重
        lines.append(LyricLine(start=t, end=0.0, text=text_clean))
    for i, ln in enumerate(lines):
        nxt = lines[i + 1].start if i + 1 < len(lines) else ln.start + 8.0
        ln.end = nxt if ln.end <= 0 else min(ln.end, nxt)
        if ln.end <= ln.start:
            ln.end = ln.start + max(2.0, nxt - ln.start)
    return lines


class LyricsClient:
    """搜索 + 下载 + 缓存。所有方法线程安全,失败返回 None。"""

    def __init__(self, cache_dir: Path, try_qrc: bool = False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.try_qrc = try_qrc
        self._lock = threading.Lock()

    # ── 缓存 ────────────────────────────────────────────
    def _cache_path(self, key: str) -> Path:
        import hashlib
        return self.cache_dir / (hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")

    def _load_cache(self, key: str):
        p = self._cache_path(key)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if data.get("none"):
            if time.time() - data.get("ts", 0) < NEG_TTL:
                return "NEG"
            return None
        lines = [LyricLine(**ln) for ln in data.get("lines", [])]
        return Lyrics(key=data.get("key", key), song_mid=data.get("mid", ""),
                      lines=lines, source=data.get("source", "lrc"))

    def _save_cache(self, key: str, value):
        payload = ({"none": True, "ts": time.time()} if value is None else
                   {"key": value.key, "mid": value.song_mid, "source": value.source,
                    "lines": [{"start": ln.start, "end": ln.end, "text": ln.text,
                               "words": ln.words} for ln in value.lines]})
        try:
            self._cache_path(key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ── 对外 ────────────────────────────────────────────
    def fetch(self, key: str, title: str, artist: str):
        with self._lock:
            cached = self._load_cache(key)
            if cached == "NEG":
                return None
            if cached is not None:
                return cached
            lyrics = self._fetch_online(key, title, artist)
            self._save_cache(key, lyrics)
            return lyrics

    def _fetch_online(self, key: str, title: str, artist: str):
        query = _clean_search_text(title, artist)
        if not query:
            return None
        mid = self._search(query, title, artist)
        if not mid:
            mid = self._search(title or query, title, artist)  # 退化:仅标题
        if not mid:
            log.info("未匹配到歌曲: %s", query)
            return None
        lrc_text = self._download_lrc(mid)
        if not lrc_text:
            return None
        lines = parse_lrc(lrc_text)
        if not lines:
            return None
        log.info("歌词就绪 mid=%s 行数=%d (%s)", mid, len(lines), query)
        return Lyrics(key=key, song_mid=mid, lines=lines)

    # ── QQ音乐接口 ───────────────────────────────────────
    def _search(self, query: str, title: str, artist: str):
        payload = {"req": {"module": "music.search.SearchCgiService",
                           "method": "DoSearchForQQMusicDesktop",
                           "param": {"search_type": 0, "query": query,
                                     "page_num": 1, "num_per_page": 8}}}
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                _SEARCH_URL, data=body,
                headers={"User-Agent": UA, "Referer": REFERER,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            songs = data["req"]["data"]["body"]["song"]["list"]
        except Exception as exc:
            log.warning("搜索失败 %s: %s", query, exc)
            return None
        if not songs:
            return None

        nt, na = _normalize(title), _normalize(artist)
        best, best_score = None, 0.0
        for s in songs:
            name_sim = difflib.SequenceMatcher(None, _normalize(s.get("name", "")), nt).ratio()
            singers = "".join(x.get("name", "") for x in s.get("singer", []))
            artist_sim = difflib.SequenceMatcher(None, _normalize(singers), na).ratio() if na else 0.5
            score = name_sim * 2.0 + artist_sim
            if score > best_score:
                best, best_score = s.get("mid"), score
        log.debug("搜索 %s → %s (score=%.2f)", query, best, best_score)
        # 名称至少要像同一首歌
        name_only = best_score / 3.0 * 2.0  # 还原名称占比
        return best if name_only >= 0.55 or best_score >= 2.2 else None

    def _download_lrc(self, mid: str):
        try:
            data = json.loads(_http_get(_LYRIC_URL.format(mid=mid)))
            b64 = data.get("lyric") or ""
            if not b64:
                return ""
            return base64.b64decode(b64).decode("utf-8", "replace")
        except Exception as exc:
            log.warning("歌词下载失败 %s: %s", mid, exc)
            return ""
