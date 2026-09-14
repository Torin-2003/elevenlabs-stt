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
import sys
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

    def _headers(self, extra: dict[str, str]) -> dict[str, str]:
        # A private site (worker PASSWORDS set) gates every path except /open_api
        # and /telegram behind x-custom-auth — including /admin/* and the mail
        # poll — so attach it to every request when site_password is configured.
        h = dict(extra)
        site = self._cfg.get("site_password")
        if site:
            h["x-custom-auth"] = site
        return h

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
                                headers=self._headers({"x-admin-auth": cfg["admin_password"]}))
                if r.status_code < 400:
                    return self._to_address(r.json())
                if r.status_code not in (401, 403):
                    raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
            r = client.post(f"{base}/api/new_address", json=body, headers=self._headers({}))
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
        headers = self._headers({"Authorization": f"Bearer {addr.token}"})
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


SIGNUP_URL = "https://elevenlabs.io/app/sign-up"


class PlatformDriver(Protocol):
    """OS-level management of the temporary real-Chrome window (used only by
    UICoordinateStrategy). `mod_key` is the clipboard/select-all/address-bar
    modifier — 'ctrl' on Windows, 'command' on macOS."""
    mod_key: str

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
        try:
            from register_platform_win import WinDriver
        except ImportError:
            raise SystemExit("auto-register needs pyautogui pyperclip pygetwindow")
        return WinDriver()
    raise SystemExit(f"register 目前只支持 macOS 与 Windows；当前平台: {sys.platform}")


class UICoordinateStrategy:
    """Real Chrome + coordinate automation. Gets hCaptcha's invisible pass from
    an authentic fingerprint + real OS input; ignores `captcha` (none needed)."""

    def __init__(self, platform: PlatformDriver | None = None) -> None:
        self._platform = platform  # None → resolved per sys.platform at register() time

    def register(self, *, provider: EmailProvider, proxy_driver: proxy.ProxyDriver,
                 captcha: "CaptchaSolver | None" = None) -> dict[str, Any]:
        platform = self._platform or _default_platform_driver()
        if getattr(platform, "mod_key", None) == "command":
            stt._rlog("提示：macOS 首次运行需在 系统设置→隐私与安全性→辅助功能 给终端授权，"
                      "否则键鼠自动化会被静默丢弃、注册会卡在填表这一步")
        picked = proxy_driver.pick()
        if picked is None and proxy_driver.has_proxies:
            stt._rlog("警告：所有代理已禁用，本次直连注册")
        # one sticky IP for this whole registration (see proxy.with_session)
        proxy_url = proxy.with_session(picked.url) if picked else None
        profile_dir = pathlib.Path(tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-"))
        _write_no_password_prefs(profile_dir)
        popen = None
        try:
            stt._rlog("创建临时邮箱...")
            addr = provider.create_address()
            email = addr.address
            stt._rlog(f"临时邮箱已创建: {email}")
            password = stt.random_password()
            stt._rlog("启动临时 Chrome...")
            popen = platform.launch_chrome(profile_dir, SIGNUP_URL, proxy_url)
            stt._rlog("等待临时 Chrome 窗口出现（最长 30s）...")
            window = platform.find_profile_window(profile_dir, 30)
            # Foreground the temp Chrome immediately, before any page-load waits,
            # so the key/click sequence lands in sync.
            stt._rlog("窗口已找到，置顶并等待页面渲染...")
            platform.ensure_foreground(window)
            time.sleep(4)
            # Chrome already opened /app/sign-up from its command line; just wait
            # for the app to render instead of re-navigating (visible reload).
            time.sleep(12)
            stt._rlog("填写注册表单...")
            _fill_signup_form(window, platform, email, password)
            tcfg = stt.temp_email_config()
            stt._rlog(f"等待验证邮件（最长 {tcfg['poll_timeout_secs']}s）...")
            link = provider.poll_verification_link(addr, VERIFY_LINK_PATTERN,
                                                   tcfg["poll_timeout_secs"], tcfg["poll_interval_secs"])
            stt._rlog("打开验证链接并确认...")
            _open_verify_link_and_confirm(window, platform, link)
            stt._rlog("用新账号登录...")
            _sign_in(window, platform, email, password)
            stt._rlog("拉取账号积分...")
            account = stt.account_from_password_signin(email, password,
                                                       temp_address=email, proxy=proxy_url)
            with stt.authed_client(account, save=lambda _s: None, proxy=proxy_url) as client:
                client.get("/v1/user")
                stt.refresh_credits(account, client)
            stt._rlog(f"注册完成: {email}，剩余积分 {stt.cached_remaining(account)}")
            if picked:
                proxy_driver.mark_ok(picked)
            return account
        except (Exception, SystemExit):
            # any registration failure penalizes the proxy; KeyboardInterrupt /
            # GeneratorExit propagate without marking a healthy proxy failed.
            if picked:
                proxy_driver.mark_fail(picked)
            raise
        finally:
            platform.kill_profile(profile_dir, popen)


class HTTPProtocolStrategy:
    def register(self, **_: Any) -> dict[str, Any]:
        raise NotImplementedError(
            "HTTP 协议策略尚未实现。需要 curl_cffi + Firebase 流程 + 付费 CaptchaSolver；"
            "见 docs/superpowers/specs/2026-09-14-register-mac-and-proxy-design.md。"
            "当前请用 [register] strategy='ui'。")


def _make_camoufox():
    # Lazy import: register_camoufox imports this module, so defer to call time
    # (and keep camoufox/playwright optional for users on the ui strategy).
    from register_camoufox import CamoufoxStrategy
    return CamoufoxStrategy()


_STRATEGIES = {
    "camoufox": _make_camoufox,   # recommended: stealth Firefox + selectors
    "cdp": _make_camoufox,        # alias — Camoufox is our stealth-CDP realization
    "ui": UICoordinateStrategy,   # legacy: real Chrome + coordinates (fragile)
    "http": HTTPProtocolStrategy,
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


# --- register (UI coordinate helpers) ----------------------------------
# These run on both Windows and macOS: OS window management lives in the
# PlatformDriver; here we only send keys/clicks via pyautogui/pyperclip, using
# platform.mod_key for the clipboard / select-all / address-bar shortcuts.

def _write_no_password_prefs(profile_dir: pathlib.Path) -> None:
    # fresh profile per account avoids logged-in Chrome redirecting sign-up to
    # onboarding; disabling the password manager avoids the save-password bubble.
    prefs_path = profile_dir / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False},
    }), encoding="utf-8")


class _Input:
    """Keyboard/mouse against the temp window, re-focusing before every action.
    pyautogui/pyperclip are imported lazily so `import register` needs no GUI deps."""

    def __init__(self, window: Any, platform: PlatformDriver) -> None:
        try:
            import pyautogui, pyperclip
        except ImportError:
            raise SystemExit("auto-register needs pyautogui pyperclip")
        self._pg = pyautogui
        self._pc = pyperclip
        self._window = window
        self._platform = platform
        self._mod = platform.mod_key

    def _fg(self) -> None:
        self._platform.ensure_foreground(self._window)

    def hotkey(self, *keys: str) -> None:
        self._fg()
        self._pg.hotkey(*keys)

    def press(self, key: str) -> None:
        self._fg()
        self._pg.press(key)

    def paste(self, text: str) -> None:
        self._fg()
        self._pc.copy(text)
        self._pg.hotkey(self._mod, "v")

    def select_all(self) -> None:
        self.hotkey(self._mod, "a")

    def focus_address_bar(self) -> None:
        self.hotkey(self._mod, "l")

    def click_frac(self, x_frac: float, y_frac: float) -> None:
        self._fg()
        w = self._window
        x = w.left + int(w.width * x_frac)
        y = w.top + int(w.height * y_frac)
        if x < 0 or y < 0:
            # A minimized window reports -32000 geometry; pyautogui clamps the
            # click to (0,0), which hits Chrome's tab-search chevron.
            raise SystemExit("auto-register got bad temp Chrome window geometry; aborting")
        self._pg.click(x, y)
        time.sleep(0.1)


def _fill_signup_form(window: Any, platform: PlatformDriver, email: str, password: str) -> None:
    io = _Input(window, platform)
    io.click_frac(0.50, 0.56)  # signup email
    io.select_all(); io.paste(email)
    io.press("tab"); io.paste(password)
    io.press("enter")


def _open_verify_link_and_confirm(window: Any, platform: PlatformDriver, link: str) -> None:
    io = _Input(window, platform)
    io.focus_address_bar()
    io.paste(link)
    io.press("enter")
    time.sleep(15)
    io.press("enter")  # modal Continue if focused
    io.click_frac(0.50, 0.62)
    io.click_frac(0.65, 0.62)  # verification modal Continue fallback
    time.sleep(8)


def _sign_in(window: Any, platform: PlatformDriver, email: str, password: str) -> None:
    io = _Input(window, platform)
    io.click_frac(0.50, 0.62)  # sign-in email
    io.select_all(); io.paste(email)
    io.press("tab"); io.paste(password)
    io.press("enter")
    time.sleep(15)
