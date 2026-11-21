"""关键词库采集端（Windows 客户端）。

职责：常驻后台，连接服务器，把服务器发来的浏览器操作交给本机 Agent（127.0.0.1:7100）。
用户只需要在第一次运行时输入页面上的配对码；之后开机自启，不再需要任何窗口。

设计要点：
- token 用 Windows DPAPI 加密后落盘，配置文件里没有明文凭据。
- 开机自启写注册表 Run 项，不需要管理员权限。
- 单实例互斥，重复双击不会再起一个进程。
- 断线自动重连；连上后由服务器下发任务，本机 7100 的 Agent 负责真正的采集。
"""

import asyncio
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

# 本机 Python 运行时的 ssl.py 被打了补丁：SSLContext.__new__ 会强制加载与 ssl.py
# 同目录的 codex-rejected-cas.pem。PyInstaller 单文件打包后这个文件不在文件系统
# 上，于是任何 HTTPS 调用（配对、版本检查、下载升级）都会抛
# [Errno 2] No such file or directory。冻结环境下直接走底层 _ssl，跳过这层依赖。
if getattr(sys, "frozen", False):
    import _ssl
    import ssl

    def _frozen_ssl_context_new(cls, protocol=None, *args, **kwargs):
        if protocol is None:
            protocol = ssl.PROTOCOL_TLS
        return _ssl._SSLContext.__new__(cls, protocol)

    ssl.SSLContext.__new__ = staticmethod(_frozen_ssl_context_new)

APP_NAME = "KeywordLibraryAgent"
from agent.version import AGENT_VERSION as APP_VERSION
# 目录版（onedir）把依赖放在 _internal/，不回读 exe 自身，适合被安全软件
# 锁住 Downloads 目录的机器；它的自动替换不能安全覆盖正在加载的 DLL。
ONEDIR = getattr(sys, "frozen", False) and (
    Path(sys.executable).parent / "_internal"
).is_dir()
DEFAULT_SERVER = os.environ.get(
    "KEYWORD_DEFAULT_SERVER", "https://keyword-library.8.134.61.236.sslip.io"
)
MUTEX_NAME = r"Global\KeywordLibraryAgentClient"
RECONNECT_SECONDS = 5.0
MAX_FRAME = 32 * 1024 * 1024


# ---------------------------------------------------------------- 配置与凭据
def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def dpapi_encrypt(text: str) -> str:
    """用当前 Windows 用户的 DPAPI 加密，只在这台机器这个账户下能解开。"""
    import base64

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source = _blob(text.encode("utf-8"))
    result = _Blob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
    ):
        raise OSError("DPAPI 加密失败")
    try:
        return base64.b64encode(ctypes.string_at(result.pbData, result.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(result.pbData)


def dpapi_decrypt(cipher: str) -> str:
    import base64

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source = _blob(base64.b64decode(cipher))
    result = _Blob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
    ):
        raise OSError("DPAPI 解密失败")
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(result.pbData)


def load_config() -> dict[str, Any] | None:
    path = config_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("device_id"):
        return None
    cipher = payload.get("token_cipher")
    if not isinstance(cipher, str):
        return None
    try:
        payload["token"] = dpapi_decrypt(cipher)
    except OSError:
        return None
    return payload


def save_config(server: str, device_id: str, token: str, ws_url: str) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "server": server.rstrip("/"),
        "device_id": device_id,
        "ws_url": ws_url,
        "token_cipher": dpapi_encrypt(token),
    }
    path = config_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# ---------------------------------------------------------------- 自动升级
# 发布新版本只需替换服务器 /agent/ 下的 KeywordLibraryAgent.exe 与 version.txt，
# 客户端会在启动或空闲时自动比对、下载、校验并重启，无需用户重新下载安装。
UPDATE_CHECK_SECONDS = 300
# 70MB 安装包在弱网下常中断，允许多次重试。
DOWNLOAD_ATTEMPTS = 4
# 替换脚本：Windows 会锁住运行中的 exe，必须等旧进程退出才能覆盖。
# 这里最多重试 30 次（约 1 分钟），成功则拉起新版本并清理自身。
UPDATE_CMD = """@echo off
setlocal enabledelayedexpansion
set "PYINSTALLER_RESET_ENVIRONMENT=1"
cd /d "%~dp0"
set "TRIES=0"
:wait
ping -n 2 127.0.0.1 >nul
copy /Y "KeywordLibraryAgent.new.exe" "KeywordLibraryAgent.exe" >nul 2>&1
if errorlevel 1 (
  set /a TRIES+=1
  if !TRIES! GEQ 30 goto giveup
  goto wait
)
del /q "KeywordLibraryAgent.new.exe" >nul 2>&1
start "" "KeywordLibraryAgent.exe" --background
del /q "%~f0" >nul 2>&1
exit /b 0
:giveup
start "" "KeywordLibraryAgent.exe" --background --defer-update
del /q "%~f0" >nul 2>&1
exit /b 1
"""


def update_marker() -> Path:
    return config_dir() / "agent.version"


def remote_version(server: str) -> tuple[str, str] | None:
    """version.txt 内容：`<版本> <sha256>`；读不到就跳过，绝不阻塞采集。"""
    import httpx

    try:
        response = httpx.get(
            server.rstrip("/") + "/agent/version.txt",
            timeout=15.0,
            trust_env=False,
        )
        if response.status_code != 200:
            return None
        parts = response.text.strip().split()
        if len(parts) < 2 or len(parts[1]) != 64:
            return None
        return parts[0], parts[1].lower()
    except (httpx.HTTPError, ValueError):
        return None


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def download_update(server: str, version: str, expected: str) -> bool:
    """下载到同目录 .new.exe，校验指纹后交给 update.cmd 替换并拉起新版本。

    安装包 70MB+，弱网下很容易中途断流；这里做多次重试并支持断点续传，
    避免一次失败就永远停在旧版本。
    """
    import hashlib

    import httpx

    from agent.status_ui import STATUS

    if not getattr(sys, "frozen", False):
        return False
    current = Path(executable_path()).resolve()
    temporary = current.with_name("KeywordLibraryAgent.new.exe")
    url = server.rstrip("/") + "/agent/KeywordLibraryAgent.exe"
    if temporary.is_file() and hashlib.sha256(temporary.read_bytes()).hexdigest().lower() == expected.lower():
        return _schedule_replace(current, version)
    for attempt in range(DOWNLOAD_ATTEMPTS):
        STATUS.set_update(f"正在下载 {version}，第 {attempt + 1} 次尝试", "downloading")
        digest = hashlib.sha256()
        downloaded = 0
        try:
            headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
            with httpx.stream(
                "GET",
                url,
                timeout=httpx.Timeout(60.0, read=300.0),
                trust_env=False,
                headers=headers,
            ) as response:
                if response.status_code not in (200, 206):
                    _remove(temporary)
                    return False
                mode = "ab" if downloaded and response.status_code == 206 else "wb"
                if mode == "wb":
                    downloaded = 0
                    digest = hashlib.sha256()
                with temporary.open(mode) as output:
                    for chunk in response.iter_bytes(256 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        STATUS.set_update(f"正在下载 {version}，已接收 {downloaded / 1048576:.1f} MB", "downloading")
        except (httpx.HTTPError, OSError):
            _remove(temporary)
            time.sleep(3.0 * (attempt + 1))
            continue
        if digest.hexdigest().lower() == expected.lower():
            return _schedule_replace(current, version)
        # 指纹不符：可能是下载不全，重试一次；仍不行就放弃。
        _remove(temporary)
        time.sleep(3.0 * (attempt + 1))
    return False


def _schedule_replace(current: Path, version: str) -> bool:
    """写好替换脚本并启动它；调用方随后必须结束进程。"""
    from agent.status_ui import STATUS

    with STATUS.lock:
        busy = bool(STATUS.inflight)
        if not busy:
            STATUS.update = f"{version} 校验通过，正在重启"
            STATUS.update_phase = "restarting"
    if busy:
        STATUS.set_update(f"{version} 已下载，等待抓取任务结束后更新", "queued")
        STATUS.update_requested.set()
        return False
    script = current.with_name("update.cmd")
    # .cmd 必须纯 ASCII，且不能写死含中文的路径；用 %~dp0 在运行时展开。
    commands = UPDATE_CMD
    if STATUS.reopen_after_update:
        commands = commands.replace('start "" "KeywordLibraryAgent.exe" --background\n', 'start "" "KeywordLibraryAgent.exe"\n')
    script.write_text(commands, encoding="ascii")
    subprocess.Popen(  # noqa: S603 - 固定参数，无用户输入
        ["cmd.exe", "/c", str(script)],
        cwd=str(current.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
    )
    # 给替换脚本一点时间进入等待循环，再结束本进程释放 exe 文件锁。
    time.sleep(1.5)
    return True


def check_update(server: str, *, initial: bool = False) -> bool:
    """返回 True 表示已安排替换重启，调用方应立即结束进程。"""
    from agent.status_ui import STATUS

    STATUS.set_update("正在检查新版本", "checking")
    # 标记只描述实际运行版本，不能把发现/下载新版本当作安装成功。
    marker = update_marker()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(APP_VERSION, encoding="utf-8")
    except OSError:
        pass
    if ONEDIR:
        # 目录版正在加载 _internal 里的 DLL，运行中覆盖会失败；升级走重新下载压缩包。
        STATUS.set_update("目录版请通过完整安装包升级", "manual_install")
        return False
    remote = remote_version(server)
    if remote is None:
        STATUS.set_update("版本检查暂未成功，空闲时会自动重试", "error")
        return False
    version, digest = remote
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        STATUS.set_update("发布版本格式异常，稍后重试", "error")
        return False
    local_parts = tuple(int(part) for part in APP_VERSION.split("."))
    remote_parts = tuple(int(part) for part in version.split("."))
    if remote_parts <= local_parts:
        STATUS.set_update(f"当前 {APP_VERSION}，已是可用最新版本", "current")
        return False
    scheduled = download_update(server, version, digest)
    if not scheduled and STATUS.snapshot()["update_phase"] != "queued":
        STATUS.set_update(f"{version} 下载未完成，空闲时会自动重试", "error")
    return scheduled


def exit_for_update() -> None:
    """安排好替换后立刻结束进程，让 update.cmd 能覆盖正在运行的 exe。

    Windows 会锁住运行中的 exe 文件；只有进程退出后 move 才能成功。
    这里必须真的退出进程，光返回到调用方是不够的（长连接循环不会自己结束）。
    """
    os._exit(0)


async def update_watch(server: str, tasks: set[asyncio.Task[None]]) -> None:
    """只在没有在途采集时升级，避免打断用户正在跑的任务。"""
    from agent.status_ui import STATUS

    while True:
        await asyncio.to_thread(STATUS.update_requested.wait, UPDATE_CHECK_SECONDS)
        if tasks:
            if STATUS.update_requested.is_set():
                STATUS.set_update("当前有抓取任务，空闲后立即检查更新", "queued")
                await asyncio.sleep(1)
            continue
        STATUS.update_requested.clear()
        if await asyncio.to_thread(check_update, server):
            os._exit(0)
        if STATUS.snapshot()["update_phase"] != "queued":
            STATUS.reopen_after_update = False


# ---------------------------------------------------------------- 开机自启
def executable_path() -> str:
    return sys.executable if getattr(sys, "frozen", False) else sys.executable


def enable_autostart() -> bool:
    import winreg

    target = executable_path()
    command = f'"{target}" --background'
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        return True
    except OSError:
        return False


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        return bool(value)
    except OSError:
        return False


# ---------------------------------------------------------------- 单实例
def acquire_single_instance() -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def show_existing_window() -> None:
    import httpx

    with suppress(httpx.HTTPError):
        httpx.post("http://127.0.0.1:7100/v1/ui/show", timeout=3, trust_env=False)


# ---------------------------------------------------------------- 配对窗口
def ask_pairing(default_server: str) -> tuple[str, str] | None:
    """首次运行的小窗口：填服务器地址与页面上的配对码。"""
    import tkinter as tk
    from tkinter import ttk

    result: dict[str, str] = {}
    root = tk.Tk()
    root.title("关键词库采集端 · 首次配对")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0)

    ttk.Label(frame, text="服务器地址").grid(row=0, column=0, sticky="w")
    server_var = tk.StringVar(value=default_server)
    ttk.Entry(frame, textvariable=server_var, width=44).grid(row=1, column=0, pady=(0, 10))

    ttk.Label(frame, text="配对码（页面上显示的 8 位）").grid(row=2, column=0, sticky="w")
    code_var = tk.StringVar()
    try:
        clipboard = root.clipboard_get().strip()
        if 8 <= len(clipboard.replace("-", "")) <= 12:
            code_var.set(clipboard)
    except tk.TclError:
        pass
    entry = ttk.Entry(frame, textvariable=code_var, width=44)
    entry.grid(row=3, column=0, pady=(0, 12))
    entry.focus_set()

    status = ttk.Label(frame, text="", foreground="#b00")
    status.grid(row=4, column=0, sticky="w")

    def submit() -> None:
        code = code_var.get().strip()
        server = server_var.get().strip()
        if len(code.replace("-", "")) < 8:
            status.configure(text="请填写页面上的配对码")
            return
        if not server.startswith(("http://", "https://")):
            status.configure(text="服务器地址要以 http(s):// 开头")
            return
        result["server"] = server
        result["code"] = code
        root.destroy()

    ttk.Button(frame, text="完成配对", command=submit).grid(row=5, column=0, sticky="e")
    root.bind("<Return>", lambda _event: submit())
    root.mainloop()
    if "server" not in result:
        return None
    return result["server"], result["code"]


def pair(server: str, code: str) -> dict[str, Any]:
    import httpx

    response = httpx.post(
        f"{server.rstrip('/')}/api/v1/agent/pair",
        json={"code": code, "display_name": os.environ.get("COMPUTERNAME", "我的电脑"), "version": APP_VERSION},
        timeout=30.0,
        trust_env=False,
    )
    payload: Any = response.json()
    if response.status_code >= 400 or not isinstance(payload, dict) or "device_id" not in payload:
        message = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", ""))
        raise RuntimeError(message or f"配对失败（HTTP {response.status_code}）")
    return dict(payload)


# ---------------------------------------------------------------- 长连接
async def _request_local_agent(local: str, method: str, path: str, body: object, headers: dict[str, str]) -> Any:
    import httpx

    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(
                base_url=local, timeout=httpx.Timeout(900.0),
                headers={"Connection": "close"}, trust_env=False,
            ) as fresh:
                return await fresh.request(
                    method, path,
                    json=body if method not in {"GET", "HEAD"} and isinstance(body, dict) else None,
                    headers=headers or None,
                )
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError):
            # Read-only status can be retried; collection POSTs may already have
            # executed and must never be replayed on an unknown read outcome.
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(0.15 * (attempt + 1))
    raise AssertionError("unreachable")


async def _handle(websocket: Any, frame: dict[str, object], local: str) -> None:
    import httpx

    from agent.status_ui import STATUS

    request_id = str(frame.get("id", ""))
    method = str(frame.get("method", "GET")).upper()
    path = str(frame.get("path", "/"))
    body = frame.get("body")
    operation_id = str(frame.get("operation_id", "") or "").strip()
    headers = {"X-Keyword-Operation-ID": operation_id} if operation_id else {}
    headers["X-Keyword-Request-ID"] = request_id
    if STATUS.snapshot()["update_phase"] == "restarting":
        await websocket.send(json.dumps({"type": "response", "id": request_id, "body": {"error": {
            "status_code": 503, "code": "AGENT_CHANNEL_OFFLINE", "message": "采集器正在升级重启，请稍后重试"
        }}}, ensure_ascii=False))
        return
    STATUS.begin(request_id, path, body)
    # 本机 Agent 的 uvicorn 与 keep-alive 复用连接不兼容：复用后的第二个请求
    # 会被判成 404（{"detail":"Not Found"}）。这里每次都用新连接，避免"第一次
    # 成功、之后全部失败"的隐蔽故障。
    try:
        response = await _request_local_agent(local, method, path, body, headers)
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {
                "error": {
                    "status_code": 502,
                    "code": "AGENT_INVALID_RESPONSE",
                    "message": "本机 Agent 返回非 JSON",
                }
            }
    except httpx.HTTPError as error:
        payload = {
            "error": {
                "status_code": 503,
                "code": "AGENT_UNAVAILABLE",
                "message": f"本机 Agent 不可达（{type(error).__name__}）",
            }
        }
    if not isinstance(payload, dict):
        payload = {
            "error": {"status_code": 502, "code": "AGENT_INVALID_RESPONSE", "message": "结构异常"}
        }
    try:
        await websocket.send(
            json.dumps({"type": "response", "id": request_id, "body": payload}, ensure_ascii=False)
        )
    except Exception:
        STATUS.finish(request_id, {**payload, "error": {"message": "已收到采集结果，但服务器连接断开，回传未完成"}})
        raise
    STATUS.finish(request_id, payload)


async def channel_loop(config: dict[str, Any], server: str) -> None:
    import websockets

    from agent.status_ui import STATUS

    url = str(config["ws_url"])
    device_id = str(config["device_id"])
    token = str(config["token"])
    local = os.environ.get("KEYWORD_AGENT_LOCAL_URL", "http://127.0.0.1:7100").rstrip("/")
    tasks: set[asyncio.Task[None]] = set()
    asyncio.create_task(update_watch(server, tasks))
    while True:
        try:
            STATUS.set_connection("正在连接服务器", "已保存本机独立绑定，正在建立连接")
            async with websockets.connect(
                url, max_size=MAX_FRAME, open_timeout=20.0
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "device_id": device_id,
                            "token": token,
                            "version": APP_VERSION,
                        }
                    )
                )
                await websocket.send(json.dumps({"type": "ping"}))
                async for raw in websocket:
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") == "pong":
                        STATUS.set_connection("已连接服务器", "设备身份已确认，可以接收抓取任务")
                        continue
                    if message.get("type") != "request":
                        continue
                    task = asyncio.create_task(_handle(websocket, message, local))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except Exception:  # noqa: BLE001 - 断线必须继续重连
            pass
        STATUS.set_connection("连接已断开", "5 秒后自动重连，请检查网络或服务器")
        await asyncio.sleep(RECONNECT_SECONDS)


def ensure_local_agent() -> None:
    """本机 Agent 没在跑时顺手拉起来（同目录优先，其次项目目录）。"""
    import httpx

    try:
        httpx.get("http://127.0.0.1:7100/v1/health", timeout=2.0, trust_env=False)
        return
    except httpx.HTTPError:
        pass
    candidates = [
        Path(executable_path()).parent / "KeywordLibraryAgentRuntime" / "pythonw.exe",
        Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "pythonw.exe",
    ]
    for python in candidates:
        if not python.is_file():
            continue
        subprocess.Popen(  # noqa: S603 - 固定参数，无用户输入
            [str(python), "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1", "--port", "7100"],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(4)
        return


def main(*, instance_acquired: bool = False) -> int:
    if os.name != "nt":
        print("采集端只支持 Windows")
        return 1
    if not instance_acquired and acquire_single_instance() is None:
        show_existing_window()
        return 0

    config = load_config()
    argv = sys.argv[1:]
    if config is None:
        server, code = DEFAULT_SERVER, ""
        if "--pair" in argv:
            index = argv.index("--pair")
            if index + 1 < len(argv):
                code = argv[index + 1]
            server = next((a.split("=", 1)[1] for a in argv if a.startswith("--server=")), server)
        else:
            answer = ask_pairing(DEFAULT_SERVER)
            if answer is None:
                return 0
            server, code = answer
        try:
            payload = pair(server, code)
        except Exception as error:  # noqa: BLE001 - 需要把原因显示给用户
            try:
                import traceback

                log = (
                    Path(os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home()))
                    / "keyword-agent-error.log"
                )
                log.write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
            _message_box(f"配对失败：{error}")
            return 1
        save_config(server, str(payload["device_id"]), str(payload["token"]), str(payload["ws_url"]))
        config = load_config()
        if config is None:
            _message_box("配对信息保存失败，请检查当前用户权限")
            return 1
        enable_autostart()

    server = str(config.get("server") or DEFAULT_SERVER)
    enable_autostart()
    from agent.status_ui import STATUS, run_status_window

    def connect() -> None:
        try:
            if "--defer-update" not in argv and check_update(server, initial=True):
                exit_for_update()
            ensure_local_agent()
            asyncio.run(channel_loop(config, server))
        except Exception as error:
            STATUS.set_connection("采集端启动失败", f"{type(error).__name__}，请查看 startup.log")
            from agent.standalone import write_startup_log

            write_startup_log("channel_thread", error)

    threading.Thread(target=connect, daemon=True, name="collector-channel").start()
    run_status_window(config, background="--background" in argv)
    return 0


def _message_box(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "关键词库采集端", 0x10)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
