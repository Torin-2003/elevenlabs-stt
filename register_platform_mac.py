#!/usr/bin/env python3
"""macOS PlatformDriver: real Chrome/Brave via subprocess + AppleScript window
lookup and foreground.

Requires the terminal (Terminal / iTerm) to have Accessibility permission
(System Settings → Privacy & Security → Accessibility) — otherwise pyautogui's
keyboard/mouse events are silently dropped and the sign-up form never fills.
"""
from __future__ import annotations

import collections
import os
import pathlib
import shutil
import subprocess
import time
from typing import Callable

# Chrome first (broadest install base), then Brave. The registration profile is
# a fresh throwaway --user-data-dir, so it never reuses the daily browser
# session; brand only affects the AppleScript `tell` target.
_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

MacWindow = collections.namedtuple("MacWindow", "left top width height app")


def _find_chrome_binary(env: dict | None = None,
                        exists: Callable[[str], bool] = os.path.exists) -> str:
    env = env if env is not None else os.environ
    override = env.get("ELEVENLABS_STT_CHROME")
    if override:
        return override
    for path in _CANDIDATES:
        if exists(path):
            return path
    raise SystemExit(
        "register 需要 Chrome 或 Brave，未在标准路径找到；"
        "设 $ELEVENLABS_STT_CHROME 指向浏览器可执行文件")


def _osascript(script: str) -> str:
    out = subprocess.run(["osascript", "-e", script],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise SystemExit(f"osascript failed: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


class MacDriver:
    mod_key = "command"  # clipboard / select-all / address-bar modifier on macOS

    def __init__(self) -> None:
        self._binary = _find_chrome_binary()
        self._app = "Brave Browser" if "Brave" in self._binary else "Google Chrome"
        self._pid: int | None = None

    # Deterministic launch geometry: a fixed single-display window is stabler
    # than maximizing (Finder's desktop bounds are the multi-monitor union, which
    # would span displays and skew fraction-based coordinates).
    WINDOW_POSITION = (40, 40)
    WINDOW_SIZE = (1280, 860)

    def launch_chrome(self, profile_dir, signup_url, proxy_url):
        args = [self._binary, f"--user-data-dir={profile_dir}", "--no-first-run",
                "--new-window",
                f"--window-position={self.WINDOW_POSITION[0]},{self.WINDOW_POSITION[1]}",
                f"--window-size={self.WINDOW_SIZE[0]},{self.WINDOW_SIZE[1]}",
                "--disable-save-password-bubble"]
        if proxy_url:
            args.append(f"--proxy-server={proxy_url}")
        args.append(signup_url)
        # Launch the binary directly (not via `open`), so popen.pid IS the new
        # browser instance's main process — the key to addressing THIS window by
        # PID even when a daily browser of the same brand is already running.
        popen = subprocess.Popen(args)
        self._pid = popen.pid
        return popen

    def find_profile_window(self, profile_dir, timeout_s):
        # Read the window geometry via Quartz CGWindowList, matching our exact
        # instance by owner PID. This is read-only and reliable, unlike System
        # Events AX (which intermittently returned empty window lists via the
        # `whose unix id is` filter and -10006 on `set position`). Bounds are in
        # the global display point space — the same space pyautogui clicks in.
        # The window stays at Chrome's default geometry (a fresh profile is
        # consistent run-to-run); clicks are computed as fractions of it.
        from Quartz import (CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly,
                            kCGNullWindowID)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.5)
            infos = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
            for w in infos:
                if w.get("kCGWindowOwnerPID") != self._pid:
                    continue
                if w.get("kCGWindowLayer", 0) != 0:  # 0 = normal window (skip menus/panels)
                    continue
                b = w.get("kCGWindowBounds") or {}
                width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
                if width >= 400 and height >= 300:
                    return MacWindow(left=int(b.get("X", 0)), top=int(b.get("Y", 0)),
                                     width=width, height=height, app=self._app)
        raise SystemExit("auto-register 未找到临时浏览器窗口（检查浏览器是否正常启动）；aborting")

    def ensure_foreground(self, window) -> None:
        # Activate OUR instance by PID via AppKit (reliable; not `tell application
        # by name`, which targets the daily instance). No Windows-style
        # thread-attach dance needed on macOS.
        if self._pid is None:
            return
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(self._pid)
        if app is not None:
            app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
        time.sleep(0.2)

    def kill_profile(self, profile_dir, popen) -> None:
        if popen is not None:
            popen.terminate()
            try:
                popen.wait(timeout=3)
            except subprocess.TimeoutExpired:
                popen.kill()
        # Backup for any lingering helper processes: match by the unique profile
        # dir NAME, not the full path — macOS resolves /var → /private/var in the
        # process command line, so a full-path pattern silently misses.
        subprocess.run(["pkill", "-f", pathlib.Path(profile_dir).name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)
