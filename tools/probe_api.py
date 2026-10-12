# -*- coding: utf-8 -*-
"""探针脚本v2:用 musicu.fcg 搜索 + LRC + QRC 逐字歌词(仅标准库)。"""
import base64
import json
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def post_json(url, payload, referer="https://y.qq.com/"):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": UA, "Referer": referer,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def get(url, referer="https://y.qq.com/"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def search(query):
    url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    payload = {"req": {"module": "music.search.SearchCgiService",
                       "method": "DoSearchForQQMusicDesktop",
                       "param": {"search_type": 0, "query": query,
                                 "page_num": 1, "num_per_page": 5}}}
    data = json.loads(post_json(url, payload))
    body = data["req"]["data"]["body"]
    songs = body["song"]["list"]
    print("SEARCH_OK count:", len(songs))
    out = []
    for s in songs[:3]:
        mid = s.get("mid")
        name = s.get("name")
        singers = "/".join(x["name"] for x in s.get("singer", []))
        print("  SONG:", mid, name, singers)
        out.append(mid)
    return out


def lrc(mid):
    url = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=" + mid +
           "&g_tk=5381&loginUin=0&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8"
           "&notice=0&platform=yqq.json&needNewCode=0")
    ldata = json.loads(get(url))
    lyric = base64.b64decode(ldata["lyric"]).decode("utf-8", "replace")
    print("LRC_OK retcode:", ldata.get("retcode"), "len:", len(lyric))
    print("LRC_SAMPLE:", repr(lyric[:130]))


def qrc(mid):
    url = ("https://c.y.qq.com/qqmusic/fcgi-bin/lyric_download.fcg?version=15&miniversion=82"
           "&source=ccc&loginUin=0&charset=utf8&accent_id=1&cfg_tail=1&loginflag=1&detail=1"
           "&songmid=" + mid)
    qtxt = get(url)
    print("QRC_RAW_HEAD:", qtxt[:160].replace("\n", " "))
    try:
        qdata = json.loads(qtxt)
        content = qdata.get("content")
        if isinstance(content, str):
            content = json.loads(content)
        qrc_b64 = (content or {}).get("qrc") or ""
        print("QRC_FIELD len:", len(qrc_b64), "code:", qdata.get("code"))
        if qrc_b64:
            with open(__file__.replace("\\", "/").rsplit("/", 1)[0] + "/_qrc_sample.b64", "w") as f:
                f.write(qrc_b64)
            print("QRC saved")
    except Exception as exc:  # noqa: BLE001
        print("QRC_PARSE_FAIL:", exc)


if __name__ == "__main__":
    mids = search("晴天 周杰伦")
    if mids:
        lrc(mids[0])
        qrc(mids[0])
