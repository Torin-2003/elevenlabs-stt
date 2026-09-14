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

import atexit
import json
import re
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Set-once caches. A residential pool has ONE fixed host (e.g. gate.decodo.com),
# so a whole batch needs a single route; datacenter pools share a host too.
# _HOST_IP pins host -> chosen IP: proxy hosts often have several A-records and
# DoH returns them in rotating order, so without pinning each prepare() would
# pick a different IP and add another route. _ROUTED maps the pinned IP ->
# gateway. prepare() is idempotent; cleanup_all() (atexit) tears down at exit.
_HOST_IP: dict[str, str] = {}
_ROUTED: dict[str, str] = {}
_ATEXIT_ARMED = False


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
    IP-based proxy URL (bypasses fake-DNS) + the routed IP.

    Idempotent (set-once): if the IP is already routed this process, reuse it
    without another `route add` (so a batch only sudo's once per host); the route
    is torn down by cleanup_all() at process exit. On failure returns the URL
    unchanged so the caller can still try."""
    if sys.platform != "darwin":
        return proxy_url, None
    host = urlparse(proxy_url).hostname or ""
    if _IP_RE.match(host):
        ip = host
    elif host in _HOST_IP:  # pinned this process — same IP every call for this host
        ip = _HOST_IP[host]
    else:
        answers = doh_resolve(host)
        ip = sorted(answers)[0] if answers else None  # deterministic pick
        if ip:
            _HOST_IP[host] = ip
    if not ip:
        return proxy_url, None
    if ip in _ROUTED:  # already routed this process — no repeat sudo
        return ip_url(proxy_url, ip), ip
    gw = real_gateway()
    if not gw or not _route("add", ip, gw):
        return proxy_url, None
    _ROUTED[ip] = gw
    _arm_atexit()
    return ip_url(proxy_url, ip), ip


def cleanup(ip: str | None) -> None:
    """Delete a single host route (manual/legacy). Also drops it from the cache."""
    if ip:
        _route("delete", ip)
        _ROUTED.pop(ip, None)


def cleanup_all() -> None:
    """Tear down every route this process added (registered via atexit)."""
    for ip in list(_ROUTED):
        _route("delete", ip)
        _ROUTED.pop(ip, None)
    _HOST_IP.clear()  # re-resolve fresh next batch


def _arm_atexit() -> None:
    global _ATEXIT_ARMED
    if not _ATEXIT_ARMED:
        atexit.register(cleanup_all)
        _ATEXIT_ARMED = True


if __name__ == "__main__":  # `sudo python3 proxy_route.py <proxy_url>` to test
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    real, ip = prepare(url)
    print("gateway:", real_gateway(), "| routed ip:", ip, "| real url host swapped:", real != url)
    cleanup(ip)
