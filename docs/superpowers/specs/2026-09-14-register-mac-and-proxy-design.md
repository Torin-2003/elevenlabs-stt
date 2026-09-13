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

1. `register_one()` 在 macOS 上端到端可跑通（Apple Silicon + Intel 均需支持），依赖仅新增 macOS 系统自带的 `osascript` 与 `pkill`。
2. 引入 `[proxy]` 配置段与 `ProxyDriver`，Chrome 与 `httpx` 走同一代理；连续失败自动禁用坏代理并轮换到下一个；全空则退化到直连（当前行为）。
3. 平台差异隔离到 `register_platform_win.py` / `register_platform_mac.py`；`register.py` 主体不再出现 `os.name == "nt"` 分支。
4. `EmailProvider` 抽象出协议基类，`cloudflare_temp_email` 作为第一实现；未来切 provider 只改一处。
5. `web.py` 与 `stt.py` 现有 API 与 CLI 契约不变（`do_register()` 内部替换 driver 不影响 UI）。

### 非目标 (Non-goals)

- Linux 支持（结构上不阻碍，但本次不实现、不测试、不写 driver）。
- 动态代理提取 API、residential proxy 采购集成。
- Captcha 求解（Camoufox / 2Captcha / YesCaptcha）。
- 注册硬化：指数退避重试、封号识别、账号污染检测、失败自动回滚 temp-mail 地址。
- 迁移 `any-auto-register` 任何一行代码；不与其 API/DB 互通。
- 修改 `webui.html`「启动注册机」模态的字段或交互（`[proxy]` 段仅通过 `config.toml` 编辑，不上 UI）。

---

## 3. 架构总览

```
register.py                        (主流程，跨平台)
  ├─ EmailProvider (Protocol)      ← 抽象
  │   └─ CloudflareTempEmail       (现有 register.py:31-79 抽出)
  ├─ ProxyDriver                   ← 新增
  │   ├─ pick()  -> Proxy | None
  │   ├─ mark_ok(proxy)
  │   └─ mark_fail(proxy)          (N 次失败 → disable 15min)
  ├─ PlatformDriver (Protocol)     ← 抽象
  │   ├─ launch_chrome(profile_dir, proxy, url) -> Handle
  │   ├─ find_window(profile_dir, timeout_s) -> Window
  │   ├─ focus(window)
  │   └─ kill_profile(profile_dir)
  └─ register_one(provider, proxy_driver, platform) -> account
       (纯编排，无平台/网络具体实现)

register_platform_win.py           (现有 Windows 代码原地搬)
register_platform_mac.py           (新增)
```

**依赖方向**：`stt.py` 与 `web.py` 只 import `register.register_one`；`register.py` 内部按 `sys.platform` 挑 `register_platform_{win,mac}` 并注入 `PlatformDriver`。`stt.py` 与 `web.py` 不再感知平台。

---

## 4. 组件详设

### 4.1 `PlatformDriver` 协议

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
- httpx：`httpx.Client(proxies=proxy.url)` 同格式。
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

**第一实现 `CloudflareTempEmail`**：把现有 `temp_email_create` / `latest_verify_link` 收进 class；构造函数吃 `stt.temp_email_config()` 结果。**行为完全等价**，不改 API 调用顺序、不改 admin/user 双路径 fallback。

`register_one()` 内：`provider = provider or CloudflareTempEmail(stt.temp_email_config())`（默认注入，测试可替换成假实现）。

---

## 5. `register_one()` 编排后的骨架

```python
def register_one(
    *,
    provider: EmailProvider | None = None,
    proxy_driver: ProxyDriver | None = None,
    platform: PlatformDriver | None = None,
) -> dict[str, Any]:
    provider = provider or CloudflareTempEmail(stt.temp_email_config())
    proxy_driver = proxy_driver or ProxyDriver(stt.proxy_config())
    platform = platform or _default_platform_driver()

    proxy = proxy_driver.pick()
    profile_dir = pathlib.Path(tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-"))
    _write_no_password_prefs(profile_dir)

    popen = None
    try:
        addr = provider.create_address()
        password = stt.random_password()
        popen = platform.launch_chrome(profile_dir, SIGNUP_URL, proxy.url if proxy else None)
        window = platform.find_profile_window(profile_dir, timeout_s=30)
        platform.ensure_foreground(window)
        _fill_signup_form(window, platform, addr.address, password)
        link = provider.poll_verification_link(addr, VERIFY_LINK_PATTERN,
                                               timeout_s=cfg["poll_timeout_secs"],
                                               interval_s=cfg["poll_interval_secs"])
        _open_verify_link_and_confirm(window, platform, link)
        _sign_in(window, platform, addr.address, password)
        account = stt.account_from_password_signin(addr.address, password,
                                                    temp_address=addr.address,
                                                    proxy=proxy.url if proxy else None)
        with stt.authed_client(account, save=lambda _s: None, proxy=proxy.url if proxy else None) as client:
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

**`stt.authed_client` / `stt.account_from_password_signin` 需要加 `proxy` kw**（当前签名不带；改动局部，默认 `None` 保持向后兼容）。

**私有 helper**（`_fill_signup_form` / `_open_verify_link_and_confirm` / `_sign_in`）是从现有 `register.py:269-291` 剥出的三段脚本；它们只调 `platform.ensure_foreground` + `pyautogui` + `pyperclip` + `click_frac`（`click_frac` 也上移为模块级函数，接受 `WindowHandle`）。**逻辑不改**，只做搬家 —— 坐标、等待时间（12s / 15s / 8s）、`click_frac(0.50, 0.56)` 等常量原样搬。

---

## 6. 配置变更

`config.example.toml` 增：

```toml
[proxy]
proxies = []
fail_threshold = 3
cooldown_secs = 900
strict = false
```

`stt.py` 增（对齐 `temp_email_config` 的样式）：

```python
def proxy_config(path: pathlib.Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = {"proxies": [], "fail_threshold": 3, "cooldown_secs": 900, "strict": False}
    cfg.update(load_toml(path).get("proxy", {}))
    return cfg
```

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
| ElevenLabs 弹 hCaptcha | 现在不弹（真 Chrome）；如果弹了，表单卡住 → poll 邮件超时 → `SystemExit` | 不变。**不引入 captcha 求解**，属非目标。 |
| temp-mail 后端拒答 | `SystemExit` | 不变 |

**没有自动重试** —— 失败即向上抛给 `stt.run_plan_pipelined` / `web.do_register`，由它们的循环自然进入下一次 `register_one`（现有 `stt.py:1008-1029` 已经是这个语义）。

---

## 8. 测试策略

**单元测试**（新增 `tests/test_proxy_driver.py`）：
- `ProxyDriver` 的 pick / mark_ok / mark_fail 状态机；连续失败到阈值→禁用；cooldown 到期→复活；全禁用 + `strict=false`→返回 `None`；全禁用 + `strict=true`→抛错。
- 用 monkeypatch 冻结 `time.time()` 覆盖 cooldown 逻辑。

**接口契约测试**（新增 `tests/test_register_platform.py`）：
- 用假 `PlatformDriver`（记录调用顺序的 mock）+ 假 `EmailProvider`（返回固定 address + link）+ 空 `ProxyDriver`，跑 `register_one`，断言：
  - `launch_chrome → find_profile_window → ensure_foreground → fill_form → poll_link → open_link → sign_in → account_from_password_signin → kill_profile` 顺序不变。
  - `kill_profile` 无论中间抛什么都被调用（finally 语义）。
  - `proxy_driver.mark_ok/mark_fail` 与结果匹配。
- 这层覆盖「重构没搞坏编排逻辑」。**不测**平台 driver 内部的 pyautogui / AppleScript / PowerShell 调用（无法在 CI 稳定 mock，且它们是已经 battle-tested 的现有代码）。

**手动集成测试**（release 前跑，写进 spec 附录）：
1. Windows：在有 `[temp_email]` 配置的机器上 `stt` CLI 跑一次 `register`，跟主分支对比无回退。
2. Windows + 代理：`[proxy] proxies = ["http://…"]` 跑一次，观察 Chrome 走代理（Chrome 内部 chrome://net-internals 或抓包）。
3. Mac：一台干净 Mac，装依赖 → 首次跑 → 系统弹 Accessibility 请求 → 授权 → 二次跑到完成。
4. Mac + 代理：同 2 但在 Mac。

**不做**：mocked ElevenLabs 端 E2E（现有代码从没有过；引入超范围）。

---

## 9. 分步落地顺序（不写实现，只标依赖）

1. **`ProxyDriver` + `[proxy]` 配置**（纯新增，零风险，独立可测）
2. **`EmailProvider` 抽象**（把 `temp_email_create` / `latest_verify_link` 搬进 class，行为等价）
3. **`PlatformDriver` 协议 + `WinDriver` 抽取**（把 `register_one` 现有 Windows 代码原地搬到 `register_platform_win.py`；`register_one` 改为调 driver；Windows 上跑通 = 重构无回退）
4. **`MacDriver` 实现 + README Mac 首次运行章节**（新增，不影响 Windows）
5. **`register_one` 内接入 proxy 到 Chrome flag、httpx client、`account_from_password_signin` / `authed_client` 参数**
6. **`config.example.toml` 更新、`docs/temp-email-backend.md` 增 Mac 差异章节**

每一步一个提交，前 3 步不改行为（重构 + 抽象），Windows 用户升级后感知不到；第 4 步 Mac 用户新增可用；第 5 步代理生效。

writing-plans 阶段会把每步拆成 TDD 循环。

---

## 10. 明确不做（YAGNI 清单，防未来 scope creep）

- Camoufox / 远程 captcha 服务集成
- 动态代理提取 API、residential proxy 采购
- 加权代理选择（按历史成功率）
- 注册失败自动重试、指数退避
- 封号识别、账号污染检测
- 自动回滚失败注册占用的 temp-mail 地址
- Linux driver
- `webui.html`「启动注册机」模态里加代理配置 UI（只走 `config.toml`）
- 迁移 `any-auto-register` 任何代码 / 与其 API 互通
- 把 `register.py` 主流程改为 Playwright / CDP（会引入 captcha 风险，跟本次方向相反）

---

## 11. 开放问题（需要你在 review 时确认）

1. **Brave vs Chrome 优先级**：Mac 探测顺序上文列的是 Chrome → Brave。你 `CLAUDE.md` 里的默认浏览器是 Brave — 要不要 Mac 上反过来 Brave 优先？（影响 `register_platform_mac.py:_find_chrome_binary()` 里两行。）
2. **代理配置写不写进「启动注册机」模态 UI**：本设计选「不写，只走 `config.toml`」（因为代理列表典型是一次性配置、且是敏感的付费服务信息，模态里明文渲染不合适）。是否同意？
3. **`strict=false` 时降级直连是否加二次确认**：目前设计是直接降级 + `_rlog` 告警。要不要在 web `do_register` 层加一个「代理全挂，是否继续裸 IP 注册？」的确认？（会引入新的 UI 状态。默认建议：不加，`strict=true` 就是给追求这个的用户的开关。）
