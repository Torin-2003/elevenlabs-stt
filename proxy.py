#!/usr/bin/env python3
"""Minimal proxy pool for the registration flow.

Static list + round-robin + auto-disable after N consecutive failures.
Empty list == direct connection (the pre-proxy behavior). No dynamic
extraction, no weighting — see the design doc's YAGNI list. Use residential
/ rotating IPs: ElevenLabs and hCaptcha flag datacenter ASNs.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any


@dataclasses.dataclass
class Proxy:
    url: str                     # full URL, passed verbatim to Chrome --proxy-server and httpx
    fails: int = 0
    disabled_until: float = 0.0  # unix ts; > now means temporarily out of the pool


class ProxyDriver:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self._proxies = [Proxy(url=u) for u in (cfg.get("proxies") or [])]
        self._threshold = int(cfg.get("fail_threshold", 3))
        self._cooldown = float(cfg.get("cooldown_secs", 900))
        self._strict = bool(cfg.get("strict", False))
        self._cursor = 0

    @property
    def has_proxies(self) -> bool:
        """True if any proxies are configured (regardless of disabled state).

        Lets a caller tell 'no proxy configured → direct' apart from 'all
        proxies disabled → falling back to direct', so it can warn on the latter.
        """
        return bool(self._proxies)

    def _live(self) -> list[Proxy]:
        now = time.time()
        return [p for p in self._proxies if p.disabled_until <= now]

    def pick(self) -> Proxy | None:
        """Next live proxy round-robin, or None for a direct connection.

        None means: no proxies configured, or all disabled while strict=False.
        strict=True with all disabled raises rather than silently going direct.
        """
        if not self._proxies:
            return None
        live = self._live()
        if not live:
            if self._strict:
                raise SystemExit("proxy pool exhausted; strict mode")
            return None
        p = live[self._cursor % len(live)]
        self._cursor += 1
        return p

    def mark_ok(self, p: Proxy) -> None:
        p.fails = 0
        p.disabled_until = 0.0

    def mark_fail(self, p: Proxy) -> None:
        p.fails += 1
        if p.fails >= self._threshold:
            p.disabled_until = time.time() + self._cooldown
