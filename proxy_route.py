#!/usr/bin/env python3
"""macOS route-bypass: use an out-of-wall proxy while a global VPN (Shadowrocket)
is on, without changing the VPN.

A global VPN sets itself as the default route and fake-resolves DNS, so a normal
proxy connection gets swallowed. This routes ONLY the proxy's real IP via the
physical home gateway, so the tool reaches the proxy directly (bypassing the VPN
for that one IP); the VPN keeps handling everything else.

Enable with [proxy] route_bypass = true. Needs passwordless sudo for `route`
(one-time, see README). No-ops cleanly off macOS or when it can't set up.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def ip_url(proxy_url: str, ip: str) -> str:
    """Rebuild a proxy URL with `ip` swapped in for the hostname (auth/scheme/port
    preserved). Pure — the unit-testable core."""
    u = urlparse(proxy_url)
    userinfo = ""
    if u.username:
        userinfo = u.username + (f":{u.password}" if u.password is not None else "") + "@"
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{userinfo}{ip}{port}"


def real_gateway() -> str | None:
    """Physical LAN gateway (the real one behind the VPN's utun default route)."""
    for iface in ("en0", "en1", "en2", "en3"):
        try:
            gw = subprocess.run(["ipconfig", "getoption", iface, "router"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            gw = ""
        if _IP_RE.match(gw):
            return gw
    return None


def doh_resolve(host: str) -> list[str]:
    """Real A records via Cloudflare DoH — the VPN can't fake the JSON body,
    so this defeats fake-DNS. Routed through the VPN itself, which is fine."""
    req = urllib.request.Request(
        f"https://1.1.1.1/dns-query?name={host}&type=A",
        headers={"accept": "application/dns-json"})
    data = json.load(urllib.request.urlopen(req, timeout=15))
    return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]


def _route(action: str, ip: str, gw: str | None = None) -> bool:
    cmd = ["sudo", "-n", "route", "-n", action, "-host", ip]
    if gw:
        cmd.append(gw)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.returncode == 0


def prepare(proxy_url: str) -> tuple[str, str | None]:
    """Route the proxy host's real IP via the home gateway and return an
    IP-based proxy URL (bypasses fake-DNS) + the routed IP (pass to cleanup()).
    On failure returns the URL unchanged so the caller can still try."""
    if sys.platform != "darwin":
        return proxy_url, None
    host = urlparse(proxy_url).hostname or ""
    ip = host if _IP_RE.match(host) else (doh_resolve(host) or [None])[0]
    if not ip:
        return proxy_url, None
    gw = real_gateway()
    if not gw or not _route("add", ip, gw):
        return proxy_url, None
    return ip_url(proxy_url, ip), ip


def cleanup(ip: str | None) -> None:
    if ip:
        _route("delete", ip)


if __name__ == "__main__":  # `sudo python3 proxy_route.py <proxy_url>` to test
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    real, ip = prepare(url)
    print("gateway:", real_gateway(), "| routed ip:", ip, "| real url host swapped:", real != url)
    cleanup(ip)
