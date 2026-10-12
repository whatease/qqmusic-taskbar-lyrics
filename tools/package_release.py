# -*- coding: utf-8 -*-
"""打包分享版 zip:只含源码与说明,自动排除日志/缓存/个人配置。

用法:  python tools/package_release.py
输出:  上一级目录/QQ音乐底栏歌词-分享版.zip
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "QQ音乐底栏歌词"
EXCLUDE_DIRS = {"logs", "lyrics_cache", "__pycache__", ".git"}
EXCLUDE_FILES = {"config.json", "_smtc_stream.ps1", ".zcodeignore"}
EXCLUDE_SUFFIX = {".pyc"}


def main() -> None:
    out = ROOT.parent / f"{DIST_NAME}-分享版.zip"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIX:
                continue
            z.write(p, Path(DIST_NAME) / p.relative_to(ROOT))
            count += 1
    print(f"已生成: {out}")
    print(f"共 {count} 个文件,同学解压后双击 启动QQ音乐底栏歌词.bat 即可")


if __name__ == "__main__":
    main()
