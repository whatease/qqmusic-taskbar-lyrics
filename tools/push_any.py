# -*- coding: utf-8 -*-
"""推送本地提交到 GitHub:优先 git push,失败自动改走 GitHub API。

用途:本机到 GitHub 的 git 通道不稳(443 常被重置)时也能发布更新。

用法:
  python tools/push_any.py            # 推送当前分支到 origin
"""
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH = r"C:\Program Files\GitHub CLI\gh.exe"


def sh(cmd, check=False, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", cwd=ROOT, **kw)


def git_remote_repo() -> str | None:
    url = sh(["git", "remote", "get-url", "origin"]).stdout.strip()
    if "github.com" not in url:
        return None
    part = url.split("github.com")[1].lstrip(":/").removesuffix(".git")
    return part


def gh(method, endpoint, payload=None, jq=None):
    cmd = [GH, "api", "--method", method, f"repos/{endpoint}"]
    if jq:
        cmd += ["--jq", jq]
    if payload is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, input=json.dumps(payload) if payload else None,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:400])
    return r.stdout.strip()


def push_via_api(repo: str) -> None:
    subprocess.run(["git", "config", "core.quotepath", "false"], cwd=ROOT)
    remote_sha = gh("GET", f"{repo}/git/refs/heads/main", jq=".object.sha")
    head = sh(["git", "rev-parse", "HEAD"]).stdout.strip()
    if remote_sha == head:
        print("远端已是最新,无需推送")
        return
    # 找出远端缺失的提交链(逐提交回放,保持历史)
    chain, cur = [], head
    while cur and cur != remote_sha:
        chain.append(cur)
        cur = sh(["git", "rev-parse", f"{cur}^"]).stdout.strip()
        if not cur:
            raise RuntimeError("本地与远端历史已分叉,请先运行 同步GitHub.bat")
    chain.reverse()
    base_tree = gh("GET", f"{repo}/git/commits/{remote_sha}", jq=".tree.sha")
    for commit in chain:
        parent = sh(["git", "rev-parse", f"{commit}^"]).stdout.strip()
        files = sh(["git", "diff", "--name-only", parent, commit]).stdout.splitlines()
        entries = []
        for rel in filter(None, files):
            p = ROOT / rel
            if not p.exists():
                continue                      # 删除的文件暂不支持自动同步
            blob = gh("POST", f"{repo}/git/blobs",
                      {"content": base64.b64encode(p.read_bytes()).decode(),
                       "encoding": "base64"}, jq=".sha")
            entries.append({"path": rel, "mode": "100644", "type": "blob",
                            "sha": blob})
        tree = gh("POST", f"{repo}/git/trees",
                  {"base_tree": base_tree, "tree": entries}, jq=".sha")
        msg = sh(["git", "log", "-1", "--format=%s", commit]).stdout.strip()
        newc = gh("POST", f"{repo}/git/commits",
                  {"message": msg, "tree": tree, "parents": [remote_sha]}, jq=".sha")
        gh("PATCH", f"{repo}/git/refs/heads/main", {"sha": newc, "force": False})
        remote_sha, base_tree = newc, tree
        print("已推送:", msg)
    print("完成。远端 main =", remote_sha[:10])
    print("提示:本地历史与远端略有差异,网络恢复后运行 同步GitHub.bat 对齐。")


def main() -> int:
    repo = git_remote_repo()
    if not repo:
        print("origin 不是 GitHub 仓库")
        return 1
    print("尝试 git push ...")
    r = sh(["git", "push", "origin", "main"])
    if r.returncode == 0:
        print("git push 成功")
        return 0
    print("git 通道不可用,改走 GitHub API ...")
    push_via_api(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
