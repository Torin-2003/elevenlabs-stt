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

    def launch_chrome(self, profile_dir, signup_url, proxy_url):
        args = [self._binary, f"--user-data-dir={profile_dir}", "--no-first-run",
                "--new-window", "--window-position=40,40",
                "--disable-save-password-bubble"]
        if proxy_url:
            args.append(f"--proxy-server={proxy_url}")
        args.append(signup_url)
        return subprocess.Popen(args)

    def find_profile_window(self, profile_dir, timeout_s):
        # A fresh profile opens exactly one window; find the one whose active
        # tab is the sign-up page (AppleScript can't map a window to a PID
        # cleanly, but the URL is unambiguous here). Then maximize it to the
        # screen so the fraction-based click coordinates map onto a large,
        # stable area — mirrors the Windows restore+maximize step.
        script = (
            'tell application "Finder" to set d to bounds of window of desktop\n'
            f'tell application "{self._app}"\n'
            ' repeat with w in windows\n'
            '  try\n'
            '   if (URL of active tab of w) starts with "https://elevenlabs.io/app/sign-up" then\n'
            '    set bounds of w to d\n'
            '    set b to bounds of w\n'
            '    return ((item 1 of b) as text) & "," & ((item 2 of b) as text) & ","'
            ' & ((item 3 of b) as text) & "," & ((item 4 of b) as text)\n'
            '   end if\n'
            '  end try\n'
            ' end repeat\n'
            'end tell\n'
            'return ""')
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                res = _osascript(script)
            except (SystemExit, subprocess.TimeoutExpired):
                res = ""  # app not scriptable yet / permission prompt pending; retry
            if res:
                x1, y1, x2, y2 = (int(v) for v in res.split(","))
                return MacWindow(left=x1, top=y1, width=x2 - x1, height=y2 - y1, app=self._app)
        raise SystemExit("auto-register 未找到新的临时浏览器窗口；aborting")

    def ensure_foreground(self, window) -> None:
        # A user-launched CLI is a foreground process, so `activate` is honored
        # (unlike Windows, macOS needs no thread-attach dance here).
        _osascript(f'tell application "{window.app}" to activate')
        time.sleep(0.15)

    def kill_profile(self, profile_dir, popen) -> None:
        subprocess.run(["pkill", "-f", str(profile_dir)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)
