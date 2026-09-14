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

from typing import Any
from urllib.parse import urlparse

import stt
from register import (EmailProvider, VERIFY_LINK_PATTERN, CloudflareTempEmail,
                      SIGNUP_URL)

EMAIL_SEL = 'input[name="email"]'
PASSWORD_SEL = 'input[name="password"]'
# Radix checkbox: the real target is the button[role=checkbox]; input[name=terms]
# is a hidden mirror.
TERMS_SEL = 'button[role="checkbox"]'


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
        proxy_url = picked.url if picked else None

        launch: dict[str, Any] = {"headless": headless}
        cam_proxy = _camoufox_proxy(proxy_url)
        if cam_proxy:
            launch["proxy"] = cam_proxy
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
                page.goto(SIGNUP_URL, wait_until="networkidle", timeout=60000)
                page.wait_for_selector(EMAIL_SEL, timeout=30000)
                stt._rlog("填写注册表单...")
                page.fill(EMAIL_SEL, email)
                page.fill(PASSWORD_SEL, password)
                page.click(TERMS_SEL)
                page.get_by_role("button", name="Sign up", exact=True).click()

                stt._rlog(f"等待验证邮件（最长 {tcfg['poll_timeout_secs']}s）...")
                link = provider.poll_verification_link(
                    addr, VERIFY_LINK_PATTERN, tcfg["poll_timeout_secs"], tcfg["poll_interval_secs"])
                stt._rlog("打开验证链接确认邮箱...")
                page.goto(link, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(4000)  # let the confirmation settle

            stt._rlog("用新账号登录并拉取积分...")
            account = stt.account_from_password_signin(
                email, password, temp_address=email, proxy=proxy_url)
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
