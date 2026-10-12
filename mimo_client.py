# -*- coding: utf-8 -*-
"""小米 MiMo 控制台 余额/今日用量 取数 + 会话管理。

数据源(platform.xiaomimimo.com 控制台,需小米账号 SSO 会话,API Key 不可代替):
  GET  /api/v1/balance                       -> 现金/赠送余额
  GET  /api/v1/usage                         -> 累计与当月消费、总 Token
  POST /api/v1/usage/detail/list?api-platform_ph=<ph>  {year,month}
                                             -> 每日消费金额/Token(取 UTC 今日)

会话策略:
  * 登录一次(playwright 有头浏览器,用户手工登录),cookie 持久化在
    .mimo_profile/ (浏览器 profile) 与 mimo_session.json (storage_state 快照);
  * 日常取数走纯 HTTP(urllib + 快照 cookie),不拉起浏览器;
  * 收到 401 时用无头浏览器访问 /api/v1/genLoginUrl 走 SSO 静默续期
    (账号会话有效期内全自动,见 account.xiaomi.com → /sts 链);
  * 账号会话也过期时状态置 need_login,等用户通过托盘菜单再次登录。

会话时效: platform 侧 serviceToken/ph/slh 有效期 24 小时,靠上述 SSO 续期滚动。
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import app_config

log = logging.getLogger("mimo")

BASE = "https://platform.xiaomimimo.com"
PROFILE_DIR = app_config.ROOT / ".mimo_profile"      # playwright 持久化 profile
STATE_PATH = app_config.ROOT / "mimo_session.json"   # cookie 快照(纯 HTTP 取数用)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


class SessionExpired(Exception):
    """平台会话失效(401)——需要 SSO 续期或重新登录。"""


class NeedLogin(Exception):
    """小米账号会话也失效,必须用户手工登录一次。"""


def _now_utc_date() -> str:
    # 控制台口径:日期为 UTC 时间(页面提示"日期为 UTC 时间")
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False),
                              encoding="utf-8")
    except OSError as exc:
        log.warning("保存会话快照失败: %s", exc)


def _cookie_header(state: dict) -> tuple[str, str | None]:
    """从 playwright storage_state 拼 Cookie 头,返回 (header, ph值)。"""
    now = time.time()
    host = "platform.xiaomimimo.com"
    parts: list[str] = []
    ph = None
    for c in state.get("cookies", []):
        dom = c.get("domain", "").lstrip(".")
        if not dom or not (host == dom or host.endswith("." + dom)):
            continue
        exp = c.get("expires", -1)
        if exp is not None and exp != -1 and exp < now:
            continue
        name, value = c.get("name", ""), str(c.get("value", ""))
        parts.append(f"{name}={value}")
        if name == "api-platform_ph":
            ph = value
    return "; ".join(parts), ph


def _http(method: str, path: str, cookie: str, body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, data=body, method=method, headers={
        "Cookie": cookie,
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE + "/console/balance",
        "x-timeZone": "Asia/Shanghai",
    })
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read().decode("utf-8", "replace") if exc.fp else "")


def _get_json(method: str, path: str, cookie: str, body: bytes | None = None):
    status, text = _http(method, path, cookie, body)
    if status in (401, 403):
        raise SessionExpired(f"{method} {path} -> {status}")
    if status != 200:
        raise RuntimeError(f"{method} {path} -> {status}: {text[:160]}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {path} 响应非 JSON: {text[:120]}") from exc
    if data.get("code") not in (0, 200):
        raise RuntimeError(f"{method} {path} code={data.get('code')}: "
                           f"{str(data.get('message'))[:120]}")
    return data.get("data")


def fetch() -> dict:
    """纯 HTTP 拉取余额/用量;会话失效抛 SessionExpired。

    返回: {balance, month_cost, total_cost, today_tokens, today_cost, ts}
    """
    state = _load_state()
    if not state:
        raise SessionExpired("无会话快照")
    cookie, ph = _cookie_header(state)
    if not cookie:
        raise SessionExpired("快照中无平台 cookie")

    balance = _get_json("GET", "/api/v1/balance", cookie)
    usage = _get_json("GET", "/api/v1/usage", cookie)
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    body = json.dumps({"year": utc_now.year, "month": utc_now.month},
                      ensure_ascii=False).encode("utf-8")
    path = "/api/v1/usage/detail/list"
    if ph:
        path += "?api-platform_ph=" + urllib.parse.quote(ph, safe="")
    rows = _get_json("POST", path, cookie, body) or []

    today = _now_utc_date()
    today_rows = [r for r in rows if isinstance(r, dict) and r.get("date") == today]
    return {
        "balance": float(balance.get("balance") or 0),
        "gift_balance": float(balance.get("giftBalance") or 0),
        "total_cost": float((usage.get("costUsage") or {}).get("totalCost") or 0),
        "month_cost": float((usage.get("costUsage") or {}).get("currentMonthCost") or 0),
        "today_tokens": sum(int(r.get("totalToken") or 0) for r in today_rows),
        "today_cost": sum(float(r.get("consumedAmount") or 0) for r in today_rows),
        "ts": time.time(),
    }


# ── 浏览器会话(登录 / SSO 续期) ─────────────────────────────
def _pw():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def _has_platform_session(page, timeout_ms: int) -> bool:
    """轮询等待平台 cookie 就位(SSO 链完成后 set)。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if page.evaluate("() => document.cookie.includes('api-platform_ph=')"):
                return True
        except Exception:
            pass  # 正在跨域跳转
        page.wait_for_timeout(400)
    return False


def renew(timeout_ms: int = 25000) -> bool:
    """无头走一遍 genLoginUrl SSO 链续期。成功返回 True;账号会话失效返回 False。"""
    PROFILE_DIR.mkdir(exist_ok=True)
    with _pw() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True, viewport={"width": 960, "height": 640})
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{BASE}/api/v1/genLoginUrl"
                      f"?currentPath={urllib.parse.quote('/console/balance')}",
                      wait_until="domcontentloaded", timeout=timeout_ms)
            # 正常链:302 → account.xiaomimimo.com(会话在,静默) → /sts → 控制台
            ok = _has_platform_session(page, timeout_ms)
            if ok:
                _save_state(ctx.storage_state())
                log.info("MiMo 会话 SSO 续期成功")
            else:
                log.warning("MiMo SSO 续期未拿到会话,当前: %s", page.url[:120])
            return ok
        finally:
            ctx.close()


def login_interactive(timeout_sec: int = 300) -> bool:
    """有头浏览器让用户登录一次(托盘菜单触发);成功后保存会话快照。"""
    PROFILE_DIR.mkdir(exist_ok=True)
    with _pw() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False,
            viewport={"width": 960, "height": 720},
            args=["--window-position=120,120"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(BASE + "/console/balance", wait_until="domcontentloaded",
                          timeout=30000)
            except Exception:
                pass
            log.info("等待 MiMo 登录(最多 %ds)...", timeout_sec)
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                try:
                    cookie, _ = _cookie_header(ctx.storage_state())
                    if cookie and "api-platform_ph=" in cookie:
                        # 再确认真能取到数据(光有 cookie 不够)
                        _save_state(ctx.storage_state())
                        try:
                            fetch()
                            _save_state(ctx.storage_state())
                            log.info("MiMo 登录成功并验证取数通过")
                            return True
                        except SessionExpired:
                            STATE_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
                page.wait_for_timeout(800)
            log.warning("MiMo 登录超时")
            return False
        finally:
            try:
                ctx.close()
            except Exception:
                pass


# ══════════════════════════ 后台线程 ══════════════════════════
class MimoWorker(threading.Thread):
    """周期取数 + 401 自动续期 + 登录请求处理。

    状态机: init → fetching → ok / need_login / error
    每次状态或数据变化调用 notify()(由宿主 PostMessage 到 UI 线程)。
    """

    def __init__(self, notify, refresh_sec: float = 180.0):
        super().__init__(daemon=True, name="mimo-worker")
        self.notify = notify
        self.refresh_sec = max(30.0, refresh_sec)
        self.state = "init"
        self.data: dict | None = None
        self.message = ""
        self.version = 0            # 每次更新 +1,UI 侧据此判断重绘
        self._login_req = threading.Event()
        self._stop = threading.Event()

    # ── 对外 ──
    def request_login(self) -> None:
        self._login_req.set()

    def stop(self) -> None:
        self._stop.set()

    def _set(self, state: str, message: str = "") -> None:
        if state != self.state or message != self.message:
            self.state, self.message = state, message
            self.version += 1
            self._notify()

    def _notify(self) -> None:
        try:
            self.notify()
        except Exception:
            pass

    # ── 主循环 ──
    def run(self) -> None:
        first = True
        while not self._stop.is_set():
            if self._login_req.is_set():
                self._login_req.clear()
                self._do_login()
                continue
            interval = self._cycle(first)
            first = False
            # 登录请求可打断等待
            self._stop.wait(interval)

    def _cycle(self, first: bool) -> float:
        try:
            if first:
                self._set("fetching", "正在获取 MiMo 余额...")
            data = fetch()
            self.data = data
            self._set("ok")
            return self.refresh_sec
        except SessionExpired as exc:
            log.info("会话过期(%s),尝试 SSO 续期", exc)
            self._set("fetching", "会话续期中...")
            try:
                if renew():
                    data = fetch()
                    self.data = data
                    self._set("ok")
                    return self.refresh_sec
            except SessionExpired:
                pass
            except Exception as exc2:
                log.warning("SSO 续期异常: %s", exc2)
            self._set("need_login", "小米账号会话已过期")
            return 60.0
        except Exception as exc:
            log.warning("MiMo 取数失败: %s", exc)
            self._set("error", str(exc)[:80])
            return max(60.0, self.refresh_sec / 2)

    def _do_login(self) -> None:
        self._set("fetching", "请在打开的浏览器中登录小米账号")
        try:
            if login_interactive():
                data = fetch()
                self.data = data
                self._set("ok")
            else:
                self._set("need_login", "登录未完成")
        except Exception as exc:
            log.warning("登录流程异常: %s", exc)
            self._set("error", str(exc)[:80])
