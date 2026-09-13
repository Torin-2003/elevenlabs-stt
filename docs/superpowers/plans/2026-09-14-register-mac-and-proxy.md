# 注册机 可插拔多策略 + Mac 适配 + 代理池 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 superpowers:executing-plans，task-by-task 执行。步骤用 `- [ ]` 勾选跟踪。

**Goal:** 把注册机重构成可插拔多策略架构（`RegisterStrategy`），本轮实现真 Chrome 的 `UICoordinateStrategy`（Windows 零回退 + 新增 macOS），加最小代理池（住宅 IP，Chrome + httpx 同代理，失败禁用轮换），HTTP/CDP 策略与 `CaptchaSolver` 做成占位扩展点。

**Architecture:** 见 `docs/superpowers/specs/2026-09-14-register-mac-and-proxy-design.md`（已同步更新）。两层：顶层 `RegisterStrategy`（ui/http/cdp，按 `[register].strategy` 分发），下层共享服务 `EmailProvider` / `ProxyDriver` / `CaptchaSolver`。`PlatformDriver`（Win/Mac 窗口管理）是 `UICoordinateStrategy` 内部实现。

**Tech Stack:** Python 3 标准库 + httpx（0.28.1，用 `proxy=` 单数）+ 现有 pyautogui/pyperclip/pygetwindow；macOS 用自带 `osascript`/`pkill`。**本轮不新增第三方依赖**（HTTP/CDP 策略是占位，其依赖 curl_cffi/nodriver 等下轮才引入）。

## Global Constraints

- **测试载体**：无 pytest、无 `tests/`。测试是 `selfcheck.py::run()` 里的 `assert`，`python selfcheck.py` 成功打印 `selfcheck ok`。新测试加 `_check_*()` 并在 `run()` 末尾 `print("selfcheck ok")` 前调用。
- **Windows 行为零回退**：Task 4 是重构，坐标/等待时长（12s/15s/8s）/admin-user 双路径/profile 清理逐字保留。
- **向后兼容签名**：`register.register_one()` 现有调用点 `web.py:318`、`stt.py:960`（`register_one as register_fn`）必须裸调仍工作；新参数全 keyword-only 带默认。
- **配置风格**：`register_config()` / `proxy_config()` 对齐 `stt.temp_email_config()`（`stt.py:137`）dict-with-defaults 写法。
- **License**：不复制 any-auto-register（AGPL）任何代码，只借多策略架构思路。
- **住宅代理**：`[proxy]` 注释与 README 写明必须住宅/轮换 IP。
- **扩展点契约**：HTTP/CDP 占位类被选中即 `NotImplementedError` + 明确指引；下轮加策略只新增 class，不动 dispatcher/UI 策略/共享服务。

---

### Task 1: `[proxy]` 配置 + `ProxyDriver`

**Files:** Create `proxy.py`；Modify `stt.py`（`proxy_config()`）、`selfcheck.py`（`_check_proxy_driver`）、`config.example.toml`（`[proxy]`）。

**Interfaces (Produces):** `stt.proxy_config(path)->dict`；`proxy.Proxy(url,fails=0,disabled_until=0.0)`；`proxy.ProxyDriver(cfg)` with `pick()->Proxy|None` / `mark_ok(p)` / `mark_fail(p)`。`pick()`：未禁用集合 round-robin；池空→`None`；全禁用 strict=False→`None`、strict=True→`SystemExit("proxy pool exhausted; strict mode")`。

- [ ] **Step 1 (RED):** `selfcheck.py` import 区加 `import proxy`，新增 `_check_proxy_driver()`（见 spec §8 覆盖点：空池→None、round-robin 覆盖两个、失败到阈值禁用、mark_ok 清零、cooldown 到期复活手动改 `disabled_until`、strict 全禁用抛错）。`run()` 末尾调用。
- [ ] **Step 2 (RED):** `python selfcheck.py` → FAIL `No module named 'proxy'`。
- [ ] **Step 3 (GREEN):** 实现 `proxy.py`（`Proxy` dataclass + `ProxyDriver`，`_live()` 按 `disabled_until<=now` 过滤，游标 round-robin）。代码见 spec §4.2。
- [ ] **Step 4:** `stt.py` 加 `proxy_config()`（紧邻 `temp_email_config` ~`stt.py:137`），默认 `{"proxies":[],"fail_threshold":3,"cooldown_secs":900,"strict":False}`。
- [ ] **Step 5:** `config.example.toml` 在 `[accounts]` 后加 `[proxy]` 段（含"必须住宅 IP"注释，见 spec §6）。
- [ ] **Step 6 (GREEN):** `python selfcheck.py` → `selfcheck ok`。
- [ ] **Step 7:** `git add proxy.py stt.py selfcheck.py config.example.toml && git commit -m "feat(register): minimal proxy pool (static list + rotate + auto-disable)"`

---

### Task 2: `EmailProvider` 协议 + `CloudflareTempEmail` + 域名轮换

把 `register.py:31-79` 的 `temp_email_create`/`latest_verify_link` 收进 class，加 `domains[]` 轮换（分散版，见 spec §4.3 限定）。

**Files:** Modify `register.py`（`EmailAddress`、`EmailProvider` Protocol、`CloudflareTempEmail`、`VERIFY_LINK_PATTERN`；删旧模块级函数）、`selfcheck.py`（`_check_email_provider`）。

**Interfaces:** `register.EmailAddress(address,token,raw)`；`register.EmailProvider` Protocol（`create_address()->EmailAddress`、`poll_verification_link(addr,pattern,timeout_s,interval_s)->str`）；`register.CloudflareTempEmail(cfg)`；`register.VERIFY_LINK_PATTERN`。

- [ ] **Step 1 (RED):** `_check_email_provider()`（见 spec §8）：`VERIFY_LINK_PATTERN` 命中 elevenlabs / 不命中 evil.com；`CloudflareTempEmail(cfg)` 从 `config.example.toml` 构造不触网；**域名轮换**——monkeypatch `httpx.Client.post` 捕获 body，多域名下连续 `create_address()` 的 `domain` 字段游标推进、单域名下不变。假 provider 鸭子类型满足协议。
- [ ] **Step 2 (RED):** `python selfcheck.py` → FAIL（无 `VERIFY_LINK_PATTERN`/`CloudflareTempEmail`）。
- [ ] **Step 3 (GREEN):** `register.py` 顶部加 `import dataclasses`、`from typing import Protocol`。实现 `EmailAddress`/`EmailProvider`/`CloudflareTempEmail`（代码见 spec §4.3 详版；构造函数存 `self._domains = cfg.get("domains") or [cfg["domain"]]` + `self._cursor=0`，`create_address` 取 `self._domains[self._cursor % len]` 并 `self._cursor+=1`）。admin/user 双路径 fallback、轮询正则逐字保留。删旧模块级 `temp_email_create`/`latest_verify_link`。
- [ ] **Step 4 (GREEN):** `python selfcheck.py` → `selfcheck ok`。
- [ ] **Step 5:** `git add register.py selfcheck.py && git commit -m "refactor(register): EmailProvider protocol + CloudflareTempEmail with domain rotation"`

---

### Task 3: `RegisterStrategy` 分发层 + `CaptchaSolver` 协议 + HTTP/CDP 占位 + `[register]` 配置

`register_one` 变 dispatcher；定义策略协议 + 占位类 + captcha 协议。此时 `UICoordinateStrategy` 尚未实现（Task 4），故本任务测试用**假策略**验证分发逻辑。

**Files:** Modify `register.py`（`RegisterStrategy`/`CaptchaSolver` Protocol、`_STRATEGIES`、`HTTPProtocolStrategy`/`StealthCDPStrategy` 占位、`register_one` dispatcher）、`stt.py`（`register_config()`）、`selfcheck.py`（`_check_register_dispatch`）、`config.example.toml`（`[register]`+`[captcha]`）。

**Interfaces:** `register.RegisterStrategy` Protocol（`register(*,provider,proxy_driver,captcha=None)->dict`）；`register.CaptchaSolver` Protocol（`solve_hcaptcha(sitekey,page_url)->str`）；`register.register_one(*,strategy=None,provider=None,proxy_driver=None,captcha=None)->dict`；`stt.register_config(path)->dict`（默认 `{"strategy":"ui"}`）；`HTTPProtocolStrategy`/`StealthCDPStrategy`（`register` 抛 `NotImplementedError`）。

> **依赖注意**：`_STRATEGIES["ui"]` 指向 `UICoordinateStrategy`，Task 4 才实现。本任务可先让 `_STRATEGIES = {"ui": _lazy_ui, "http": HTTPProtocolStrategy, "cdp": StealthCDPStrategy}`，其中 `_lazy_ui` 是一个占位——**或**更简洁：本任务只建 dispatcher 与占位 http/cdp + 协议，`"ui"` 键的 factory 引用一个本任务内的最小 `UICoordinateStrategy` 空壳（`register` 抛 `NotImplementedError("Task 4")`），Task 4 再填实现。测试用 `register_one(strategy=FakeStrategy())` 显式注入，绕过 `_STRATEGIES`，故不依赖 ui 实现。

- [ ] **Step 1 (RED):** `_check_register_dispatch()`：
  - `register_one(strategy=FakeStrategy(), provider=FakeProvider(), proxy_driver=empty_pool)` 返回 `FakeStrategy` 的结果，且 `FakeStrategy.register` 收到注入的 provider/proxy_driver。
  - monkeypatch `stt.register_config` 返回 `{"strategy":"http"}` → `register_one()`（不显式传 strategy）→ `NotImplementedError`（含 "HTTP" / "strategy='ui'" 指引）。
  - `{"strategy":"cdp"}` → `NotImplementedError`。
  - `{"strategy":"zzz"}` → `SystemExit`（含 "未知" 与可选项）。
- [ ] **Step 2 (RED):** `python selfcheck.py` → FAIL（`register_one` 无 `strategy` kw / 无 `register_config`）。
- [ ] **Step 3 (GREEN):** `register.py` 加 `RegisterStrategy`/`CaptchaSolver` Protocol、`HTTPProtocolStrategy`/`StealthCDPStrategy` 占位（代码见 spec §4.0）、`UICoordinateStrategy` 空壳（`register` 抛 `NotImplementedError("Task 4")`，Task 4 替换）、`_STRATEGIES` dict、`register_one` dispatcher（选策略→注入 `provider`/`proxy_driver`→`strategy.register(...)`）。`stt.py` 加 `register_config()`。
- [ ] **Step 4:** `config.example.toml` 加 `[register]`（`strategy="ui"`）与 `[captcha]`（`provider=""`,`api_key=""`），见 spec §6。
- [ ] **Step 5 (GREEN):** `python selfcheck.py` → `selfcheck ok`。
- [ ] **Step 6:** `git add register.py stt.py selfcheck.py config.example.toml && git commit -m "feat(register): pluggable RegisterStrategy dispatch + CaptchaSolver protocol + http/cdp stubs"`

---

### Task 4: `PlatformDriver` + `WinDriver` 抽取 + `UICoordinateStrategy` 承接编排

把旧 `register_one` 的 Windows 编排整体搬进 `UICoordinateStrategy.register`，OS 窗口管理抽到 `WinDriver`。**Windows 行为逐字保留。**

**Files:** Create `register_platform_win.py`（`WinDriver`）；Modify `register.py`（`PlatformDriver` Protocol、`SIGNUP_URL`、`_default_platform_driver()`、模块级 `click_frac`/`_write_no_password_prefs`/`_fill_signup_form`/`_open_verify_link_and_confirm`/`_sign_in`、`UICoordinateStrategy.register` 实现替换 Task 3 空壳）、`selfcheck.py`（`_check_register_orchestration`）。

**Interfaces:** `register.PlatformDriver` Protocol（`launch_chrome(profile_dir,signup_url,proxy_url)->Popen|None`、`find_profile_window(profile_dir,timeout_s)->window(.left/.top/.width/.height)`、`ensure_foreground(window)`、`kill_profile(profile_dir,popen)`）；`register.SIGNUP_URL`；`register_platform_win.WinDriver`。

- [ ] **Step 1 (RED):** `_check_register_orchestration()`（见 spec §8）：假 `PlatformDriver`（记录调用序）+ 假 `EmailProvider` + 空 `ProxyDriver`，stub `register._fill_signup_form/_open_verify_link_and_confirm/_sign_in` 与 `stt.account_from_password_signin/authed_client/refresh_credits`，跑 `UICoordinateStrategy(platform=FakePlatform()).register(provider=..., proxy_driver=...)`，断言序 `create_addr→launch→find→focus→fill→poll→verify→signin→kill`；空池→`launch` 收到 `proxy_url=None`；失败路径（poll 抛错）→`kill` 仍调用 + 有代理时 `mark_fail` 被调。
- [ ] **Step 2 (RED):** `python selfcheck.py` → FAIL（`UICoordinateStrategy.register` 还是 Task 3 的 `NotImplementedError`）。
- [ ] **Step 3 (GREEN):** 建 `register_platform_win.py`，把 `register.py` 现有块**逐字**搬进 `WinDriver`：`launch_chrome`（`register.py:135-156` + `proxy_url` 时加 `--proxy-server=`，URL 用参数）、`find_profile_window`（`register.py:107-133` WMI + 30s 轮询 `157-175`）、`ensure_foreground`（`register.py:177-238` 含首次 restore/maximize）、`kill_profile`（`register.py:300-316`）。Win32 前台抢占顺序不重写。
- [ ] **Step 4 (GREEN):** `register.py` 加 `PlatformDriver` Protocol、`SIGNUP_URL`、`_default_platform_driver()`（`darwin`→MacDriver[Task5]、`nt`→WinDriver、else→SystemExit）、模块级 helper（从 `register.py:249-291` 剥出，坐标/时长逐字保留，`ensure_window_foreground()`→`platform.ensure_foreground(window)`、`new_window.*`→`window.*`）。用 Task 5 实现前 `_default_platform_driver` 的 darwin 分支延迟 import 即可（Task 4 在 Windows 测不触发）。`UICoordinateStrategy.register` 替换为完整编排（代码见 spec §5）。移除 `register_one` 顶部的 pyautogui/pygetwindow import（下沉到 driver / 模块顶部 try-except）。
- [ ] **Step 5 (GREEN):** `python selfcheck.py` → `selfcheck ok`。
- [ ] **Step 6:** `git add register.py register_platform_win.py selfcheck.py && git commit -m "refactor(register): PlatformDriver + WinDriver; UICoordinateStrategy holds orchestration"`

---

### Task 5: `MacDriver` + 浏览器路径探测

**Files:** Create `register_platform_mac.py`（`MacDriver` + `_find_chrome_binary`）；Modify `selfcheck.py`（`_check_mac_chrome_discovery`）。

**Interfaces:** `register_platform_mac.MacDriver`（实现 `PlatformDriver`）；`register_platform_mac._find_chrome_binary(env=None, exists=os.path.exists)->str`（可注入，离线可测）。

- [ ] **Step 1 (RED):** `_check_mac_chrome_discovery()`（见 spec §8 / 原计划）：env 覆盖优先、Chrome 优先 Brave、只 Brave、都无→`SystemExit`（含 `ELEVENLABS_STT_CHROME`）。
- [ ] **Step 2 (RED):** `python selfcheck.py` → FAIL（`No module named 'register_platform_mac'`）。
- [ ] **Step 3 (GREEN):** 实现 `register_platform_mac.py`（代码见 spec §4.1.2 修正版）：`_find_chrome_binary`（Chrome→Brave→env 覆盖）；`MacDriver.launch_chrome`（`subprocess.Popen` + `--proxy-server` 可选）；`find_profile_window`（AppleScript 遍历 window，active tab URL 命中 `https://elevenlabs.io/app/sign-up` → 返回 `bounds` 转 `MacWindow(left,top,width,height,app)`，轮询到 timeout）；`ensure_foreground`（`osascript activate`）；`kill_profile`（`pkill -f profile_dir` + rmtree 重试）。
- [ ] **Step 4 (GREEN):** `python selfcheck.py` → `selfcheck ok`。
- [ ] **Step 5:** `git add register_platform_mac.py selfcheck.py && git commit -m "feat(register): macOS PlatformDriver (Chrome/Brave via AppleScript)"`

---

### Task 6: 代理贯通 `stt.py` httpx（Firebase 登录 + ElevenLabs API）

**Files:** Modify `stt.py`（`firebase_signin_password`/`account_from_password_signin`/`authed_client` 加 `proxy: str|None=None`）、`register.py`（编排里传 `proxy_url`——Task 4 的 spec §5 代码已含，此处确认）、`selfcheck.py`（`_check_proxy_threading`）。

**Interfaces:** `stt.authed_client(session,save=None,proxy=None)`；`stt.account_from_password_signin(email,password,temp_address=None,proxy=None)`；`stt.firebase_signin_password(email,password,proxy=None)`。

- [ ] **Step 1 (RED):** `_check_proxy_threading()`（见 spec §8）：monkeypatch `httpx.Client` spy 捕获 kwargs，`authed_client(sess, proxy="http://p")`（用未过期 jwt 避免触网）→ 捕获 `proxy=="http://p"`；不传 proxy → 无 `proxy` kwarg。
- [ ] **Step 2 (RED):** `python selfcheck.py` → FAIL（`authed_client` 无 `proxy` kw）。
- [ ] **Step 3 (GREEN):** `stt.authed_client` 用 kwargs dict，`if proxy: kwargs["proxy"]=proxy`（httpx 0.28 单数）。`account_from_password_signin` 加 `proxy` 转发给 `firebase_signin_password`。`firebase_signin_password` 的 `httpx.post` → `if proxy: with httpx.Client(proxy=proxy,timeout=30) as c: c.post(...)` else 原 `httpx.post`（保持行为）。
- [ ] **Step 4:** 确认 `register.py` 编排（`UICoordinateStrategy.register`）两处已传 `proxy=proxy_url`（Task 4 已含则跳过）。
- [ ] **Step 5 (GREEN):** `python selfcheck.py` → `selfcheck ok`。
- [ ] **Step 6:** `git add stt.py register.py selfcheck.py && git commit -m "feat(register): thread proxy through Firebase signin + ElevenLabs API"`

---

### Task 7: 文档

**Files:** Modify `README.md`（macOS 首次运行 + 策略选择 + 住宅代理）、`docs/temp-email-backend.md`（Mac 无差异，走同一 HTTP API 一句话）。

- [ ] **Step 1:** README 注册机章节后加：**策略选择**（`[register].strategy=ui`，http/cdp 为未实现扩展点）、**macOS 首次运行**（Accessibility 权限、浏览器探测/`$ELEVENLABS_STT_CHROME`、勿切走临时窗口）、**代理**（`[proxy]` 必须住宅 IP、fail_threshold/cooldown/strict 语义）。文案见 spec §6 + 原计划 Task 6。
- [ ] **Step 2 (GREEN):** `python selfcheck.py` → `selfcheck ok`（确认文档改动没碰坏代码）。
- [ ] **Step 3:** `git add README.md docs/temp-email-backend.md && git commit -m "docs: register strategies, macOS first-run (Accessibility), residential proxy"`

---

## 手动集成测试（合并前跑，非 CI）

离线 selfcheck 覆盖：ProxyDriver 状态机、域名轮换、策略分发、UICoordinateStrategy 编排、Mac 浏览器探测、proxy 参数贯通。真机需：

1. **Windows 回归**：主分支 vs 本分支各跑一次 `stt` 注册（strategy 默认 ui），确认坐标/时序/成功率无差异。
2. **Windows + 代理**：`[proxy] proxies=["http://住宅…"]`，chrome://net-internals 确认走代理。
3. **macOS 干净机**：装依赖 → 首次跑 → 系统弹 Accessibility → 授权 → 再跑到「注册完成」。
4. **macOS + 代理**：同 2，Mac 验证。
5. **扩展点冒烟**：`[register] strategy="http"` → 立即 `NotImplementedError` 指引；`strategy="zzz"` → `SystemExit`。

## Self-Review 记录

- **Spec 覆盖**：§4.0 策略层→Task 3；§4.1 PlatformDriver→Task 4；§4.1.2 Mac→Task 5；§4.2 ProxyDriver→Task 1；§4.3 EmailProvider+域名轮换→Task 2；§5 dispatcher+UI 编排→Task 3/4；§6 配置→Task 1/3/7；§7 错误处理→各 Task SystemExit/NotImplementedError；§8 测试→各 Task selfcheck；§9 顺序→Task 1-7 一致；§10 YAGNI→占位不实现。全覆盖。
- **依赖顺序**：1/2 独立；3 依赖 1/2；4 依赖 3（填 UI 空壳）；5 依赖 4（满足 PlatformDriver）；6 依赖 4（编排传 proxy）；7 收尾。TDD 每 Task RED→GREEN→commit。
- **零回退**：Task 4 逐字搬 Windows 代码；`register_one` 裸调经 dispatcher→ui→UICoordinateStrategy，等价旧流程。
- **占位符**：无 TBD/TODO。
