# 注册机 Mac 适配 + 代理池最小实现 — 设计

**日期**：2026-09-14
**作用范围**：`register.py`（重构 + 拆分）、`stt.py`（少量：`[proxy]` 配置读取、`has_temp_email_config` 类比）、`config.example.toml`（新增 `[proxy]`）、`README.md`（Mac 首次运行说明）、`docs/temp-email-backend.md`（补 Mac 差异一节）。
**不影响**：转录主流程、`web.py` 现有 `/api/*` 契约、`webui.html` 现有交互。

---

## 1. 背景与问题

`register.py` 目前是「Windows 独占 + 无代理」的实现：

- **Windows 独占**：`subprocess.Popen(r"C:\Program Files\Google\Chrome\...")` 硬编码路径；窗口查找与前台抢占依赖 PowerShell WMI (`Get-CimInstance Win32_Process`) + `ctypes.windll.user32` (`SetForegroundWindow` / `AttachThreadInput` / `SetWindowPos`)；清理走 PowerShell `Stop-Process`。
- **无代理**：Chrome 与 `httpx.Client()` 都直连出网；无从旋转 IP，一台机器长期同 IP 大量注册的封号风险不可控。
- **平台特化未隔离**：Windows 特有代码与主注册流程（填表 → 拉验证邮件 → 打验证链接 → 登录 → 取 refresh token）耦合在 `register_one()` 一函数（`register.py:84-316`）内，Mac 适配无法只加、必须先拆。

同时用户提出了参考项目 [lxf746/any-auto-register](https://github.com/lxf746/any-auto-register)。评估结论（brainstorming 阶段已达成）：

- **不采用 fork 或迁移代码**：AGPL-3.0 会强制本项目所有衍生代码开源为 AGPL；且它是 FastAPI + React + Electron 的独立平台（50k+ LOC），与本项目 `PRODUCT.md` 明写的「零构建、零依赖，前端单文件 HTML，后端标准库」正交；且其内置平台列表不含 ElevenLabs，"Anything" 通用适配器仍需自行映射。
- **借用两个接口思路**（本文档采纳）：
  1. `ProxyDriver` 协议 —— 静态列表 + 加权轮换 + 连续 N 次失败自动禁用。
  2. `EmailProvider` 协议 —— `create_address()` / `poll_mail()`。本项目 `register.py:31-79` 已是此形状，本次正式化。
- **Camoufox / 远程 captcha**：暂不引入。`register.py:105` 的既有注释指出「坐标 + 真 Chrome」组合不触发 hCaptcha，这是我们区别于 selector 类自动化的核心资产；只有实际观察到 captcha 时才考虑引入。

---

## 2. 目标 / 非目标

### 目标 (Goals)

1. **可插拔多策略架构**：`RegisterStrategy` 协议 + `[register].strategy` 配置分发；本轮实现 `UICoordinateStrategy`（真 Chrome），`HTTPProtocolStrategy` / `StealthCDPStrategy` 作为占位扩展点（协议 + 配置键就位，调用即 `NotImplementedError` 明示指引）。下轮加策略只新增一个 class，不动 dispatcher 与共享服务。
2. `UICoordinateStrategy` 在 macOS 上端到端可跑通（Apple Silicon + Intel），依赖仅新增 macOS 自带的 `osascript` / `pkill`；Windows 行为零回退。
3. 引入 `[proxy]` 配置段与 `ProxyDriver`（住宅 IP），Chrome 与 `httpx` 走同一代理；连续失败自动禁用轮换；全空退化直连（当前行为）。
4. 平台差异隔离到 `register_platform_win.py` / `register_platform_mac.py`（`UICoordinateStrategy` 内部按 `sys.platform` 注入）；`register.py` 主体不再出现 `os.name == "nt"` 分支。
5. `EmailProvider` 抽象出协议基类，`CloudflareTempEmail` 为第一实现，**用 `domains[]` 做地址域名轮换**（≤3 次重试，应对 ElevenLabs 拒收部分域名）；未来切 provider 只改一处。
6. `CaptchaSolver` 协议定义就位（供 HTTP/CDP 策略下轮用；本轮不实现任何 solver）。
7. `web.py` 与 `stt.py` 现有 API 与 CLI 契约不变（`register.register_one()` 签名向后兼容，裸调行为等价于旧 UI 流程）。

### 非目标 (Non-goals)

- Linux 支持（结构上不阻碍，但本次不实现、不测试、不写 driver）。
- 动态代理提取 API、residential proxy 采购集成（本次只做静态列表；文档说明"必须用住宅 IP"）。
- **本轮不实现** HTTP 协议策略与隐身 CDP 策略——只定义 `RegisterStrategy` / `CaptchaSolver` 协议接口 + 占位实现（`NotImplementedError` + 清晰指引），作为**扩展点**，下轮插入。
- Captcha 求解引擎（Camoufox / EzCaptcha / 2Captcha 的实际调用）——随 HTTP/CDP 策略下轮做；本轮只留 `CaptchaSolver` 协议。
- 注册硬化：指数退避重试、封号识别、账号污染检测、失败自动回滚 temp-mail 地址。
- 迁移 `any-auto-register` 任何一行代码；不与其 API/DB 互通（只借其"多策略插件"架构思路）。
- 修改 `webui.html`「启动注册机」模态的字段或交互（`[proxy]` / `[register]` / `[captcha]` 段仅通过 `config.toml` 编辑，不上 UI）。

### 研究结论（社区调研，影响设计的事实）

- ElevenLabs 注册用 **hCaptcha 被动/隐形模式**（sitekey `3aad1500-7e79-4051-aac5-6852324dab76`），是否弹可见挑战取决于 4 因子：指纹、IP 信誉、会话历史、操作节奏。
- **"真 Chrome + 坐标"能过验证码是因为它把指纹和输入拉满了，不是因为坐标本身**；隐身 CDP 浏览器（Kameleo/Camoufox/nodriver/Patchright）用 selector 自动化 + 零打码也能拿到隐形放行。→ UI 策略保留真 Chrome 是正确的低风险选择；CDP 策略是它的可脚本化替代。
- **纯 HTTP 协议路线放弃隐形放行 → 每次注册都要付费打码**（EzCaptcha/2Captcha，~$1–3/千次）；底层是 Firebase Auth（`identitytoolkit.googleapis.com`）经 `api.us.elevenlabs.io/v1/user/*` 包装。这是 `CaptchaSolver` 协议存在的原因。
- **代理必须是住宅/轮换 IP**：ElevenLabs 政策"每 IP 一个免费账号"，同 IP 重复 → `"Unusual activity detected. Free Tier usage disabled."`；数据中心 IP 会被 hCaptcha IP 信誉因子直接标记。`ProxyDriver` 类型无关（只吃 URL），但配置注释与文档必须写明用住宅 IP。
- **ElevenLabs 拒收很多一次性邮箱域名** → `CloudflareTempEmail.create_address` 用现有 `domains[]` 做域名轮换（≤3 次重试），降低"地址被拒"导致的注册失败。

---

## 3. 架构总览

**两层抽象**：顶层是「用哪种注册**策略**」（UI 真 Chrome / HTTP 协议 / 隐身 CDP），策略之下是所有策略复用的**共享服务**（邮箱、代理、打码）。`register_one()` 退化为「按配置选策略并注入共享服务」的 dispatcher。

```
register.py
  register_one(*, strategy=None, provider=None, proxy_driver=None, ...) -> account
    │  按 stt.register_config()["strategy"] 选策略，注入共享服务
    │
    ├─ RegisterStrategy (Protocol)          ← 顶层抽象（新增）
    │     def register(self, *, provider, proxy_driver, captcha=None) -> dict
    │   ├─ UICoordinateStrategy             ← 本轮实现（现有 Win 流程 + Mac）
    │   │     └─ PlatformDriver (Protocol)  ← UI 策略内部：OS 窗口管理
    │   │         ├─ WinDriver              (现有 register.py Windows 代码搬入)
    │   │         └─ MacDriver              (新增：AppleScript + pyautogui)
    │   ├─ HTTPProtocolStrategy             ← 扩展点（本轮 NotImplementedError 占位）
    │   │     用 curl_cffi + Firebase 流程 + CaptchaSolver
    │   └─ StealthCDPStrategy               ← 扩展点（本轮 NotImplementedError 占位）
    │         用 nodriver/Patchright/Camoufox
    │
    共享服务（所有策略复用）:
    ├─ EmailProvider (Protocol)             ← CloudflareTempEmail（+域名轮换）
    ├─ ProxyDriver                          ← 静态列表 + 轮换 + N 次失败禁用（住宅 IP）
    └─ CaptchaSolver (Protocol)             ← 扩展点：仅 HTTP/CDP 策略需要；本轮只定义协议

register_platform_win.py   (WinDriver — 现有 Windows 代码原地搬)
register_platform_mac.py   (MacDriver — 新增)
```

**依赖方向**：`stt.py` 与 `web.py` 只 import `register.register_one`，不感知策略/平台/代理。`register_one` 按 `[register].strategy` 选策略；`UICoordinateStrategy` 内部按 `sys.platform` 挑 `PlatformDriver`。HTTP/CDP 策略这轮是占位类：被选中时 `raise NotImplementedError("HTTP 协议策略尚未实现；见 docs/…；当前请用 strategy='ui'")`。

**为什么策略层在平台层之上**：平台差异（Win/Mac 窗口管理）只在"真 Chrome"这一种策略里存在；HTTP 策略无浏览器、无平台差异。所以 `PlatformDriver` 属于 `UICoordinateStrategy` 的内部实现，不是顶层抽象。这修正了本设计初版把 `PlatformDriver` 当顶层的错位。

---

## 4. 组件详设

### 4.0 `RegisterStrategy` 分发层 + `CaptchaSolver` 扩展点

**动机**：用户要求"支持多种"注册路线并把"协议"接上。借 `any-auto-register` 的多策略插件思路（不抄代码），把"如何完成一次 ElevenLabs 注册"抽成可换的策略。

**协议**（`register.py`）：

```python
class RegisterStrategy(Protocol):
    def register(self, *, provider: EmailProvider,
                 proxy_driver: "ProxyDriver",
                 captcha: "CaptchaSolver | None" = None) -> dict[str, Any]: ...
```

**分发**（`register_one` 顶层）：

```python
_STRATEGIES = {
    "ui": lambda: UICoordinateStrategy(),
    "http": lambda: HTTPProtocolStrategy(),   # 本轮占位
    "cdp": lambda: StealthCDPStrategy(),      # 本轮占位
}

def register_one(*, strategy: RegisterStrategy | None = None,
                 provider=None, proxy_driver=None, captcha=None) -> dict[str, Any]:
    if strategy is None:
        name = stt.register_config()["strategy"]        # 默认 "ui"
        factory = _STRATEGIES.get(name)
        if factory is None:
            raise SystemExit(f"未知 register.strategy: {name!r}；可选 {list(_STRATEGIES)}")
        strategy = factory()
    provider = provider or CloudflareTempEmail(stt.temp_email_config())
    proxy_driver = proxy_driver or _proxy.ProxyDriver(stt.proxy_config())
    return strategy.register(provider=provider, proxy_driver=proxy_driver, captcha=captcha)
```

**扩展点占位**（本轮不实现）：

```python
class HTTPProtocolStrategy:
    def register(self, **_):
        raise NotImplementedError(
            "HTTP 协议策略尚未实现。需要 curl_cffi + Firebase 流程 + 付费 CaptchaSolver；"
            "见 docs/superpowers/specs/…（研究结论段的完整流程）。当前请用 [register] strategy='ui'。")

class StealthCDPStrategy:
    def register(self, **_):
        raise NotImplementedError(
            "隐身 CDP 策略尚未实现。需引入 nodriver/Patchright/Camoufox。当前请用 strategy='ui'。")
```

**`CaptchaSolver` 协议**（仅定义，供 HTTP/CDP 策略下轮用）：

```python
class CaptchaSolver(Protocol):
    def solve_hcaptcha(self, sitekey: str, page_url: str) -> str: ...   # 返回 token
```

**配置**（新增 `config.example.toml`）：

```toml
[register]
strategy = "ui"          # ui(真 Chrome,默认) | http(协议,未实现) | cdp(隐身,未实现)

[captcha]                # 仅 http/cdp 策略需要；ui 策略忽略
provider = ""            # ezcaptcha | 2captcha | yescaptcha
api_key = ""
```

`stt.register_config()` 对齐 `temp_email_config` 样式（dict-with-defaults + `load_toml().get("register", {})`），默认 `{"strategy": "ui"}`。

**为什么占位而不省略**：留下 `NotImplementedError` 占位类 + 协议 + 配置键，是为了固化"扩展点"契约——下轮加 HTTP 策略时只新增一个 class + 一个 `CaptchaSolver` 实现，不动 dispatcher、不动 UI 策略、不动共享服务。这就是"可插拔"的验收标准。

---

### 4.1 `PlatformDriver` 协议（`UICoordinateStrategy` 内部）

> **层级**：`PlatformDriver` 不是顶层抽象，而是 `UICoordinateStrategy` 的内部实现——只有"真 Chrome"策略需要 OS 窗口管理。`UICoordinateStrategy.register()` 承接原 `register_one` 的编排（建邮箱→启动 Chrome→填表→拉验证信→开链接→登录→取 token），按 `sys.platform` 注入 `WinDriver` / `MacDriver`。

**动机**：`register.py:107-316` 里所有 `os.name == "nt"` 分支、`ctypes.windll`、PowerShell 调用都是「操作系统如何管理 Chrome 窗口」的具体实现，与「填什么内容、等哪封邮件」正交。

**协议签名**（在 `register.py` 顶部用 `typing.Protocol`）：

```python
class PlatformDriver(Protocol):
    def launch_chrome(
        self,
        profile_dir: pathlib.Path,
        signup_url: str,
        proxy: str | None,   # "http://host:port" or None
    ) -> subprocess.Popen: ...

    def find_profile_window(
        self,
        profile_dir: pathlib.Path,
        timeout_s: float,
    ) -> "WindowHandle": ...

    def ensure_foreground(self, window: "WindowHandle") -> None: ...

    def kill_profile(
        self,
        profile_dir: pathlib.Path,
        popen: subprocess.Popen | None,
    ) -> None: ...
```

`WindowHandle` 是不透明对象；两个 driver 各自返回一个内部类型，暴露 `left/top/width/height` 属性给 `click_frac()` 用（`register.py:249-258` 现在直接读 `new_window.left/top`）。

#### 4.1.1 Windows driver（`register_platform_win.py`）

**行为等价搬迁**，不做逻辑改动：

- `launch_chrome`：搬 `register.py:135-156`（Chrome 路径、`STARTUPINFO`、`CREATE_NEW_PROCESS_GROUP`、`--user-data-dir` / `--no-first-run` / `--new-window` / `--window-position` / `--disable-save-password-bubble` / `--do-not-de-elevate`）+ 新增 `--proxy-server=<proxy>` 当 proxy 非空。
- `find_profile_window`：搬 `register.py:107-133`（PowerShell WMI + `pygetwindow`）。
- `ensure_foreground`：搬 `register.py:187-229`（`GetForegroundWindow` / `AttachThreadInput` / `SetForegroundWindow` / topmost toggle）。
- `kill_profile`：搬 `register.py:300-316`（PowerShell `Stop-Process` + `shutil.rmtree` 重试）。

**依赖**：`pygetwindow`、`ctypes.windll`（Windows only；import 放在 driver 内部，Mac 不加载）。

#### 4.1.2 macOS driver（`register_platform_mac.py`）

**Chrome 路径探测**（顺序）：

1. `$ELEVENLABS_STT_CHROME` 环境变量（用户覆盖）。
2. `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
3. `/Applications/Brave Browser.app/Contents/MacOS/Brave Browser`（用户 `CLAUDE.md` 声明默认 Brave；两者命令行 flag 兼容）
4. `mdfind "kMDItemCFBundleIdentifier == 'com.google.Chrome'"` 兜底。

找不到 → `SystemExit("register 需要 Chrome 或 Brave，未在标准路径找到；设 $ELEVENLABS_STT_CHROME 指向可执行文件")`。

**`launch_chrome`**：`subprocess.Popen([chrome_path, "--user-data-dir=…", "--no-first-run", "--new-window", "--window-position=40,40", "--disable-save-password-bubble", *(["--proxy-server=" + proxy] if proxy else []), signup_url])`。不需要 `STARTUPINFO`，不需要 `CREATE_NEW_PROCESS_GROUP`。

**`find_profile_window`**：用 AppleScript 查询同 PID 的 Chrome window，返回 `bounds`（AppleScript 原生给 `{x1, y1, x2, y2}`）：

```applescript
tell application "Google Chrome"
    set win to first window whose id is not missing value
    return bounds of win
end tell
```

按 `popen.pid` 过滤（AppleScript 拿不到 PID 时，退回：轮询直到该 profile_dir 下 `Default/Preferences` 存在 + 出现「新 window，1s 内 bounds 稳定」即视为目标窗口）。返回的 `WindowHandle` 是 `namedtuple` `(left, top, width, height, chrome_pid)`。

**`ensure_foreground`**：
```applescript
tell application "Google Chrome" to activate
```
比 Windows 简单一个量级 —— macOS 不允许后台进程无声抢前台，但用户主动运行的 CLI 是前台进程，`activate` 直接生效。**前置条件**：终端 app（iTerm/Terminal）需要 **Accessibility 权限**（System Settings → Privacy & Security → Accessibility），否则 PyAutoGUI 的键盘/鼠标事件被系统丢弃。README 会把这一步写进「首次运行」。

**`kill_profile`**：`subprocess.run(["pkill", "-f", str(profile_dir)])` + `shutil.rmtree` 重试。

**依赖**：仅 macOS 自带 `osascript` / `pkill`；`pyautogui` / `pyperclip` 已是跨平台可用（Mac 版内部会走 CGEvent / pbcopy）；**不需要** `pygetwindow`（Mac 支持很差），窗口 bounds 用 AppleScript 直接拿。

#### 4.1.3 平台选择

`register.py` 顶部：

```python
def _default_platform_driver() -> PlatformDriver:
    if sys.platform == "darwin":
        from register_platform_mac import MacDriver
        return MacDriver()
    if os.name == "nt":
        from register_platform_win import WinDriver
        return WinDriver()
    raise SystemExit(f"register 目前只支持 macOS 与 Windows；当前平台: {sys.platform}")
```

延迟 import 遵循现有约定（`register.py:26` 已把 `import stt` 单例化；平台 driver 只在 `register_one()` 首次调用时载入）。

### 4.2 `ProxyDriver`

**动机**：单机长期同 IP 大量注册的封号风险不可控（用户两次强调此点）。参考 any-auto-register：静态列表 + 加权 + N 次失败禁用；本次做**最小实现**，不做动态提取 API、不做 residential 集成。

**配置**（新增 `config.example.toml [proxy]` 段）：

```toml
[proxy]
# List of upstream proxies (HTTP or SOCKS5). Empty → direct connection (current behavior).
# Example: proxies = ["http://u:p@1.2.3.4:8080", "socks5://5.6.7.8:1080"]
proxies = []
fail_threshold = 3          # 连续失败几次后临时禁用该代理
cooldown_secs = 900         # 禁用后多久重新纳入池子（默认 15min）
strict = false              # true 时全池不可用直接报错；false 时降级为直连
```

**接口**（`register.py`）：

```python
@dataclasses.dataclass
class Proxy:
    url: str                # 完整 URL，直接传给 Chrome --proxy-server 与 httpx proxies=
    fails: int = 0
    disabled_until: float = 0.0   # unix ts

class ProxyDriver:
    def __init__(self, cfg: dict): ...
    def pick(self) -> Proxy | None:      # None = 直连；池空或全 disabled 时依 strict 决定
    def mark_ok(self, p: Proxy) -> None: # 归零 fails
    def mark_fail(self, p: Proxy) -> None:  # fails += 1; 到阈值 → disabled_until = now + cooldown
```

**选择策略**：从「未禁用」子集里 round-robin（用一个游标 index）。不做加权 —— 加权在没有历史成功率数据的启动阶段是幻觉，YAGNI；游标+失败降级已经覆盖「让活的代理更常被选」的实际诉求。

**失败判定**：`register_one()` 内所有捕获到的 `SystemExit` / `httpx.HTTPError` / Chrome 启动失败（`Popen` 后 5s 内进程退出）→ `proxy_driver.mark_fail(p)` 再重新 `raise`。成功走完 `refresh_credits()` → `mark_ok(p)`。**本次不做自动重试**（属于「注册硬化」非目标）—— 失败即向上抛，下一次 `register_one()` 调用会自然拿到下一个代理。

**Chrome 与 httpx 一致性**：
- Chrome：`--proxy-server=` 直接支持 `http://…` / `socks5://…` 格式。
- httpx：本项目 httpx==0.28.1，用 `httpx.Client(proxy=proxy.url)`（单数 `proxy=`；`proxies=` 在 0.28 已移除）。
- `temp_email_create` / `latest_verify_link` （对 cloudflare_temp_email 后端的调用）**是否走代理**？→ **走**。同一 IP 出去更一致；且如果代理挂了，temp-mail 也失败，一次性 fail 更快，避免「Chrome 用代理、httpx 直连、只 Chrome 侧封」的分裂状态。

**代理未配置**：`ProxyDriver([])` → `pick()` 永远返回 `None` → 所有下游看到 `proxy=None` → 走当前直连路径。这是零配置迁移路径 —— 老用户 `config.toml` 不用改。

### 4.3 `EmailProvider` 协议

**动机**：现在 `register.py:31-79` 直接调 cloudflare_temp_email 的 `/admin/new_address` 与 `/api/parsed_mails`。协议化 → 未来切 MoeMail / TempMail.lol 时只加新实现。

**协议**：

```python
class EmailProvider(Protocol):
    def create_address(self) -> "EmailAddress": ...   # {address, jwt or token, provider_meta}
    def poll_verification_link(
        self,
        addr: "EmailAddress",
        pattern: re.Pattern,
        timeout_s: float,
        interval_s: float,
    ) -> str: ...
```

**第一实现 `CloudflareTempEmail`**：把现有 `temp_email_create` / `latest_verify_link` 收进 class；构造函数吃 `stt.temp_email_config()` 结果。admin/user 双路径 fallback 与轮询逻辑**行为等价**。

**新增：域名轮换（分散版，非重试版）**（研究结论——ElevenLabs 拒收部分域名）。`create_address()` 按 `cfg["domains"]`（回退 `[cfg["domain"]]`）**轮换**选域名建址——一个 `CloudflareTempEmail` 实例内用游标，连续多次 `create_address()`（如池预热注册 N 个账号）会分散到不同域名，避免所有账号押在同一个可能被拒的域名上。**若 `domains[]` 只有一个，行为等价于现在。**

> **限定**："某域名地址被 ElevenLabs 拒收 → 换域名重试同一次注册"这种**重试版**本轮不做——UI 坐标策略读不到页面 DOM，无法感知"域名被拒"，它只会表现为验证邮件不来→poll 超时→整次失败。失败信号检测 + 单次注册内换域名重试，随 HTTP 策略（有明确 HTTP 错误码）下轮做。本轮只做无副作用的"分散"。

`UICoordinateStrategy` 内：`provider or CloudflareTempEmail(stt.temp_email_config())`（默认注入，测试可替换成假实现）。

---

## 5. `register_one()` dispatcher + `UICoordinateStrategy.register()` 编排

`register_one()` 只做"选策略 + 注入共享服务"（骨架见 §4.0）。原来那段注册编排整体搬进 `UICoordinateStrategy.register()`——它是唯一需要 `PlatformDriver` 与代理生命周期的策略：

```python
class UICoordinateStrategy:
    def __init__(self, platform: PlatformDriver | None = None) -> None:
        self._platform = platform  # None → register() 内按 sys.platform 选

    def register(self, *, provider: EmailProvider, proxy_driver: "ProxyDriver",
                 captcha=None) -> dict[str, Any]:   # captcha 忽略（真 Chrome 拿隐形放行）
        platform = self._platform or _default_platform_driver()
        cfg = stt.temp_email_config()
        proxy = proxy_driver.pick()
        proxy_url = proxy.url if proxy else None
        profile_dir = pathlib.Path(tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-"))
        _write_no_password_prefs(profile_dir)
        popen = None
        try:
            addr = provider.create_address()
            password = stt.random_password()
            popen = platform.launch_chrome(profile_dir, SIGNUP_URL, proxy_url)
            window = platform.find_profile_window(profile_dir, timeout_s=30)
            platform.ensure_foreground(window)
            _fill_signup_form(window, platform, addr.address, password)
            link = provider.poll_verification_link(addr, VERIFY_LINK_PATTERN,
                                                   cfg["poll_timeout_secs"], cfg["poll_interval_secs"])
            _open_verify_link_and_confirm(window, platform, link)
            _sign_in(window, platform, addr.address, password)
            account = stt.account_from_password_signin(addr.address, password,
                                                        temp_address=addr.address, proxy=proxy_url)
            with stt.authed_client(account, save=lambda _s: None, proxy=proxy_url) as client:
                client.get("/v1/user")
                stt.refresh_credits(account, client)
            if proxy: proxy_driver.mark_ok(proxy)
            return account
        except BaseException:
            if proxy: proxy_driver.mark_fail(proxy)
            raise
        finally:
            platform.kill_profile(profile_dir, popen)
```

**代理生命周期归属策略**：`pick/mark_ok/mark_fail` 在 `UICoordinateStrategy.register` 内（每种策略对"什么算失败"判定不同；UI 策略里任何异常都 `mark_fail`）。`register_one` dispatcher 只把 `proxy_driver` 传进去。

**`stt.authed_client` / `stt.account_from_password_signin` 加 `proxy` kw**（默认 `None` 向后兼容）。

**私有 helper**（`_fill_signup_form` / `_open_verify_link_and_confirm` / `_sign_in`）从现有 `register.py:269-291` 剥出，只调 `platform.ensure_foreground` + `pyautogui` + `pyperclip` + `click_frac`（`click_frac` 上移为模块级、接受 `WindowHandle`）。**逻辑不改**——坐标、等待时长（12s/15s/8s）、`click_frac(0.50, 0.56)` 等常量原样搬。

---

## 6. 配置变更

`config.example.toml` 增三段：

```toml
[register]
strategy = "ui"          # ui(真 Chrome,默认) | http(协议,未实现) | cdp(隐身,未实现)

[proxy]
# 必须用住宅/轮换 IP —— 数据中心 IP 会被 ElevenLabs 与 hCaptcha 直接标记。留空=直连。
proxies = []
fail_threshold = 3          # 连续失败几次临时禁用该代理
cooldown_secs = 900         # 禁用后多久重新纳入（默认 15min）
strict = false              # true: 全池不可用直接报错；false: 降级直连

[captcha]                 # 仅 http/cdp 策略需要；ui 策略忽略。本轮未接任何 solver。
provider = ""            # ezcaptcha | 2captcha | yescaptcha
api_key = ""
```

`stt.py` 增两个 config 读取（对齐 `temp_email_config` 样式）：

```python
def register_config(path: pathlib.Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = {"strategy": "ui"}
    cfg.update(load_toml(path).get("register", {}))
    return cfg

def proxy_config(path: pathlib.Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = {"proxies": [], "fail_threshold": 3, "cooldown_secs": 900, "strict": False}
    cfg.update(load_toml(path).get("proxy", {}))
    return cfg
```

（`[captcha]` 本轮不读——没有 solver 消费它；留在 example.toml 作为下轮扩展点的占位说明。）

**不加 `has_proxy_config`**：代理为空池不是错误状态，是「明确直连」。`register.py` 不校验、不告警。

---

## 7. 错误处理与失败模式

| 失败点 | 现在的行为 | 本次后的行为 |
|---|---|---|
| Chrome 找不到（Mac） | N/A | `SystemExit` 明示路径与 `$ELEVENLABS_STT_CHROME` 覆盖办法 |
| Accessibility 权限未授（Mac） | N/A | PyAutoGUI 静默失败 → 表单填不动 → 30s 后 temp-mail 拉不到验证信 → `SystemExit`。README 首次运行章节前置说明，配合 `stt` CLI 首次跑 `register` 时打印一行提示（`register.py` 顶部检测 `sys.platform == "darwin"` 时用 `os.access` 探 `/usr/bin/osascript` + 打 `_rlog("Mac 首次运行需要给终端授予 Accessibility …")`） |
| 代理不可用（连接被拒 / 超时） | N/A | Chrome 启动后 5s 内进程退出 or `httpx` `ConnectError` → 计入 `proxy_driver.mark_fail`；本次调用抛 `SystemExit`；下一次 `register_one` 拿下一个代理 |
| 全部代理禁用且 `strict=false` | N/A | 降级直连并 `_rlog("警告：所有代理已禁用，本次直连注册")` |
| 全部代理禁用且 `strict=true` | N/A | `SystemExit("proxy pool exhausted; strict mode")` |
| ElevenLabs 弹 hCaptcha | 现在不弹（真 Chrome 拿隐形放行）；如果弹了，表单卡住 → poll 邮件超时 → `SystemExit` | UI 策略不变（真 Chrome + 住宅 IP 拿隐形放行）。可见挑战的求解由 HTTP/CDP 策略经 `CaptchaSolver` 处理——本轮占位不实现。 |
| temp-mail 后端拒答 | `SystemExit` | 不变 |

**没有自动重试** —— 失败即向上抛给 `stt.run_plan_pipelined` / `web.do_register`，由它们的循环自然进入下一次 `register_one`（现有 `stt.py:1008-1029` 已经是这个语义）。

---

## 8. 测试策略

**测试载体**：本项目无 pytest、无 `tests/`。所有离线测试是 `selfcheck.py::run()` 里的 `assert`，`python selfcheck.py` 成功打印 `selfcheck ok` 返回 0。新测试一律加 `_check_*()` 函数并在 `run()` 末尾 `print("selfcheck ok")` 前调用。

- **`_check_proxy_driver()`**：pick / mark_ok / mark_fail 状态机；连续失败到阈值→禁用；cooldown 到期→复活（手动改 `disabled_until`）；全禁用 + `strict=false`→`None`；全禁用 + `strict=true`→抛错；空池→`None`。
- **`_check_email_provider()`**：`VERIFY_LINK_PATTERN` 命中/不命中；`CloudflareTempEmail` 能从 cfg 构造（不触网）；**域名轮换**——连续 `create_address()`（monkeypatch httpx 捕获请求 body）在多域名下游标推进、单域名下不变。
- **`_check_register_dispatch()`**（新增，策略层）：`register_one(strategy=FakeStrategy())` 把 `provider`/`proxy_driver` 正确注入并返回其结果；`[register].strategy` 未知值→`SystemExit`；`strategy="http"`/`"cdp"` 走到占位类→`NotImplementedError`。
- **`_check_register_orchestration()`**（UI 策略编排）：假 `PlatformDriver`（记录调用顺序）+ 假 `EmailProvider` + 空 `ProxyDriver`，跑 `UICoordinateStrategy().register(...)`，断言 `create_addr → launch → find → focus → fill → poll → verify → signin → kill` 顺序；`kill_profile` 在 finally 恒被调用；失败路径 `proxy_driver.mark_fail` 被调。
- **`_check_mac_chrome_discovery()`**：`_find_chrome_binary` 的注入式纯逻辑（env 覆盖 > Chrome > Brave > 抛错）。
- **`_check_proxy_threading()`**：`authed_client(proxy=...)` 把 `proxy` 透传给 `httpx.Client`；不传时不带该 kwarg。
- **不测**：平台 driver 内部的 pyautogui / AppleScript / PowerShell（无法离线稳定 mock，且是 battle-tested 现有代码）；占位策略的内部（本轮无实现）。

**手动集成测试**（release 前跑，写进 spec 附录）：
1. Windows：在有 `[temp_email]` 配置的机器上 `stt` CLI 跑一次 `register`，跟主分支对比无回退。
2. Windows + 代理：`[proxy] proxies = ["http://…"]` 跑一次，观察 Chrome 走代理（Chrome 内部 chrome://net-internals 或抓包）。
3. Mac：一台干净 Mac，装依赖 → 首次跑 → 系统弹 Accessibility 请求 → 授权 → 二次跑到完成。
4. Mac + 代理：同 2 但在 Mac。

**不做**：mocked ElevenLabs 端 E2E（现有代码从没有过；引入超范围）。

---

## 9. 分步落地顺序（不写实现，只标依赖）

1. **`ProxyDriver` + `[proxy]` 配置**（纯新增，零风险，独立可测）
2. **`EmailProvider` 抽象 + 域名轮换**（`temp_email_create` / `latest_verify_link` 搬进 class）
3. **`RegisterStrategy` 分发层 + `CaptchaSolver` 协议 + HTTP/CDP 占位 + `[register]` 配置**（`register_one` 变 dispatcher；用假策略测分发）
4. **`PlatformDriver` 协议 + `WinDriver` 抽取 + `UICoordinateStrategy` 承接编排**（Windows 代码原地搬；Windows 跑通 = 重构无回退）
5. **`MacDriver` 实现**（新增，不影响 Windows）
6. **proxy 贯通 `stt.py` httpx**（Firebase 登录 + ElevenLabs API 加 `proxy` kw）
7. **文档**（`config.example.toml`、README Mac 首次运行 + 策略选择 + 住宅代理、`docs/temp-email-backend.md`）

依赖链：3 依赖 1/2（dispatcher 注入共享服务）；4 依赖 3（UI 策略实现 `RegisterStrategy`）；5 依赖 4（Mac driver 满足 `PlatformDriver`）；6 依赖 4（编排里传 proxy）。前 4 步除新增策略层外不改 Windows 行为。

writing-plans 已把每步拆成 TDD 循环（见 `docs/superpowers/plans/2026-09-14-register-mac-and-proxy.md`，本次同步更新）。

---

## 10. 明确不做（YAGNI 清单，防本轮 scope creep）

**本轮做占位扩展点、不做实现**（下轮插入，接口已固化）：
- `HTTPProtocolStrategy` 实现（curl_cffi + Firebase 流程 + 单次注册内域名重试）
- `StealthCDPStrategy` 实现（nodriver/Patchright/Camoufox）
- 任何 `CaptchaSolver` 实现（EzCaptcha/2Captcha/YesCaptcha 实际调用）+ `[captcha]` 消费

**本轮彻底不做**：
- 动态代理提取 API、residential proxy 采购集成、加权代理选择（按历史成功率）
- 注册失败自动重试 / 指数退避、封号识别、账号污染检测、自动回滚失败注册占用的 temp-mail 地址
- UI 策略里"域名被拒→换域名重试同一次注册"（读不到 DOM，见 §4.3 限定）
- Linux driver
- `webui.html`「启动注册机」模态里加 `[proxy]`/`[register]`/`[captcha]` UI（只走 `config.toml`）
- 迁移 `any-auto-register` 任何代码 / 与其 API 互通（只借多策略架构思路）

---

## 11. 已定决策（原开放问题，用合理默认拍板，可随时推翻）

1. **Brave vs Chrome 优先级**：Mac 探测 **Chrome → Brave**（Chrome 装机面更广），`$ELEVENLABS_STT_CHROME` 覆盖。用户默认浏览器是 Brave，但注册用**独立临时 profile**（`--user-data-dir` 全新目录），不复用日常浏览器 session，所以用哪个牌子不影响登录态，优先级纯看装机概率。
2. **代理/策略配置不上 UI**：`[proxy]`/`[register]`/`[captcha]` 只走 `config.toml`——代理是敏感付费信息、策略是低频专家开关，模态明文渲染不合适。
3. **`strict=false` 降级直连不加二次确认**：直接降级 + `_rlog` 告警；想强制失败的用户用 `strict=true`。不引入新 UI 状态。

> 这三条都是低风险默认。若 review 时想改，只影响 `_find_chrome_binary` 顺序 / 是否加 UI 字段 / 是否加确认——都是局部改动。
