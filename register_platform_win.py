#!/usr/bin/env python3
"""Windows PlatformDriver: real Chrome via subprocess + WMI window lookup +
Win32 foreground forcing.

Behavior-identical to the pre-refactor register_one Windows path. The
foreground-forcing order (AttachThreadInput / BringWindowToTop /
SetForegroundWindow / topmost toggle) was tuned against Windows' foreground-lock
rules — do not rewrite it.
"""
from __future__ import annotations

import ctypes
import pathlib
import shutil
import subprocess
import time

import pygetwindow as gw

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class WinDriver:
    mod_key = "ctrl"  # clipboard / select-all / address-bar modifier on Windows

    def launch_chrome(self, profile_dir, signup_url, proxy_url):
        args = [CHROME, f"--user-data-dir={profile_dir}", "--no-first-run",
                "--new-window", "--window-position=40,40",
                "--disable-save-password-bubble", "--do-not-de-elevate"]
        if proxy_url:
            args.append(f"--proxy-server={proxy_url}")
        args.append(signup_url)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL: do not inherit a hidden Web UI process state.
        return subprocess.Popen(args, startupinfo=startupinfo,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    def _profile_window_handles(self, profile_dir) -> set[int]:
        if not shutil.which("powershell"):
            return set()
        profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
        ps = (
            f"$profile = '{profile_name}'; "
            "$pids = @(Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
            "Select-Object -ExpandProperty ProcessId); "
            "if ($pids.Count -gt 0) { "
            "Get-Process -Id $pids -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowHandle -ne 0 } | "
            "ForEach-Object { $_.MainWindowHandle } "
            "}"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=3)
        except Exception:
            return set()
        handles: set[int] = set()
        for line in out.stdout.splitlines():
            try:
                handles.add(int(line.strip()))
            except ValueError:
                pass
        return handles

    @staticmethod
    def _window_op(window, name: str) -> None:
        try:
            getattr(window, name)()
        except Exception as e:
            # pygetwindow/pywin32 can report Windows error code 0 ("success")
            # after the window operation actually completed. Treat only that
            # wrapper bug as non-fatal; real focus/window errors should abort.
            if "Error code from Windows: 0" not in str(e):
                raise

    def find_profile_window(self, profile_dir, timeout_s):
        new_window = None
        # 30s: a brand-new profile cold-starts slowly (profile init + AV scan); 10s
        # missed the window on busy machines and the finally-block killed late Chrome.
        deadline = time.time() + timeout_s
        next_profile_probe = 0.0
        while time.time() < deadline and new_window is None:
            time.sleep(0.5)
            profile_handles: set[int] = set()
            if time.time() >= next_profile_probe:
                profile_handles = self._profile_window_handles(profile_dir)
                next_profile_probe = time.time() + 1.0
            for w in gw.getAllWindows():
                hwnd = getattr(w, "_hWnd", None)
                if hwnd in profile_handles:
                    new_window = w
                    break
        if new_window is None:
            raise SystemExit("auto-register could not find the new temporary Chrome window; aborting")
        # One-time: bring the fresh window up and maximize so fractional click
        # coordinates map onto a large, stable area.
        self._window_op(new_window, "restore")
        self._window_op(new_window, "maximize")
        return new_window

    def ensure_foreground(self, window) -> None:
        hwnd = getattr(window, "_hWnd", None)
        if not hwnd:
            self._window_op(window, "activate")
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Fast path: already foreground. Forcing anyway is what caused the
        # constant restore/maximize flicker between every automation action.
        if user32.GetForegroundWindow() == hwnd:
            return
        SW_RESTORE = 9
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_SHOWWINDOW = 0x0040
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        for attempt in range(8):
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            # AttachThreadInput to the current foreground thread satisfies
            # Windows' foreground-lock rules. A synthetic Alt tap also works
            # but toggles Chrome's menu-accelerator mode, breaking in-window
            # keyboard focus for the very keys we send next.
            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            cur_tid = kernel32.GetCurrentThreadId()
            attached = fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                if attempt >= 4:  # last resort: topmost toggle
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            finally:
                if attached:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
            time.sleep(0.2)
            if user32.GetForegroundWindow() == hwnd:
                return
        raise SystemExit("auto-register could not focus the temporary Chrome window; aborting before sending keys")

    def kill_profile(self, profile_dir, popen) -> None:
        if shutil.which("powershell"):
            profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
            subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"$profile = '{profile_name}'; "
                "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif popen is not None:
            popen.terminate()
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)
