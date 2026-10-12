# -*- coding: utf-8 -*-
"""推送本地代码到 GitHub:优先 git push,失败自动改走 GitHub API 整树同步。

背景:本机到 GitHub 的 git 通道(443)时常被重置,API 更稳。

用法:
  python tools/push_any.py      # 或双击 发布到GitHub.bat

API 模式为"整树重建":以本地索引为准生成完整文件树(自动处理新增/修改/删除),
远端树的 SHA 与本地 HEAD 完全一致;提交历史会与本地产生差异
(远端为单个合并提交),网络恢复后运行 同步GitHub.bat 对齐即可。
"""
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
GH = r"C:\Program Files\GitHub CLI\gh.exe"


def sh(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=ROOT, timeout=timeout)


def git_remote_repo() -> str | None:
    url = sh(["git", "remote", "get-url", "origin"]).stdout.strip()
    if "github.com" not in url:
        return None
    return url.split("github.com")[1].lstrip(":/").removesuffix(".git")


def gh(repo, method, endpoint, payload=None, jq=None, retries=3):
    cmd = [GH, "api", "--method", method, f"repos/{repo}/{endpoint}"]
    if jq:
        cmd += ["--jq", jq]
    if payload is not None:
        cmd += ["--input", "-"]
    last = None
    for i in range(retries):
        try:
            r = subprocess.run(cmd, input=json.dumps(payload) if payload else None,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=90)
            if r.returncode == 0:
                return r.stdout.strip()
            last = r.stderr.strip()[:200]
        except subprocess.TimeoutExpired:
            last = "timeout"
        print(f"    (网络重试 {i + 1}/{retries})", flush=True)
        time.sleep(3)
    raise RuntimeError(f"{method} {endpoint} 失败: {last}")


def push_via_api(repo: str) -> None:
    sh(["git", "config", "core.quotepath", "false"])
    ls = sh(["git", "ls-files", "-s"]).stdout
    local: dict[str, tuple[str, str]] = {}
    for line in ls.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, sha, stage = meta.split()
        if stage == "0":
            local[path] = (mode, sha)

    remote_sha = gh(repo, "GET", "git/refs/heads/main", jq=".object.sha")
    local_head = sh(["git", "rev-parse", "HEAD"]).stdout.strip()
    local_tree = sh(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip()
    remote_tree = gh(repo, "GET", f"git/commits/{remote_sha}", jq=".tree.sha")
    if remote_tree == local_tree:
        print("远端内容已是最新,无需推送")
        return

    remote: dict[str, str] = {}
    for item in json.loads(gh(repo, "GET",
                              f"git/trees/{remote_sha}?recursive=1")).get("tree", []):
        if item["type"] == "blob":
            remote[item["path"]] = item["sha"]

    need = [p for p, (_, sha) in local.items() if remote.get(p) != sha]
    print(f"差异文件 {len(need)} 个,上传中 ...", flush=True)
    for p in need:
        # 从索引读 blob(与 git add 后的归一化内容一致);直接读工作区
        # 文件会因 CRLF/LF 混合导致 SHA 对不上
        data = subprocess.run(["git", "cat-file", "blob", f":{p}"],
                              cwd=ROOT, capture_output=True).stdout
        blob = json.loads(gh(repo, "POST", "git/blobs",
                             {"content": base64.b64encode(data).decode(),
                              "encoding": "base64"}))["sha"]
        if blob != local[p][1]:
            raise RuntimeError(f"blob SHA 校验失败: {p}")
        print(f"  ↑ {p}", flush=True)
    gone = [p for p in remote if p not in local]
    for p in gone:
        print(f"  ✗ 删除 {p}", flush=True)

    tree_in = [{"path": p, "mode": m, "type": "blob", "sha": s}
               for p, (m, s) in sorted(local.items())]
    new_tree = json.loads(gh(repo, "POST", "git/trees", {"tree": tree_in}))["sha"]
    if new_tree != local_tree:
        raise RuntimeError("树 SHA 校验失败")
    msg = sh(["git", "log", "-1", "--format=%s"]).stdout.strip()
    new_commit = json.loads(gh(repo, "POST", "git/commits",
                               {"message": msg, "tree": new_tree,
                                "parents": [remote_sha]}))["sha"]
    gh(repo, "PATCH", "git/refs/heads/main",
       {"sha": new_commit, "force": False})
    print(f"完成:远端 main = {new_commit[:10]} (内容与本地一致)")
    print("提示:网络恢复后运行 同步GitHub.bat 可对齐完整提交历史")


def main() -> int:
    repo = git_remote_repo()
    if not repo:
        print("origin 不是 GitHub 仓库")
        return 1
    print("尝试 git push ...", flush=True)
    try:
        r = sh(["git", "push", "origin", "main"], timeout=120)
        if r.returncode == 0:
            print("git push 成功")
            return 0
    except subprocess.TimeoutExpired:
        pass
    print("git 通道不可用,改走 GitHub API ...", flush=True)
    push_via_api(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
