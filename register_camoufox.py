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

import json
import os
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


def _attach_capture(page, path: str) -> None:
    """Append the signup/captcha request chain to a JSONL file — a protocol-spike
    aid, active only when EL_CAPTURE=<path> is set. Records non-GET requests (the
    signUp POST, Firebase/hCaptcha calls) plus hCaptcha GETs, skipping asset GETs.
    Best-effort: every handler is wrapped so capture never breaks a registration."""
    hosts = ("hcaptcha.com", "identitytoolkit", "securetoken",
             "elevenlabs.io", "api.us.elevenlabs", "recaptcha")

    def _rec(d: dict) -> None:
        try:
            with open(path, "a") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _on_request(req) -> None:
        try:
            url = req.url
            if not any(h in url for h in hosts):
                return
            if req.method == "GET" and "hcaptcha" not in url:
                return  # skip page-asset GETs; keep POSTs + hCaptcha calls
            body = None
            try:
                body = req.post_data
            except Exception:
                pass
            _rec({"t": "req", "method": req.method, "url": url, "post_data": body})
        except Exception:
            pass

    def _on_response(resp) -> None:
        try:
            if any(h in resp.url for h in hosts):
                _rec({"t": "resp", "status": resp.status, "url": resp.url})
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)


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


def _pick_unused_session(base_url: str, used_ips: set[str], build_url, fetch_ip,
                         tries: int = 3) -> tuple[str | None, str | None]:
    """Choose a sticky-session proxy URL whose exit IP isn't already used.

    Returns (proxy_url, exit_ip). Re-rolls the {session} token up to `tries`
    times for residential pools (only those can rotate to a fresh IP); returns
    the last attempt even if still used (caller warns). `exit_ip` is None when
    probing failed — dedup is then skipped (best-effort, never blocks a run)."""
    url = build_url(base_url)
    ip = fetch_ip(url)
    can_reroll = "{session}" in (base_url or "")
    n = 1
    while can_reroll and ip is not None and ip in used_ips and n < tries:
        url = build_url(base_url)
        ip = fetch_ip(url)
        n += 1
    return url, ip


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

        # Build a concrete sticky-session proxy URL, applying route-bypass so the
        # proxy is reachable under a global VPN (Shadowrocket). The route is
        # set-once per host (idempotent, torn down at process exit — see
        # proxy_route), so a whole batch only sudo's once.
        route_on = bool(stt.proxy_config().get("route_bypass"))

        def _build_url(base: str) -> str:
            url = _proxy.with_session(base)
            if url and route_on:
                import proxy_route
                url, _ = proxy_route.prepare(url)
            return url

        # one sticky IP for this whole registration (browser + API calls);
        # dedup: prefer a session whose exit IP hasn't registered before
        # (residential {session} pools re-roll for a fresh IP).
        proxy_url: str | None = None
        exit_ip: str | None = None
        if picked:
            used_ips = stt.used_exit_ips()
            proxy_url, exit_ip = _pick_unused_session(
                picked.url, used_ips, _build_url, stt.exit_ip)
            if exit_ip and exit_ip in used_ips:
                stt._rlog(f"警告：出口 IP {exit_ip} 已用过，继续注册")
            elif exit_ip:
                stt._rlog(f"代理出口 IP: {exit_ip}")

        launch: dict[str, Any] = {"headless": headless}
        cam_proxy = _camoufox_proxy(proxy_url)
        if cam_proxy:
            launch["proxy"] = cam_proxy
            if _geoip_available():
                launch["geoip"] = True  # align timezone/locale to the proxy exit

        # captcha solver addon(s) for when the invisible pass fails (visible
        # hCaptcha challenge) — e.g. ["nopecha"]. Loaded into the Firefox profile.
        addon_names = rcfg.get("captcha_addons") or []
        if addon_names:
            try:
                import captcha_addon
                launch["addons"] = captcha_addon.resolve_addons(addon_names)
                stt._rlog(f"已加载验证码解题插件: {', '.join(addon_names)}")
            except Exception as e:  # never block a run on addon setup
                stt._rlog(f"验证码插件加载失败（继续，不用插件）: {e}")

        try:
            stt._rlog("创建临时邮箱...")
            addr = provider.create_address()
            email = addr.address
            stt._rlog(f"临时邮箱已创建: {email}")
            password = stt.random_password()

            stt._rlog("启动 Camoufox 并打开注册页...")
            with Camoufox(**launch) as browser:
                page = browser.new_page()
                _cap_path = os.environ.get("EL_CAPTURE")
                if _cap_path:
                    _attach_capture(page, _cap_path)
                    stt._rlog(f"网络抓包已开启 -> {_cap_path}")
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
            if exit_ip:
                account["exit_ip"] = exit_ip  # recorded for used-IP dedup
            stt._rlog(f"注册完成: {email}，剩余积分 {stt.cached_remaining(account)}")
            if picked:
                proxy_driver.mark_ok(picked)
            return account
        except (Exception, SystemExit):
            if picked:
                proxy_driver.mark_fail(picked)
            raise
