# -*- coding: utf-8 -*-
"""一键发布到 GitHub:检查认证 → 创建仓库 → 推送。

用法(先完成一次 GitHub 登录,二选一):
  A. 本脚本会自动调起 `gh auth login`(浏览器里点一下授权即可);
  B. 或设置环境变量 GH_TOKEN=<你的Personal Access Token> 后运行。

然后: python tools/publish_github.py [仓库名]
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "qqmusic-taskbar-lyrics"


def find_gh() -> str | None:
    for p in (r"C:\Program Files\GitHub CLI\gh.exe",
              shutil.which("gh")):
        if p and Path(p).exists():
            return str(p)
    return None


def run(cmd, **kw):
    print(">", " ".join(cmd) if isinstance(cmd, list) else cmd)
    return subprocess.run(cmd, **kw)


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    gh = find_gh()
    if gh is None:
        print("未找到 gh CLI。请先安装:winget install GitHub.cli")
        print("或手动发布:在 GitHub 网页新建空仓库,然后:")
        print(f'  git remote add origin https://github.com/<你的用户名>/{repo}.git')
        print("  git push -u origin main")
        return 1

    # 1. 认证
    r = run([gh, "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("尚未登录 GitHub,即将启动登录流程(浏览器里点一下授权)...")
        if run([gh, "auth", "login", "--web", "--git-protocol", "https"]).returncode != 0:
            print("登录未完成。也可设置 GH_TOKEN 环境变量后重试。")
            return 1

    # 2. 创建仓库并推送(已存在则只推送)
    r = run([gh, "repo", "view", repo], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"远端仓库 {repo} 已存在,直接推送")
        run(["git", "remote", "remove", "origin"], capture_output=True)
        user = run([gh, "api", "user", "--jq", ".login"],
                   capture_output=True, text=True).stdout.strip()
        run(["git", "remote", "add", "origin",
             f"https://github.com/{user}/{repo}.git"])
        run(["git", "push", "-u", "origin", "main"], cwd=ROOT, check=False)
    else:
        run([gh, "repo", "create", repo, "--public", "--source", str(ROOT),
             "--push", "--description",
             "QQ音乐任务栏卡拉OK歌词:粉/橘双色、图标自适应、跟唱提前量"],
            check=False)

    print("\n完成!仓库地址: https://github.com/<你的用户名>/" + repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
