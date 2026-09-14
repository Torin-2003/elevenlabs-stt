#!/usr/bin/env python3
"""Camoufox registration strategy — the robust, cross-platform path.

Drives a stealthed Firefox (Camoufox) via Playwright selectors instead of
coordinate clicks: immune to layout shifts, DPI, and OS keyboard quirks, and
Camoufox's fingerprint earns hCaptcha's invisible pass (same approach the mature
any-auto-register / thedepegger tools use — libraries and method are public;
none of their code is copied). Shares the EmailProvider, ProxyDriver, and
account/credit plumbing with the other strategies.

First run needs the browser: `python -m camoufox fetch`.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse, parse_qs

import proxy as _proxy
import stt
from register import (EmailProvider, VERIFY_LINK_PATTERN, CloudflareTempEmail,
                      SIGNUP_URL)

EMAIL_SEL = 'input[name="email"]'
PASSWORD_SEL = 'input[name="password"]'
# Radix checkbox: the real target is the button[role=checkbox]; input[name=terms]
# is a hidden mirror.
TERMS_SEL = 'button[role="checkbox"]'


def _geoip_available() -> bool:
    """camoufox[geoip] present? (aligns browser timezone/locale to the proxy exit)."""
    try:
        from camoufox.geolocation import geoip_allowed
        geoip_allowed()
        return True
    except Exception:
        return False


def _camoufox_proxy(url: str | None) -> dict[str, str] | None:
    """Playwright/Camoufox proxy dict; supports user:pass (Chrome flags don't)."""
    if not url:
        return None
    u = urlparse(url)
    proxy: dict[str, str] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        proxy["username"] = u.username
    if u.password:
        proxy["password"] = u.password
    return proxy


class CamoufoxStrategy:
    """Real stealth browser + selector automation; ignores `captcha` unless a
    visible challenge appears (extension point, not wired yet)."""

    def __init__(self, headless: bool | None = None) -> None:
        self._headless = headless

    def register(self, *, provider: EmailProvider, proxy_driver,
                 captcha=None) -> dict[str, Any]:
        from camoufox.sync_api import Camoufox

        provider = provider or CloudflareTempEmail(stt.temp_email_config())
        rcfg = stt.register_config()
        tcfg = stt.temp_email_config()
        headless = self._headless if self._headless is not None else bool(rcfg.get("headless", False))

        picked = proxy_driver.pick()
        if picked is None and proxy_driver.has_proxies:
            stt._rlog("警告：所有代理已禁用，本次直连注册")
        # one sticky IP for this whole registration (browser + API calls)
        proxy_url = _proxy.with_session(picked.url) if picked else None

        # route-bypass: under a global VPN (Shadowrocket), route the proxy's real
        # IP via the physical gateway so it's reachable (see proxy_route).
        routed_ip = None
        if proxy_url and stt.proxy_config().get("route_bypass"):
            import proxy_route
            proxy_url, routed_ip = proxy_route.prepare(proxy_url)

        launch: dict[str, Any] = {"headless": headless}
        cam_proxy = _camoufox_proxy(proxy_url)
        if cam_proxy:
            launch["proxy"] = cam_proxy
            if _geoip_available():
                launch["geoip"] = True  # align timezone/locale to the proxy exit

        try:
            stt._rlog("创建临时邮箱...")
            addr = provider.create_address()
            email = addr.address
            stt._rlog(f"临时邮箱已创建: {email}")
            password = stt.random_password()

            stt._rlog("启动 Camoufox 并打开注册页...")
            with Camoufox(**launch) as browser:
                page = browser.new_page()
                # domcontentloaded (not networkidle — this SPA never idles); the
                # selector wait is the real readiness signal.
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector(EMAIL_SEL, timeout=30000)
                stt._rlog("填写注册表单...")
                page.fill(EMAIL_SEL, email)
                page.fill(PASSWORD_SEL, password)
                page.click(TERMS_SEL)
                page.get_by_role("button", name="Sign up", exact=True).click()

                stt._rlog(f"等待验证邮件（最长 {tcfg['poll_timeout_secs']}s）...")
                link = provider.poll_verification_link(
                    addr, VERIFY_LINK_PATTERN, tcfg["poll_timeout_secs"], tcfg["poll_interval_secs"])

            # Browser's job ends at submit (account created + email sent). Confirm
            # the email by applying the oobCode via Firebase REST — the ElevenLabs
            # SPA action route never reaches networkidle and is flaky to drive.
            stt._rlog("确认邮箱...")
            q = parse_qs(urlparse(link).query)
            oob = q.get("oobCode", [None])[0]
            internal = q.get("internalCode", [None])[0]
            if not oob:
                raise SystemExit(f"无法从验证链接解析 oobCode: {link[:120]}")
            stt.firebase_apply_oob(oob, proxy=proxy_url)  # Firebase emailVerified=true
            if internal:
                # clear ElevenLabs' internal verification block (else sign-in 400s)
                stt.elevenlabs_prepare_internal_verification(email, internal, proxy=proxy_url)

            stt._rlog("用新账号登录并拉取积分...")
            # Verification can take a beat to propagate to the sign-in blocking
            # function ("sign in once more"); retry a few times.
            account = None
            last_err: BaseException | None = None
            for attempt in range(4):
                try:
                    account = stt.account_from_password_signin(
                        email, password, temp_address=email, proxy=proxy_url)
                    break
                except SystemExit as e:
                    last_err = e
                    stt._rlog(f"登录未就绪，重试 ({attempt + 1}/4)...")
                    time.sleep(6)
            if account is None:
                raise last_err  # type: ignore[misc]
            with stt.authed_client(account, save=lambda _s: None, proxy=proxy_url) as client:
                client.get("/v1/user")
                stt.refresh_credits(account, client)
            stt._rlog(f"注册完成: {email}，剩余积分 {stt.cached_remaining(account)}")
            if picked:
                proxy_driver.mark_ok(picked)
            return account
        except (Exception, SystemExit):
            if picked:
                proxy_driver.mark_fail(picked)
            raise
        finally:
            if routed_ip:
                import proxy_route
                proxy_route.cleanup(routed_ip)
