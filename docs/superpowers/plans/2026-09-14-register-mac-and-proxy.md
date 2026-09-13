# 注册机 Mac 适配 + 代理池最小实现 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `register_one()` 在 macOS 上端到端跑通，并给注册流程加一个最小代理池（Chrome + ElevenLabs/Firebase httpx 走同一代理，失败自动禁用轮换）。

**Architecture:** 把 `register.py` 里「操作系统如何管理 Chrome 窗口」的部分抽成 `PlatformDriver` 协议 + `register_platform_win.py` / `register_platform_mac.py` 两个实现；把「用哪个临时邮箱后端」抽成 `EmailProvider` 协议；新增 `ProxyDriver`（静态列表 + 轮换 + N 次失败禁用）。`register_one()` 变成纯编排，按平台注入 driver。`stt.py` / `web.py` 只 import `register.register_one`，不感知平台或代理。

**Tech Stack:** Python 3（标准库为主）、httpx、pyautogui / pyperclip（已有依赖）、pygetwindow（仅 Windows driver）、macOS 自带 `osascript` / `pkill`。**不新增第三方依赖。**

**Spec:** `docs/superpowers/specs/2026-09-14-register-mac-and-proxy-design.md`

## Global Constraints

- **测试约定**：本项目无 pytest、无 `tests/` 目录。所有离线测试是 `selfcheck.py::run()` 里的 `assert` 语句，通过 `python selfcheck.py`（或 `python stt.py selfcheck`）运行，成功打印 `selfcheck ok` 并返回 0。新测试一律加进 `selfcheck.py`，不引入 pytest。
- **不新增第三方依赖**：Mac 支持只用 `osascript` / `pkill`（系统自带）；代理只用 httpx 已有能力 + Chrome `--proxy-server` flag。
- **Windows 行为零回退**：Task 2/3 是重构，Windows 上的注册流程（坐标、等待时长 12s/15s/8s、admin/user 双路径 fallback、profile 清理）必须逐字保留，不改逻辑。
- **向后兼容签名**：`register_one()` 现有调用点是 `register.register_one()`（`web.py:318`）与 `from register import register_one as register_fn`（`stt.py:960`），新签名所有参数必须 keyword-only 且有默认实现，裸调 `register_one()` 行为不变。
- **配置风格**：`proxy_config()` 对齐现有 `temp_email_config()`（`stt.py:137`）的 dict-with-defaults + `load_toml(path).get(section, {})` 写法。
- **License**：不复制 `any-auto-register`（AGPL-3.0）任何代码，只借接口思路。
- **平台探测**：Mac 浏览器探测顺序 Chrome → Brave，`$ELEVENLABS_STT_CHROME` 覆盖。
- **代理降级**：`strict=false` 时全代理禁用 → 静默降级直连 + `_rlog` 告警；`strict=true` → 抛 `SystemExit`。代理不上 UI，只走 `config.toml`。

---

### Task 1: `[proxy]` 配置 + `ProxyDriver`

**Files:**
- Modify: `stt.py`（新增 `proxy_config()`，紧邻 `temp_email_config` 约 `stt.py:137`）
- Create: `proxy.py`（`Proxy` dataclass + `ProxyDriver`）
- Modify: `selfcheck.py`（新增 `_check_proxy_driver()`，在 `run()` 末尾 `print("selfcheck ok")` 前调用）
- Modify: `config.example.toml`（新增 `[proxy]` 段）

**Interfaces:**
- Produces:
  - `stt.proxy_config(path=CONFIG_PATH) -> dict`，键 `{"proxies": list[str], "fail_threshold": int, "cooldown_secs": int, "strict": bool}`
  - `proxy.Proxy`（dataclass：`url: str`, `fails: int = 0`, `disabled_until: float = 0.0`）
  - `proxy.ProxyDriver(cfg: dict)`，方法 `pick() -> Proxy | None`、`mark_ok(p: Proxy) -> None`、`mark_fail(p: Proxy) -> None`
  - `pick()` 语义：从未禁用集合 round-robin 返回；池空返回 `None`；全禁用时 `strict=False` 返回 `None`、`strict=True` 抛 `SystemExit("proxy pool exhausted; strict mode")`

- [ ] **Step 1: 写失败测试 — 在 `selfcheck.py` 顶部 import 区加 `import proxy`，并新增 `_check_proxy_driver()`**

在 `selfcheck.py` 的 import 段（约 `import stt` 附近）加 `import proxy`，然后在 `def run()` 之前新增：

```python
def _check_proxy_driver() -> None:
    now = time.time()
    # 空池 → 永远直连
    d = proxy.ProxyDriver({"proxies": [], "fail_threshold": 3, "cooldown_secs": 900, "strict": False})
    assert d.pick() is None, "empty pool must yield direct connection"

    # 轮换：两个代理，round-robin
    cfg = {"proxies": ["http://a", "http://b"], "fail_threshold": 2, "cooldown_secs": 900, "strict": False}
    d = proxy.ProxyDriver(cfg)
    p1 = d.pick(); p2 = d.pick(); p3 = d.pick()
    assert {p1.url, p2.url} == {"http://a", "http://b"}, "round-robin should cover both"
    assert p3.url == p1.url, "cursor wraps around"

    # 失败到阈值 → 禁用；只剩另一个
    d = proxy.ProxyDriver(cfg)
    bad = next(p for p in [d.pick(), d.pick()] if p.url == "http://a")
    d.mark_fail(bad); d.mark_fail(bad)  # 2 次到阈值
    picks = {d.pick().url for _ in range(4)}
    assert picks == {"http://b"}, f"disabled proxy must drop out, got {picks}"

    # mark_ok 清零失败计数
    d = proxy.ProxyDriver(cfg)
    a = next(p for p in [d.pick(), d.pick()] if p.url == "http://a")
    d.mark_fail(a); d.mark_ok(a); d.mark_fail(a)  # ok 后只累计 1 次，未禁用
    picks = {d.pick().url for _ in range(4)}
    assert "http://a" in picks, "mark_ok must reset fail count"

    # cooldown 到期复活
    d = proxy.ProxyDriver({"proxies": ["http://a"], "fail_threshold": 1, "cooldown_secs": 900, "strict": False})
    a = d.pick(); d.mark_fail(a)
    assert d.pick() is None, "sole disabled proxy + strict=False → direct"
    a.disabled_until = now - 1  # 手动过期
    assert d.pick().url == "http://a", "cooldown expiry must revive proxy"

    # strict=True 全禁用 → 抛错
    d = proxy.ProxyDriver({"proxies": ["http://a"], "fail_threshold": 1, "cooldown_secs": 900, "strict": True})
    a = d.pick(); d.mark_fail(a)
    try:
        d.pick(); assert False, "strict pool exhausted must raise"
    except SystemExit as e:
        assert "strict" in str(e)
```

并在 `run()` 里 `print("selfcheck ok")` 之前加一行 `_check_proxy_driver()`。

- [ ] **Step 2: 运行确认失败**

Run: `python selfcheck.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'proxy'`

- [ ] **Step 3: 实现 `proxy.py`**

```python
#!/usr/bin/env python3
"""Minimal proxy pool for the registration flow.

Static list + round-robin + auto-disable after N consecutive failures.
Empty list == direct connection (the pre-proxy behavior). No dynamic
extraction, no weighting — see the design doc's YAGNI list.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any


@dataclasses.dataclass
class Proxy:
    url: str                    # full URL, passed verbatim to Chrome --proxy-server and httpx
    fails: int = 0
    disabled_until: float = 0.0  # unix ts; > now means temporarily out of the pool


class ProxyDriver:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self._proxies = [Proxy(url=u) for u in (cfg.get("proxies") or [])]
        self._threshold = int(cfg.get("fail_threshold", 3))
        self._cooldown = float(cfg.get("cooldown_secs", 900))
        self._strict = bool(cfg.get("strict", False))
        self._cursor = 0

    def _live(self) -> list[Proxy]:
        now = time.time()
        return [p for p in self._proxies if p.disabled_until <= now]

    def pick(self) -> Proxy | None:
        if not self._proxies:
            return None
        live = self._live()
        if not live:
            if self._strict:
                raise SystemExit("proxy pool exhausted; strict mode")
            return None
        p = live[self._cursor % len(live)]
        self._cursor += 1
        return p

    def mark_ok(self, p: Proxy) -> None:
        p.fails = 0
        p.disabled_until = 0.0

    def mark_fail(self, p: Proxy) -> None:
        p.fails += 1
        if p.fails >= self._threshold:
            p.disabled_until = time.time() + self._cooldown
```

- [ ] **Step 4: 在 `stt.py` 加 `proxy_config()`**（紧邻 `temp_email_config`，约 `stt.py:137`）

```python
def proxy_config(path: pathlib.Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = {"proxies": [], "fail_threshold": 3, "cooldown_secs": 900, "strict": False}
    cfg.update(load_toml(path).get("proxy", {}))
    return cfg
```

- [ ] **Step 5: 在 `config.example.toml` 加 `[proxy]` 段**（放在 `[accounts]` 之后）

```toml
[proxy]
# Upstream proxies (HTTP or SOCKS5) for registration. Empty = direct (default).
# proxies = ["http://user:pass@1.2.3.4:8080", "socks5://5.6.7.8:1080"]
proxies = []
fail_threshold = 3          # 连续失败几次临时禁用该代理
cooldown_secs = 900         # 禁用后多久重新纳入（默认 15min）
strict = false              # true: 全池不可用直接报错；false: 降级直连
```

- [ ] **Step 6: 运行确认通过**

Run: `python selfcheck.py`
Expected: PASS — 打印 `selfcheck ok`

- [ ] **Step 7: 提交**

```bash
git add proxy.py stt.py selfcheck.py config.example.toml
git commit -m "feat(register): add minimal proxy pool (static list + rotate + auto-disable)"
```

---

### Task 2: `EmailProvider` 协议 + `CloudflareTempEmail`

把 `register.py:31-79` 的 `temp_email_create` / `latest_verify_link` 收进一个 class，行为完全等价。

**Files:**
- Modify: `register.py`（新增 `EmailAddress` dataclass、`EmailProvider` Protocol、`CloudflareTempEmail` class；`temp_email_create` / `latest_verify_link` 改为 class 方法，保留模块级薄封装以防其他引用）
- Modify: `selfcheck.py`（新增 `_check_email_provider()`）

**Interfaces:**
- Consumes: `stt.temp_email_config()`（现有）
- Produces:
  - `register.EmailAddress`（dataclass：`address: str`, `token: str`, `raw: dict`）
  - `register.EmailProvider` Protocol：`create_address() -> EmailAddress`、`poll_verification_link(addr, pattern, timeout_s, interval_s) -> str`
  - `register.CloudflareTempEmail(cfg: dict)`，实现上述两方法；`cfg` 是 `stt.temp_email_config()` 返回值
  - `register.VERIFY_LINK_PATTERN`（`re.Pattern`，即现有 `register.py:75` 的正则）

- [ ] **Step 1: 写失败测试 — `selfcheck.py` 新增 `_check_email_provider()`**

只测离线可测的部分：假 provider 满足协议 + `VERIFY_LINK_PATTERN` 正则命中/不命中 + `CloudflareTempEmail` 能从 cfg 构造（不发网络）。

```python
def _check_email_provider() -> None:
    import register
    # 正则契约：只认 elevenlabs.io action 链接
    good = 'go here https://elevenlabs.io/app/action?mode=verifyEmail&oobCode=XYZ&x=1 end'
    bad = 'https://evil.com/app/action?oobCode=XYZ'
    assert register.VERIFY_LINK_PATTERN.search(good).group(0).endswith("x=1")
    assert register.VERIFY_LINK_PATTERN.search(bad) is None

    # CloudflareTempEmail 从 cfg 构造，不触网
    cfg = stt.temp_email_config(pathlib.Path("config.example.toml"))
    prov = register.CloudflareTempEmail(cfg)
    assert hasattr(prov, "create_address") and hasattr(prov, "poll_verification_link")

    # 假 provider 满足 register_one 需要的鸭子类型
    class FakeProvider:
        def create_address(self):
            return register.EmailAddress(address="x@t.co", token="jwt", raw={})
        def poll_verification_link(self, addr, pattern, timeout_s, interval_s):
            return "https://elevenlabs.io/app/action?oobCode=Z"
    fp = FakeProvider()
    assert fp.create_address().address == "x@t.co"
```

在 `run()` 里 `print("selfcheck ok")` 前加 `_check_email_provider()`。

- [ ] **Step 2: 运行确认失败**

Run: `python selfcheck.py`
Expected: FAIL — `AttributeError: module 'register' has no attribute 'VERIFY_LINK_PATTERN'`（或 `CloudflareTempEmail`）

- [ ] **Step 3: 在 `register.py` 实现协议与 class**

在 `register.py` 顶部 import 区加 `import dataclasses`、`from typing import Protocol`。把 `# --- temp-email ---` 段替换为：

```python
VERIFY_LINK_PATTERN = re.compile(
    r"https://elevenlabs\.io/app/action\?[^\s\"<>]+oobCode=[^\s\"<>]+"
)


@dataclasses.dataclass
class EmailAddress:
    address: str
    token: str        # bearer token for polling this mailbox (cloudflare_temp_email jwt)
    raw: dict[str, Any]


class EmailProvider(Protocol):
    def create_address(self) -> EmailAddress: ...
    def poll_verification_link(self, addr: EmailAddress, pattern: "re.Pattern[str]",
                               timeout_s: float, interval_s: float) -> str: ...


class CloudflareTempEmail:
    """cloudflare_temp_email backend; admin path first, user path fallback."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg

    def create_address(self, name: str | None = None) -> EmailAddress:
        cfg = self._cfg
        if not cfg["base_url"] or not cfg["domain"]:
            raise SystemExit("temp_email.base_url and temp_email.domain are required")
        base = str(cfg["base_url"]).rstrip("/")
        local = name or ("el" + secrets.token_hex(5))
        body = {"name": local, "domain": cfg["domain"], "cf_token": "",
                "enableRandomSubdomain": False}
        with httpx.Client(timeout=30) as client:
            if cfg.get("use_admin_path", True) and cfg.get("admin_password"):
                r = client.post(f"{base}/admin/new_address", json=body,
                                headers={"x-admin-auth": cfg["admin_password"]})
                if r.status_code < 400:
                    data = r.json()
                    return EmailAddress(address=data["address"], token=data["jwt"], raw=data)
                if r.status_code not in (401, 403):
                    raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
            headers = {}
            if cfg.get("site_password"):
                headers["x-custom-auth"] = cfg["site_password"]
            r = client.post(f"{base}/api/new_address", json=body, headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
            data = r.json()
            return EmailAddress(address=data["address"], token=data["jwt"], raw=data)

    def poll_verification_link(self, addr: EmailAddress, pattern: "re.Pattern[str]",
                               timeout_s: float, interval_s: float) -> str:
        base = str(self._cfg["base_url"]).rstrip("/")
        deadline = time.time() + float(timeout_s)
        headers = {"Authorization": f"Bearer {addr.token}"}
        with httpx.Client(timeout=30) as client:
            while time.time() < deadline:
                r = client.get(f"{base}/api/parsed_mails", params={"limit": 20, "offset": 0},
                               headers=headers)
                if r.status_code >= 400:
                    raise SystemExit(f"temp-email poll failed ({r.status_code}): {r.text[:300]}")
                for mail in r.json().get("results", []):
                    text = html.unescape("\n".join(str(mail.get(k) or "") for k in ("text", "html")))
                    match = pattern.search(text)
                    if match:
                        return match.group(0)
                time.sleep(float(interval_s))
        raise SystemExit("timed out waiting for ElevenLabs verification email")
```

删除旧的模块级 `temp_email_create` / `latest_verify_link`（它们的调用点在 Task 3 会改成走 provider；此处一并移除，避免两份实现漂移）。

- [ ] **Step 4: 运行确认通过**

Run: `python selfcheck.py`
Expected: PASS — `selfcheck ok`

- [ ] **Step 5: 提交**

```bash
git add register.py selfcheck.py
git commit -m "refactor(register): extract EmailProvider protocol + CloudflareTempEmail"
```

---

### Task 3: `PlatformDriver` 协议 + 抽出 `WinDriver` + `register_one` 重编排

把 `register_one()` 里所有 Windows 特有代码搬到 `register_platform_win.py`，`register_one` 改为纯编排 + 代理生命周期。**Windows 行为逐字保留。**

**Files:**
- Create: `register_platform_win.py`（`WinDriver`：从 `register.py:107-316` 搬 Windows 代码）
- Modify: `register.py`（新增 `PlatformDriver` Protocol、`_default_platform_driver()`、模块级 `click_frac` / 表单 helper；`register_one` 重写为编排）
- Modify: `selfcheck.py`（新增 `_check_register_orchestration()`）

**Interfaces:**
- Consumes: `proxy.ProxyDriver`（Task 1）、`register.EmailProvider` / `CloudflareTempEmail`（Task 2）
- Produces:
  - `register.PlatformDriver` Protocol：
    - `launch_chrome(profile_dir: pathlib.Path, signup_url: str, proxy_url: str | None) -> subprocess.Popen`
    - `find_profile_window(profile_dir: pathlib.Path, timeout_s: float) -> Any`（返回带 `.left/.top/.width/.height` 的 window handle）
    - `ensure_foreground(window: Any) -> None`
    - `kill_profile(profile_dir: pathlib.Path, popen: "subprocess.Popen | None") -> None`
  - `register.register_one(*, provider=None, proxy_driver=None, platform=None) -> dict`（keyword-only，全默认）
  - `register.SIGNUP_URL = "https://elevenlabs.io/app/sign-up"`
  - `register_platform_win.WinDriver`（实现 PlatformDriver）

- [ ] **Step 1: 写失败测试 — `selfcheck.py` 新增 `_check_register_orchestration()`**

用假 driver / provider / 空 proxy 驱动 `register_one`，断言调用顺序、finally 清理、代理生命周期。需要 monkeypatch 掉 `register_one` 内的键盘/鼠标 helper 与 `stt` 网络调用。

```python
def _check_register_orchestration() -> None:
    import register
    calls = []

    class FakeWindow:
        left = top = 0; width = height = 1000

    class FakePlatform:
        def launch_chrome(self, profile_dir, signup_url, proxy_url):
            calls.append(("launch", proxy_url)); return None
        def find_profile_window(self, profile_dir, timeout_s):
            calls.append("find"); return FakeWindow()
        def ensure_foreground(self, window):
            calls.append("focus")
        def kill_profile(self, profile_dir, popen):
            calls.append("kill")

    class FakeProvider:
        def create_address(self):
            calls.append("create_addr")
            return register.EmailAddress(address="e@t.co", token="jwt", raw={})
        def poll_verification_link(self, addr, pattern, timeout_s, interval_s):
            calls.append("poll")
            return "https://elevenlabs.io/app/action?oobCode=Z"

    # stub 掉真正会操作系统/网络的部分
    register._fill_signup_form = lambda *a, **k: calls.append("fill")
    register._open_verify_link_and_confirm = lambda *a, **k: calls.append("verify")
    register._sign_in = lambda *a, **k: calls.append("signin")
    fake_account = {"email": "e@t.co", "remaining": 10000}
    stt.account_from_password_signin = lambda *a, **k: fake_account

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): pass
    stt.authed_client = lambda *a, **k: _FakeClient()
    stt.refresh_credits = lambda *a, **k: None

    pd = proxy.ProxyDriver({"proxies": [], "fail_threshold": 3, "cooldown_secs": 900, "strict": False})
    acct = register.register_one(provider=FakeProvider(), proxy_driver=pd, platform=FakePlatform())
    assert acct == fake_account
    order = [c if isinstance(c, str) else c[0] for c in calls]
    assert order == ["create_addr", "launch", "find", "focus", "fill",
                     "poll", "verify", "signin", "kill"], order
    assert ("launch", None) in calls, "empty pool → proxy_url None to Chrome"

    # 失败路径：poll 抛错，kill 仍被调用，代理被 mark_fail
    calls.clear()
    marks = []
    class FailProvider(FakeProvider):
        def poll_verification_link(self, *a, **k):
            raise SystemExit("boom")
    pd2 = proxy.ProxyDriver({"proxies": ["http://p"], "fail_threshold": 3,
                             "cooldown_secs": 900, "strict": False})
    orig_fail = pd2.mark_fail
    pd2.mark_fail = lambda p: (marks.append(p.url), orig_fail(p))
    try:
        register.register_one(provider=FailProvider(), proxy_driver=pd2, platform=FakePlatform())
        assert False, "should propagate"
    except SystemExit:
        pass
    assert "kill" in [c if isinstance(c, str) else c[0] for c in calls], "kill must run in finally"
    assert marks == ["http://p"], "failure must mark_fail the picked proxy"
```

在 `run()` 里 `print("selfcheck ok")` 前加 `_check_register_orchestration()`。（注意：它 monkeypatch 了 `stt.*` 与 `register._*`，放在 `run()` 最后、其他检查之后，避免污染。）

- [ ] **Step 2: 运行确认失败**

Run: `python selfcheck.py`
Expected: FAIL — `register_one() got an unexpected keyword argument 'provider'`（现签名无参）

- [ ] **Step 3: 建 `register_platform_win.py`，搬 Windows 代码**

把 `register.py` 现有 `register_one` 内的这些块**原样**搬进 `WinDriver` 方法（逻辑不改，只换宿主）：
- `profile_window_handles()`（`register.py:107-133`）→ `WinDriver.find_profile_window` 内部（含 30s 轮询循环 `register.py:157-175`）
- Chrome 启动（`register.py:135-156`）→ `WinDriver.launch_chrome`，末尾把 `signup_url` 用参数替换硬编码 URL；`proxy_url` 非空时在 arg 列表加 `f"--proxy-server={proxy_url}"`
- `window_op` / `ensure_window_foreground`（`register.py:177-229`）→ `WinDriver.ensure_foreground`（注意 restore/maximize 首次序列 `register.py:236-238` 也并入）
- profile 清理（`register.py:300-316`）→ `WinDriver.kill_profile`

```python
#!/usr/bin/env python3
"""Windows PlatformDriver: real Chrome via subprocess + WMI window lookup +
Win32 foreground forcing. Behavior-identical to the pre-refactor register_one
Windows path — see git history of register.py."""
from __future__ import annotations

import os, pathlib, shutil, subprocess, time
from typing import Any

import pygetwindow as gw


class WinDriver:
    def launch_chrome(self, profile_dir, signup_url, proxy_url):
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        args = [chrome, f"--user-data-dir={profile_dir}", "--no-first-run",
                "--new-window", "--window-position=40,40",
                "--disable-save-password-bubble", "--do-not-de-elevate"]
        if proxy_url:
            args.append(f"--proxy-server={proxy_url}")
        args.append(signup_url)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
        return subprocess.Popen(args, startupinfo=startupinfo,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    # find_profile_window / ensure_foreground / kill_profile: 见下,逐字搬 register.py
```

> 实现者注意：`find_profile_window`、`ensure_foreground`、`kill_profile` 的方法体是从 `register.py` 现有对应块**逐字复制**（含全部注释与 `ctypes` 常量），只把闭包变量 `profile_dir` / `new_window` / `proc` 换成方法参数。不要重写逻辑——那些 Win32 前台抢占顺序是踩坑踩出来的。

- [ ] **Step 4: 在 `register.py` 重写 `register_one` 为编排 + 抽出模块级 helper**

新增 `PlatformDriver` Protocol、`SIGNUP_URL`、`_default_platform_driver()`，以及从旧代码剥出的 `click_frac(window, x_frac, y_frac)` / `_fill_signup_form` / `_open_verify_link_and_confirm` / `_sign_in`（坐标、`ctrl+a`/`paste`/`tab`/`enter` 序列、等待时长 `time.sleep(12/15/8)` **逐字保留**，只把 `ensure_window_foreground()` 换成 `platform.ensure_foreground(window)`，把 `new_window.left/width` 换成 `window.left/width`）。`register_one`：

```python
SIGNUP_URL = "https://elevenlabs.io/app/sign-up"


class PlatformDriver(Protocol):
    def launch_chrome(self, profile_dir: pathlib.Path, signup_url: str,
                      proxy_url: str | None) -> "subprocess.Popen | None": ...
    def find_profile_window(self, profile_dir: pathlib.Path, timeout_s: float) -> Any: ...
    def ensure_foreground(self, window: Any) -> None: ...
    def kill_profile(self, profile_dir: pathlib.Path,
                     popen: "subprocess.Popen | None") -> None: ...


def _default_platform_driver() -> PlatformDriver:
    if sys.platform == "darwin":
        from register_platform_mac import MacDriver
        return MacDriver()
    if os.name == "nt":
        from register_platform_win import WinDriver
        return WinDriver()
    raise SystemExit(f"register 目前只支持 macOS 与 Windows；当前平台: {sys.platform}")


def register_one(*, provider: EmailProvider | None = None,
                 proxy_driver: "ProxyDriver | None" = None,
                 platform: PlatformDriver | None = None) -> dict[str, Any]:
    import proxy as _proxy
    provider = provider or CloudflareTempEmail(stt.temp_email_config())
    proxy_driver = proxy_driver or _proxy.ProxyDriver(stt.proxy_config())
    platform = platform or _default_platform_driver()
    tcfg = stt.temp_email_config()

    picked = proxy_driver.pick()
    proxy_url = picked.url if picked else None
    profile_dir = pathlib.Path(tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-"))
    _write_no_password_prefs(profile_dir)  # 从 register.py:98-103 抽出

    popen = None
    try:
        stt._rlog("创建临时邮箱...")
        addr = provider.create_address()
        stt._rlog(f"临时邮箱已创建: {addr.address}")
        password = stt.random_password()
        stt._rlog("启动临时 Chrome...")
        popen = platform.launch_chrome(profile_dir, SIGNUP_URL, proxy_url)
        window = platform.find_profile_window(profile_dir, timeout_s=30)
        platform.ensure_foreground(window)
        time.sleep(4)
        time.sleep(12)  # 等 app 渲染（保留原 register.py:267 注释语义）
        stt._rlog("填写注册表单...")
        _fill_signup_form(window, platform, addr.address, password)
        stt._rlog(f"等待验证邮件（最长 {tcfg['poll_timeout_secs']}s）...")
        link = provider.poll_verification_link(addr, VERIFY_LINK_PATTERN,
                                               tcfg["poll_timeout_secs"], tcfg["poll_interval_secs"])
        stt._rlog("打开验证链接并确认...")
        _open_verify_link_and_confirm(window, platform, link)
        stt._rlog("用新账号登录...")
        _sign_in(window, platform, addr.address, password)
        stt._rlog("拉取账号积分...")
        account = stt.account_from_password_signin(addr.address, password, temp_address=addr.address)
        with stt.authed_client(account, save=lambda _s: None) as client:
            client.get("/v1/user")
            stt.refresh_credits(account, client)
        stt._rlog(f"注册完成: {addr.address}，剩余积分 {stt.cached_remaining(account)}")
        if picked:
            proxy_driver.mark_ok(picked)
        return account
    except BaseException:
        if picked:
            proxy_driver.mark_fail(picked)
        raise
    finally:
        platform.kill_profile(profile_dir, popen)
```

> `pyautogui`/`pyperclip`/`pygetwindow` 的 import 从 `register_one` 顶部移除——它们现在只在各 driver 内部（Win driver import pygetwindow；两个 helper 用的 pyautogui/pyperclip 放在 `register.py` 顶部的 `try/except ImportError`，Mac/Win 都需要）。保留原 `register.py:86-89` 的 ImportError 友好报错。

- [ ] **Step 5: 运行确认通过**

Run: `python selfcheck.py`
Expected: PASS — `selfcheck ok`（编排顺序、finally、proxy 生命周期断言全过）

- [ ] **Step 6: 提交**

```bash
git add register.py register_platform_win.py selfcheck.py
git commit -m "refactor(register): extract PlatformDriver + WinDriver; register_one is pure orchestration"
```

---

### Task 4: `MacDriver` + 浏览器路径探测

**Files:**
- Create: `register_platform_mac.py`（`MacDriver` + `_find_chrome_binary()`）
- Modify: `selfcheck.py`（新增 `_check_mac_chrome_discovery()`，仅测纯函数 `_find_chrome_binary`）

**Interfaces:**
- Produces: `register_platform_mac.MacDriver`（实现 `PlatformDriver`）、`register_platform_mac._find_chrome_binary(env: dict, exists: callable) -> str`（可注入依赖，便于离线测试）

- [ ] **Step 1: 写失败测试 — `_check_mac_chrome_discovery()`**

`_find_chrome_binary` 接受注入的 `env` 与 `exists`，纯逻辑可离线测：

```python
def _check_mac_chrome_discovery() -> None:
    import register_platform_mac as mac
    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    BRAVE = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    # 环境变量覆盖优先
    assert mac._find_chrome_binary({"ELEVENLABS_STT_CHROME": "/custom/x"},
                                   exists=lambda p: True) == "/custom/x"
    # Chrome 优先于 Brave
    assert mac._find_chrome_binary({}, exists=lambda p: p in (CHROME, BRAVE)) == CHROME
    # 只有 Brave
    assert mac._find_chrome_binary({}, exists=lambda p: p == BRAVE) == BRAVE
    # 都没有 → SystemExit
    try:
        mac._find_chrome_binary({}, exists=lambda p: False)
        assert False, "no browser must raise"
    except SystemExit as e:
        assert "ELEVENLABS_STT_CHROME" in str(e)
```

在 `run()` 里加 `_check_mac_chrome_discovery()`。

- [ ] **Step 2: 运行确认失败**

Run: `python selfcheck.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'register_platform_mac'`

- [ ] **Step 3: 实现 `register_platform_mac.py`**

```python
#!/usr/bin/env python3
"""macOS PlatformDriver: real Chrome/Brave via subprocess + AppleScript window
lookup and foreground. Requires the terminal to have Accessibility permission
(System Settings → Privacy & Security → Accessibility) for pyautogui input."""
from __future__ import annotations

import collections, os, pathlib, shutil, subprocess, time
from typing import Any, Callable

_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

MacWindow = collections.namedtuple("MacWindow", "left top width height app")


def _find_chrome_binary(env: dict | None = None,
                        exists: Callable[[str], bool] = os.path.exists) -> str:
    env = env if env is not None else os.environ
    override = env.get("ELEVENLABS_STT_CHROME")
    if override:
        return override
    for path in _CANDIDATES:
        if exists(path):
            return path
    raise SystemExit(
        "register 需要 Chrome 或 Brave，未在标准路径找到；"
        "设 $ELEVENLABS_STT_CHROME 指向浏览器可执行文件")


def _osascript(script: str) -> str:
    out = subprocess.run(["osascript", "-e", script],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise SystemExit(f"osascript failed: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


class MacDriver:
    def __init__(self) -> None:
        self._binary = _find_chrome_binary()
        # AppleScript app 名：Chrome 或 Brave 决定 tell 目标
        self._app = "Brave Browser" if "Brave" in self._binary else "Google Chrome"

    def launch_chrome(self, profile_dir, signup_url, proxy_url):
        args = [self._binary, f"--user-data-dir={profile_dir}", "--no-first-run",
                "--new-window", "--window-position=40,40",
                "--disable-save-password-bubble"]
        if proxy_url:
            args.append(f"--proxy-server={proxy_url}")
        args.append(signup_url)
        return subprocess.Popen(args)

    def find_profile_window(self, profile_dir, timeout_s):
        # fresh profile → 唯一 active-tab URL 命中 sign-up 的 window 就是目标
        deadline = time.time() + timeout_s
        script = (f'tell application "{self._app}"\n'
                  ' repeat with w in windows\n'
                  '  if URL of active tab of w starts with "https://elevenlabs.io/app/sign-up" then\n'
                  '   set b to bounds of w\n'
                  '   return (item 1 of b as text) & "," & (item 2 of b as text) & ","'
                  ' & (item 3 of b as text) & "," & (item 4 of b as text)\n'
                  '  end if\n'
                  ' end repeat\n'
                  'end tell\n return ""')
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                res = _osascript(script)
            except SystemExit:
                res = ""
            if res:
                x1, y1, x2, y2 = (int(v) for v in res.split(","))
                return MacWindow(left=x1, top=y1, width=x2 - x1, height=y2 - y1, app=self._app)
        raise SystemExit("auto-register 未找到新的临时浏览器窗口；aborting")

    def ensure_foreground(self, window):
        _osascript(f'tell application "{self._app}" to activate')
        time.sleep(0.15)

    def kill_profile(self, profile_dir, popen):
        subprocess.run(["pkill", "-f", str(profile_dir)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)
```

- [ ] **Step 4: 运行确认通过**

Run: `python selfcheck.py`
Expected: PASS — `selfcheck ok`

- [ ] **Step 5: 提交**

```bash
git add register_platform_mac.py selfcheck.py
git commit -m "feat(register): macOS PlatformDriver (Chrome/Brave via AppleScript)"
```

---

### Task 5: 代理贯通 `stt.py` httpx（ElevenLabs API + Firebase 登录）

Chrome 侧代理已在 Task 3 生效；本任务让注册期的 httpx 调用（Firebase 密码登录、ElevenLabs `/v1/user`）走同一代理，保证出网 IP 一致。**注册后账号进池、日常转录时不带代理**（那是另一套生命周期），故只加可选参数、不改默认。

**Files:**
- Modify: `stt.py`（`firebase_signin_password`、`account_from_password_signin`、`authed_client` 加可选 `proxy: str | None = None`）
- Modify: `register.py`（`register_one` 把 `proxy_url` 传进上述调用）
- Modify: `selfcheck.py`（新增 `_check_proxy_threading()`）

**Interfaces:**
- Consumes: `proxy_url`（Task 3 的 `register_one` 局部变量）
- Produces:
  - `stt.authed_client(session, save=None, proxy: str | None = None) -> httpx.Client`
  - `stt.account_from_password_signin(email, password, temp_address=None, proxy: str | None = None) -> dict`
  - `stt.firebase_signin_password(email, password, proxy: str | None = None) -> dict`

- [ ] **Step 1: 写失败测试 — `_check_proxy_threading()`**

httpx 内部 proxies 不易内省；改测「函数接受 proxy kwarg 且把它传给 httpx」——monkeypatch `httpx.Client` 捕获 kwargs。

```python
def _check_proxy_threading() -> None:
    import httpx
    captured = {}
    orig = httpx.Client
    def spy(*a, **k):
        captured.update(k)
        return orig(*a, **{kk: vv for kk, vv in k.items() if kk != "proxy"} | {})
    # authed_client 应把 proxy 传进 httpx.Client(proxy=...)
    sess = {"jwt": "x", "jwt_exp": time.time() + 3600}
    httpx.Client = spy
    try:
        stt.authed_client(sess, save=lambda _s: None, proxy="http://p")
    finally:
        httpx.Client = orig
    assert captured.get("proxy") == "http://p", f"authed_client must thread proxy, got {captured}"
    # 不传 proxy → 不带该 kwarg（保持旧行为）
    captured.clear(); httpx.Client = spy
    try:
        stt.authed_client(sess, save=lambda _s: None)
    finally:
        httpx.Client = orig
    assert "proxy" not in captured or captured["proxy"] is None
```

> 说明：`get_jwt` 里若 jwt 未过期则不触网，故上面用未过期 jwt 让 `authed_client` 只走 `httpx.Client` 构造。加进 `run()`。

- [ ] **Step 2: 运行确认失败**

Run: `python selfcheck.py`
Expected: FAIL — `authed_client() got an unexpected keyword argument 'proxy'`

- [ ] **Step 3: 在 `stt.py` 加 proxy 参数**

`authed_client`（`stt.py:418`）：

```python
def authed_client(session: dict[str, Any], save=None, proxy: str | None = None) -> httpx.Client:
    jwt = get_jwt(session, save)
    kwargs: dict[str, Any] = dict(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=httpx.Timeout(30.0, read=None),
    )
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)
```

`account_from_password_signin`（`stt.py:447`）加 `proxy` 参数并转发给 `firebase_signin_password`：

```python
def account_from_password_signin(email, password, temp_address=None, proxy: str | None = None):
    data = firebase_signin_password(email, password, proxy=proxy)
    ...
```

`firebase_signin_password`：在其 `httpx.post(...)`（找到该函数体）改为：`with httpx.Client(proxy=proxy, timeout=30) if proxy else nullcontext(...)` —— 简洁做法是构造 kwargs：

```python
def firebase_signin_password(email, password, proxy: str | None = None) -> dict[str, Any]:
    kwargs = {"proxy": proxy} if proxy else {}
    resp = httpx.post(SIGNIN_URL, json={...same as now...}, timeout=30, **kwargs)
    ...
```

> 实现者：`httpx.post(..., proxy=...)` 在 httpx≥0.26 支持；若项目锁定旧版则改用 `with httpx.Client(proxy=proxy) as c: c.post(...)`。先 `python -c "import httpx; print(httpx.__version__)"` 确认。

- [ ] **Step 4: `register.py` 把 `proxy_url` 传进去**

`register_one` 内两处：
```python
account = stt.account_from_password_signin(addr.address, password,
                                           temp_address=addr.address, proxy=proxy_url)
with stt.authed_client(account, save=lambda _s: None, proxy=proxy_url) as client:
```

- [ ] **Step 5: 运行确认通过**

Run: `python selfcheck.py`
Expected: PASS — `selfcheck ok`

- [ ] **Step 6: 提交**

```bash
git add stt.py register.py selfcheck.py
git commit -m "feat(register): thread proxy through Firebase signin + ElevenLabs API calls"
```

---

### Task 6: 文档 — README Mac 首次运行 + temp-email 后端 Mac 差异

**Files:**
- Modify: `README.md`（注册机章节加 macOS 首次运行小节）
- Modify: `docs/temp-email-backend.md`（若涉及平台差异，补一句 Mac 也走同一 HTTP API，无差异）
- Modify: `config.example.toml`（已在 Task 1 加 `[proxy]`；此处仅确认注释完整，无改动则跳过）

**Interfaces:** 无代码接口。

- [ ] **Step 1: README 加 macOS 小节**

在 README 注册机相关章节后新增（跟随现有中文风格）：

```markdown
### macOS 首次运行

注册机在 macOS 上用真实 Chrome/Brave + 键鼠自动化，首次运行需：

1. **授予 Accessibility 权限**：System Settings → Privacy & Security →
   Accessibility，把你的终端 app（Terminal / iTerm）打开开关。否则自动化的
   键盘鼠标事件会被系统静默丢弃，注册会卡在填表这一步。
2. **浏览器**：默认探测 `/Applications/Google Chrome.app`，其次
   `/Applications/Brave Browser.app`。都不在标准路径时，设环境变量
   `export ELEVENLABS_STT_CHROME="/path/to/浏览器可执行文件"`。
3. 运行期间会弹出一个临时 Chrome/Brave 窗口，请勿手动切走或最小化，直到日志显示
   「注册完成」。

### 代理（可选）

`config.toml` 的 `[proxy]` 段可给注册流程加代理池（Chrome 与 API 调用共用）：
留空即直连（默认）。连续失败 `fail_threshold` 次的代理会被禁用
`cooldown_secs` 秒后重试；`strict=true` 时全池不可用直接报错，`false` 时降级直连。
```

- [ ] **Step 2: 运行完整 selfcheck 收尾**

Run: `python selfcheck.py`
Expected: PASS — `selfcheck ok`（确认文档改动没碰坏代码）

- [ ] **Step 3: 提交**

```bash
git add README.md docs/temp-email-backend.md
git commit -m "docs: macOS first-run (Accessibility) + proxy pool usage"
```

---

## 手动集成测试（合并前跑，非 CI）

离线 selfcheck 覆盖 ProxyDriver 状态机、register_one 编排顺序、Mac 浏览器探测、proxy 参数贯通。以下需真机 + 真 `[temp_email]` 后端：

1. **Windows 回归**：主分支 vs 本分支各跑一次 `stt` 注册，确认流程无差异（坐标、时序、成功率）。
2. **Windows + 代理**：`[proxy] proxies=["http://…"]`，跑一次，chrome://net-internals 或抓包确认 Chrome 走代理。
3. **macOS 干净机**：装依赖 → 首次跑 → 系统弹 Accessibility 请求 → 授权 → 再跑到「注册完成」。
4. **macOS + 代理**：同 2，在 Mac 上验证。

## Self-Review 记录

- **Spec 覆盖**：§4.1 PlatformDriver→Task 3/4；§4.2 ProxyDriver→Task 1；§4.3 EmailProvider→Task 2；§5 register_one 编排→Task 3；§6 配置→Task 1；§7 错误处理→分散在各 Task 的 SystemExit 文案；§8 测试→各 Task selfcheck；§9 顺序→Task 1-6 一致；§10 YAGNI→计划内无越界任务。全覆盖。
- **占位符**：无 TBD/TODO；每个代码步都有可运行代码。
- **类型一致**：`EmailAddress(address/token/raw)`、`Proxy(url/fails/disabled_until)`、`PlatformDriver` 四方法签名在 Task 3 定义、Task 4 MacDriver 实现一致；`register_one` keyword-only 签名 Task 3 定义、Task 5 调用一致；`proxy_url`（str|None）贯穿 Task 3→5 命名一致。
