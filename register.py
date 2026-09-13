#!/usr/bin/env python3
"""Account auto-registration for elevenlabs-stt.

temp-mail (cloudflare_temp_email) + real Chrome UI automation (pyautogui) —
the Windows-only, most platform-specific corner of the tool, kept out of
stt.py's API/packing/pipeline core. stt.py imports this lazily at its call
sites (refill_pool / run_plan_pipelined / cmd_pool_warm) to avoid an import
cycle; this module only touches stt.* at call time.
"""
from __future__ import annotations

import dataclasses
import html
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any, Protocol

import httpx

import proxy
import stt


# --- temp-email --------------------------------------------------------

VERIFY_LINK_PATTERN = re.compile(
    r"https://elevenlabs\.io/app/action\?[^\s\"<>]+oobCode=[^\s\"<>]+")


@dataclasses.dataclass
class EmailAddress:
    address: str
    token: str              # bearer token for polling this mailbox (cloudflare_temp_email jwt)
    raw: dict[str, Any]


class EmailProvider(Protocol):
    def create_address(self) -> EmailAddress: ...
    def poll_verification_link(self, addr: EmailAddress, pattern: "re.Pattern[str]",
                               timeout_s: float, interval_s: float) -> str: ...


class CloudflareTempEmail:
    """cloudflare_temp_email backend; admin path first, user path fallback.

    create_address() rotates over the configured domains[] (round-robin) so a
    batch of registrations spreads across domains — ElevenLabs rejects some
    disposable domains, and spreading avoids putting every account on a bad one.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg
        self._domains = list(cfg.get("domains") or ([cfg["domain"]] if cfg.get("domain") else []))
        self._cursor = 0

    def _next_domain(self) -> str:
        if not self._domains:
            raise SystemExit("temp_email.domain / temp_email.domains are required")
        domain = self._domains[self._cursor % len(self._domains)]
        self._cursor += 1
        return domain

    def create_address(self, name: str | None = None) -> EmailAddress:
        cfg = self._cfg
        if not cfg["base_url"]:
            raise SystemExit("temp_email.base_url is required")
        base = str(cfg["base_url"]).rstrip("/")
        # cloudflare_temp_email v1.9 requires name even on the admin API.
        local = name or ("el" + secrets.token_hex(5))
        body = {"name": local, "domain": self._next_domain(), "cf_token": "",
                "enableRandomSubdomain": False}
        with httpx.Client(timeout=30) as client:
            if cfg.get("use_admin_path", True) and cfg.get("admin_password"):
                r = client.post(f"{base}/admin/new_address", json=body,
                                headers={"x-admin-auth": cfg["admin_password"]})
                if r.status_code < 400:
                    return self._to_address(r.json())
                if r.status_code not in (401, 403):
                    raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
            headers = {}
            if cfg.get("site_password"):
                headers["x-custom-auth"] = cfg["site_password"]
            r = client.post(f"{base}/api/new_address", json=body, headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
            return self._to_address(r.json())

    @staticmethod
    def _to_address(data: dict[str, Any]) -> EmailAddress:
        return EmailAddress(address=data["address"], token=data["jwt"], raw=data)

    def poll_verification_link(self, addr: EmailAddress, pattern: "re.Pattern[str]",
                               timeout_s: float, interval_s: float) -> str:
        """Return newest ElevenLabs verification link from the temp mailbox."""
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


# --- strategy ----------------------------------------------------------
# Pluggable registration strategies. UICoordinateStrategy (real Chrome) is the
# only one implemented; HTTP/CDP are extension-point stubs — adding one is a new
# class here plus a CaptchaSolver, without touching the dispatcher or the shared
# services (EmailProvider / ProxyDriver / CaptchaSolver).

class CaptchaSolver(Protocol):
    def solve_hcaptcha(self, sitekey: str, page_url: str) -> str: ...


class RegisterStrategy(Protocol):
    def register(self, *, provider: EmailProvider, proxy_driver: proxy.ProxyDriver,
                 captcha: "CaptchaSolver | None" = None) -> dict[str, Any]: ...


class UICoordinateStrategy:
    """Real Chrome + coordinate automation. Gets hCaptcha's invisible pass from
    an authentic fingerprint + real OS input; ignores `captcha` (none needed)."""

    def register(self, *, provider: EmailProvider, proxy_driver: proxy.ProxyDriver,
                 captcha: "CaptchaSolver | None" = None) -> dict[str, Any]:
        return _ui_register(provider=provider)


class HTTPProtocolStrategy:
    def register(self, **_: Any) -> dict[str, Any]:
        raise NotImplementedError(
            "HTTP 协议策略尚未实现。需要 curl_cffi + Firebase 流程 + 付费 CaptchaSolver；"
            "见 docs/superpowers/specs/2026-09-14-register-mac-and-proxy-design.md。"
            "当前请用 [register] strategy='ui'。")


class StealthCDPStrategy:
    def register(self, **_: Any) -> dict[str, Any]:
        raise NotImplementedError(
            "隐身 CDP 策略尚未实现。需引入 nodriver/Patchright/Camoufox。"
            "当前请用 [register] strategy='ui'。")


_STRATEGIES = {
    "ui": UICoordinateStrategy,
    "http": HTTPProtocolStrategy,
    "cdp": StealthCDPStrategy,
}


def register_one(*, strategy: RegisterStrategy | None = None,
                 provider: EmailProvider | None = None,
                 proxy_driver: proxy.ProxyDriver | None = None,
                 captcha: "CaptchaSolver | None" = None) -> dict[str, Any]:
    """Register one ElevenLabs account via the configured strategy.

    Bare `register_one()` selects [register].strategy (default 'ui') and injects
    the shared services — backward-compatible with the old no-arg call sites.
    """
    if strategy is None:
        name = stt.register_config()["strategy"]
        factory = _STRATEGIES.get(name)
        if factory is None:
            raise SystemExit(f"未知 register.strategy: {name!r}；可选 {list(_STRATEGIES)}")
        strategy = factory()
    if provider is None:
        provider = CloudflareTempEmail(stt.temp_email_config())
    if proxy_driver is None:
        proxy_driver = proxy.ProxyDriver(stt.proxy_config())
    return strategy.register(provider=provider, proxy_driver=proxy_driver, captcha=captcha)


# --- register (UI coordinate orchestration) ----------------------------

def _ui_register(*, provider: EmailProvider | None = None) -> dict[str, Any]:
    """Create one ElevenLabs account via temp-mail + real Chrome, then return account."""
    try:
        import pyautogui, pyperclip, pygetwindow as gw
    except ImportError:
        raise SystemExit("auto-register needs pyautogui pyperclip pygetwindow")

    provider = provider or CloudflareTempEmail(stt.temp_email_config())
    stt._rlog("创建临时邮箱...")
    addr = provider.create_address()
    email = addr.address
    stt._rlog(f"临时邮箱已创建: {email}")
    password = stt.random_password()
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    profile_dir = tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-")
    prefs_path = pathlib.Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False},
    }), encoding="utf-8")

    # ponytail: fresh profile per account avoids logged-in Chrome redirecting sign-up to onboarding.
    # Coordinates are ugly, but selector automation triggers hCaptcha; real Chrome doesn't.
    def profile_window_handles() -> set[int]:
        if not shutil.which("powershell"):
            return set()
        profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
        ps = (
            f"$profile = '{profile_name}'; "
            "$pids = @(Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
            "Select-Object -ExpandProperty ProcessId); "
            "if ($pids.Count -gt 0) { "
            "Get-Process -Id $pids -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowHandle -ne 0 } | "
            "ForEach-Object { $_.MainWindowHandle } "
            "}"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=3)
        except Exception:
            return set()
        handles: set[int] = set()
        for line in out.stdout.splitlines():
            try:
                handles.add(int(line.strip()))
            except ValueError:
                pass
        return handles

    chrome_startup_kwargs = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL: do not inherit a hidden Web UI process state.
        chrome_startup_kwargs = {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    proc = None
    try:
        stt._rlog("启动临时 Chrome...")
        proc = subprocess.Popen([
            chrome,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--new-window",
            "--window-position=40,40",
            "--disable-save-password-bubble",
            "--do-not-de-elevate",
            "https://elevenlabs.io/app/sign-up",
        ], **chrome_startup_kwargs)
        new_window = None
        stt._rlog("等待临时 Chrome 窗口出现（最长 30s）...")
        # 30s: a brand-new profile cold-starts slowly (profile init + AV scan); 10s
        # missed the window on busy machines and the finally-block killed late Chrome.
        deadline = time.time() + 30
        next_profile_probe = 0.0
        while time.time() < deadline and new_window is None:
            time.sleep(0.5)
            profile_handles = set()
            if time.time() >= next_profile_probe:
                profile_handles = profile_window_handles()
                next_profile_probe = time.time() + 1.0
            for w in gw.getAllWindows():
                hwnd = getattr(w, "_hWnd", None)
                if hwnd in profile_handles:
                    new_window = w
                    break
        if new_window is None:
            raise SystemExit("auto-register could not find the new temporary Chrome window; aborting")

        def window_op(name: str) -> None:
            try:
                getattr(new_window, name)()
            except Exception as e:
                # pygetwindow/pywin32 can report Windows error code 0 ("success")
                # after the window operation actually completed. Treat only that
                # wrapper bug as non-fatal; real focus/window errors should abort.
                if "Error code from Windows: 0" not in str(e):
                    raise

        def ensure_window_foreground() -> None:
            hwnd = getattr(new_window, "_hWnd", None)
            if not hwnd or os.name != "nt":
                window_op("activate")
                return
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # Fast path: already foreground. Forcing anyway is what caused the
            # constant restore/maximize flicker between every automation action.
            if user32.GetForegroundWindow() == hwnd:
                return
            SW_RESTORE = 9
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            for attempt in range(8):
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                # AttachThreadInput to the current foreground thread satisfies
                # Windows' foreground-lock rules. A synthetic Alt tap also works
                # but toggles Chrome's menu-accelerator mode, breaking in-window
                # keyboard focus for the very keys we send next.
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                cur_tid = kernel32.GetCurrentThreadId()
                attached = fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    if attempt >= 4:  # last resort: topmost toggle
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                finally:
                    if attached:
                        user32.AttachThreadInput(cur_tid, fg_tid, False)
                time.sleep(0.2)
                if user32.GetForegroundWindow() == hwnd:
                    return
            raise SystemExit("auto-register could not focus the temporary Chrome window; aborting before sending keys")

        # Foreground the temp Chrome immediately, before any page-load waits.
        # pygetwindow's activate() is silently denied when we are a background
        # process, which left the window behind for ~16s until the first click
        # forced it forward and the key/click sequence landed out of sync.
        stt._rlog("窗口已找到，置顶并等待页面渲染...")
        window_op("restore")
        window_op("maximize")
        ensure_window_foreground()
        time.sleep(4)

        def hotkey(*keys: str) -> None:
            ensure_window_foreground()
            pyautogui.hotkey(*keys)

        def press(key: str) -> None:
            ensure_window_foreground()
            pyautogui.press(key)

        def click_frac(x_frac: float, y_frac: float) -> None:
            ensure_window_foreground()
            x = new_window.left + int(new_window.width * x_frac)
            y = new_window.top + int(new_window.height * y_frac)
            if x < 0 or y < 0:
                # A minimized window reports -32000 geometry; pyautogui clamps the
                # click to (0,0), which hits Chrome's tab-search chevron.
                raise SystemExit("auto-register got bad temp Chrome window geometry; aborting")
            pyautogui.click(x, y)
            time.sleep(0.1)

        def paste(text: str) -> None:
            ensure_window_foreground()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")

        # Chrome already opened /app/sign-up from its command line; just wait
        # for the app to render instead of re-navigating (visible reload).
        time.sleep(12)

        stt._rlog("填写注册表单...")
        click_frac(0.50, 0.56)  # signup email
        hotkey("ctrl", "a"); paste(email)
        press("tab"); paste(password)
        press("enter")

        _tcfg = stt.temp_email_config()
        stt._rlog(f"等待验证邮件（最长 {_tcfg['poll_timeout_secs']}s）...")
        link = provider.poll_verification_link(addr, VERIFY_LINK_PATTERN,
                                               _tcfg["poll_timeout_secs"], _tcfg["poll_interval_secs"])
        stt._rlog("打开验证链接并确认...")
        hotkey("ctrl", "l")
        paste(link)
        press("enter")
        time.sleep(15)
        press("enter")  # modal Continue if focused
        click_frac(0.50, 0.62)
        click_frac(0.65, 0.62)  # verification modal Continue fallback
        time.sleep(8)
        stt._rlog("用新账号登录...")
        click_frac(0.50, 0.62)  # sign-in email
        hotkey("ctrl", "a"); paste(email)
        press("tab"); paste(password)
        press("enter")
        time.sleep(15)

        stt._rlog("拉取账号积分...")
        account = stt.account_from_password_signin(email, password, temp_address=email)
        with stt.authed_client(account, save=lambda _s: None) as client:
            client.get("/v1/user")
            stt.refresh_credits(account, client)
        stt._rlog(f"注册完成: {email}，剩余积分 {stt.cached_remaining(account)}")
        return account
    finally:
        if shutil.which("powershell"):
            profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
            subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"$profile = '{profile_name}'; "
                "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif proc is not None:
            proc.terminate()
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)
