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
        # Target our exact instance by PID via System Events. `tell application
        # "Google Chrome"` would address whichever instance macOS registered for
        # the bundle — the user's daily browser, not our throwaway --user-data-dir
        # process. A fresh profile opens exactly one window (window 1). Chrome
        # ignores --window-position/--window-size at launch (it may land on a
        # secondary monitor at an arbitrary size), so normalize the window onto
        # the main display (positive origin) at a fixed size via AX, then read
        # back the real geometry. Requires Accessibility.
        px, py = self.WINDOW_POSITION
        sw, sh = self.WINDOW_SIZE
        script = f'''
        tell application "System Events"
            set procs to (every process whose unix id is {self._pid})
            if procs is {{}} then return ""
            set p to item 1 of procs
            if (count of windows of p) is 0 then return ""
            set w to window 1 of p
            set position of w to {{{px}, {py}}}
            set size of w to {{{sw}, {sh}}}
            set pos to position of w
            set sz to size of w
            return ((item 1 of pos) as text) & "," & ((item 2 of pos) as text) & "," & ((item 1 of sz) as text) & "," & ((item 2 of sz) as text)
        end tell'''
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                res = _osascript(script)
            except (SystemExit, subprocess.TimeoutExpired):
                res = ""  # window not up yet / Accessibility prompt pending; retry
            if res:
                x, y, w, h = (int(v) for v in res.split(","))
                return MacWindow(left=x, top=y, width=w, height=h, app=self._app)
        raise SystemExit(
            "auto-register 未找到临时浏览器窗口（若卡在这里，检查 系统设置→隐私与安全性→"
            "辅助功能 是否已给终端授权）；aborting")

    def ensure_foreground(self, window) -> None:
        # Raise OUR process by PID (not `tell application by name`, which targets
        # the daily instance). A user-launched CLI plus this is enough on macOS —
        # no Windows-style thread-attach dance needed.
        if self._pid is None:
            return
        _osascript(f'''
        tell application "System Events"
            set procs to (every process whose unix id is {self._pid})
            if procs is not {{}} then set frontmost of (item 1 of procs) to true
        end tell''')
        time.sleep(0.15)

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
