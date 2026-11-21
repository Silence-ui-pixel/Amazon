"""在独立 Edge/Chrome 应用窗口中显示本机界面，采集进程独立常驻。"""

import ctypes
import os
import subprocess
from pathlib import Path

from agent.status_ui import STATUS

WINDOW_TITLE = "关键词库采集器 · 运行监控"


def browser_executable() -> Path | None:
    for base in [os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")]:
        if not base:
            continue
        for suffix in ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"]:
            path = Path(base) / suffix
            if path.is_file():
                return path
    return None


def existing_window() -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
    user32.FindWindowW.restype = ctypes.c_void_p
    return int(user32.FindWindowW(None, WINDOW_TITLE) or 0)


def show_window() -> None:
    handle = existing_window()
    if handle:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.ShowWindow(handle, 9)
        user32.SetForegroundWindow(handle)
        return
    executable = browser_executable()
    if executable is None:
        return
    profile = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "KeywordLibraryAgent" / "window-profile"
    url = "http://127.0.0.1:7100/v1/ui#" + STATUS.ui_token
    subprocess.Popen(  # noqa: S603 - fixed local app URL and dedicated browser profile
        [str(executable), "--app=" + url, "--user-data-dir=" + str(profile), "--no-first-run",
         "--no-default-browser-check", "--disable-sync", "--window-size=1220,880"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def hide_window() -> None:
    handle = existing_window()
    if handle:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.ShowWindow(handle, 0)


def run_browser_window(*, background: bool = False) -> None:
    if not background:
        STATUS.show_requested.set()
    while not STATUS.shutdown_requested.wait(0.2):
        if STATUS.show_requested.is_set():
            STATUS.show_requested.clear()
            show_window()
        if STATUS.hide_requested.is_set():
            STATUS.hide_requested.clear()
            hide_window()
    os._exit(0)
