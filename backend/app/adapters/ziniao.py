"""紫鸟浏览器的本地 CDP 发现：只连接已打开的店铺，不负责启动客户端。"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from threading import RLock
from typing import cast

import httpx

from app.domain.errors import DomainError

# 紫鸟内核的 Chrome 版本会随客户端升级变化，不能写死版本号；
# 改为按进程可执行文件路径判定（紫鸟内核位于 ziniaobrowser 目录下），
# 这样别人的电脑上换了紫鸟版本也能认出店铺浏览器。
ZINIAO_BROWSER = re.compile(r"Chrome/\d+\.")
ZINIAO_PATH_MARKER = "ziniaobrowser"
FALLBACK_PORT = 38979
_START_LOCK = RLock()


def data_directory() -> Path:
    """默认与业务数据同目录；可用 KEYWORD_BROWSER_DIR 指向其他会话目录。"""
    configured = os.getenv("KEYWORD_BROWSER_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "amazon"


# 127.0.0.1 是后端所在机器：部署到服务器不会自动连接用户电脑的紫鸟。
# 端口探测只检查浏览器身份，成功不代表店铺、ABA 或卖家精灵已经登录。
def probe_port(port: int, pid: int | None = None) -> bool:
    """只探测本机端口，不发起任何店铺操作。

    紫鸟内核版本会随客户端升级变化，写死 Chrome/129 会让别人的电脑直接认不出
    已经打开的店铺浏览器。因此以「进程可执行文件位于紫鸟目录下」为准，
    拿不到进程信息时才退回旧版本号判断。
    """
    try:
        with httpx.Client(trust_env=False, timeout=1.2) as client:
            response = client.get(f"http://127.0.0.1:{port}/json/version")
            payload: object = response.json()
            if response.status_code != 200 or not isinstance(payload, dict):
                return False
            browser = str(cast(dict[str, object], payload).get("Browser", ""))
    except (httpx.HTTPError, ValueError):
        return False
    if not browser.startswith("Chrome/"):
        return False
    if pid is not None:
        # 有进程信息时以路径判定最准：紫鸟内核在 ziniaobrowser 目录里。
        return _is_ziniao_process(pid)
    return bool(ZINIAO_BROWSER.search(browser))


def _process_image(pid: int) -> str:
    """读取进程的可执行文件路径；失败返回空串。"""
    if os.name != "nt" or pid <= 0:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return ""


def _is_ziniao_process(pid: int) -> bool:
    return ZINIAO_PATH_MARKER in _process_image(pid).lower()


def listening_ports() -> list[int]:
    return sorted(listening_ports_with_pid())


def listening_ports_with_pid() -> dict[int, int]:
    """本机监听端口 → 进程 PID；用来把调试端口认回紫鸟浏览器。"""
    if os.name != "nt":
        return {}
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    ports: dict[int, int] = {}
    for line in result.stdout.decode("latin1").splitlines():
        match = re.search(r"127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
        if match:
            ports[int(match.group(1))] = int(match.group(2))
    return ports


def _remembered(directory: Path) -> int | None:
    try:
        value = (directory / "cdp_port").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(value) if value.isdecimal() and 1 <= int(value) <= 65535 else None


def find_port(directory: Path, env_name: str, *, shared: Path | None = None) -> int | None:
    """环境变量 > 本目录记录 > 共用记录 > 默认端口 > 扫描本机端口。

    发现多个紫鸟店铺时不任意挑一个，要求显式配置，避免读到别的店铺数据。
    """
    explicit = os.environ.get(env_name, "").strip()
    if os.environ.get("KEYWORD_SERVER_COLLECTOR_SOCKET", "").strip():
        if explicit.isdecimal() and 1 <= int(explicit) <= 65535 and probe_port(int(explicit)):
            return int(explicit)
        raise DomainError(503, "SERVER_BROWSER_UNAVAILABLE", f"服务器浏览器不可达，请检查 {env_name}")
    candidates: list[int | None] = [
        int(explicit) if explicit.isdecimal() and 1 <= int(explicit) <= 65535 else None,
        _remembered(directory),
        _remembered(shared) if shared is not None else None,
        FALLBACK_PORT,
    ]
    for port in dict.fromkeys(candidate for candidate in candidates if candidate):
        if probe_port(port):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "cdp_port").write_text(str(port), encoding="utf-8")
            return port
    ports = listening_ports_with_pid()
    matches = [port for port, pid in ports.items() if probe_port(port, pid)]
    if len(matches) > 1:
        raise DomainError(
            503, "ZINIAO_BROWSER_AMBIGUOUS", f"发现多个紫鸟浏览器，请配置目标店铺的 {env_name}"
        )
    if not matches:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cdp_port").write_text(str(matches[0]), encoding="utf-8")
    return matches[0]


def find_cli() -> Path:
    """定位紫鸟 CLI；显式配置无效时直接报错，避免启动其他程序。"""

    configured = os.getenv("KEYWORD_ZINIAO_CLI", "").strip()
    path = (
        Path(configured)
        if configured
        else Path.home() / "AppData" / "Roaming" / "ziniaobrowser" / "cli" / "ziniao.exe"
    )
    if path.is_file():
        return path
    raise DomainError(503, "ZINIAO_CLI_REQUIRED", "找不到紫鸟 CLI，请检查紫鸟安装或配置路径")


def _listed_store(output: str | None) -> str | None:
    """兼容 CLI 的制表文本和 JSON 输出，只在能确定一个店铺时返回。

    CLI 偶发返回空 stdout（进程被拦截或未输出），此时按"读不到"处理，
    由调用方给出明确的配置提示，不能让 None 冒成 AttributeError。
    """
    if not output:
        return None

    stores: list[str] = []
    for line in output.splitlines():
        first = line.split("\t", 1)[0].strip()
        if first.isdecimal():
            stores.append(first)
    if not stores:
        try:
            payload = cast(object, json.loads(output))
        except ValueError:
            payload = None
        values = cast(list[object], payload) if isinstance(payload, list) else []
        for item in values:
            if isinstance(item, dict):
                record = cast(dict[str, object], item)
                value = record.get("id") or record.get("storeId")
                if str(value).isdecimal():
                    stores.append(str(value))
    unique = list(dict.fromkeys(stores))
    return unique[0] if len(unique) == 1 else None


def list_stores(cli: Path | None = None) -> list[dict[str, str]]:
    """列出紫鸟客户端里的店铺（ID + 名称），供页面让用户选择。

    别人电脑上往往登录了多个店铺，仅靠环境变量无法配置；这里把候选店铺暴露出去。
    """
    path = cli or find_cli()
    try:
        result = subprocess.run(
            [str(path), "browser", "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DomainError(503, "ZINIAO_START_FAILED", f"紫鸟店铺列表读取失败：{error}") from None
    output = result.stdout or ""
    stores: list[dict[str, str]] = []
    try:
        payload = cast(object, json.loads(output))
    except ValueError:
        payload = None
    items: list[object] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = cast(list[object], data["items"])
        elif isinstance(data, list):
            items = cast(list[object], data)
    elif isinstance(payload, list):
        items = cast(list[object], payload)
    for item in items:
        if not isinstance(item, dict):
            continue
        record = cast(dict[str, object], item)
        store_id = str(record.get("storeId") or record.get("id") or "").strip()
        if not store_id.isdecimal():
            continue
        name = str(record.get("storeName") or record.get("name") or "").strip()
        platform = str(record.get("platformName") or "").strip()
        stores.append({"id": store_id, "name": name, "platform": platform})
    if stores:
        return stores
    # JSON 不可用时退回制表文本：storeId\tstoreName\tplatformName\tip
    for line in output.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip().isdecimal():
            continue
        stores.append(
            {
                "id": parts[0].strip(),
                "name": parts[1].strip() if len(parts) > 1 else "",
                "platform": parts[2].strip() if len(parts) > 2 else "",
            }
        )
    return stores


def select_store(directory: Path, store_id: str) -> str:
    """选定要使用的紫鸟店铺，并记住到会话目录。"""
    value = store_id.strip()
    if not value.isdecimal():
        raise DomainError(422, "ZINIAO_STORE_INVALID", "紫鸟店铺 ID 必须是数字")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "store_id").write_text(value, encoding="utf-8")
    return value


def _remembered_store(directory: Path | None) -> str | None:
    if directory is None:
        return None
    try:
        value = (directory / "store_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value.isdecimal() else None


def _resolve_store(cli: Path, directory: Path, shared: Path | None) -> str:
    configured = (
        os.getenv("KEYWORD_ZINIAO_STORE_ID", "").strip()
        or os.getenv("KEYWORD_ABA_STORE_ID", "").strip()
    )
    selected = configured or _remembered_store(directory) or _remembered_store(shared)
    if selected:
        return selected
    try:
        result = subprocess.run(
            [str(cli), "browser", "list"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DomainError(503, "ZINIAO_START_FAILED", f"紫鸟店铺列表读取失败：{error}") from None
    store = _listed_store(result.stdout)
    if result.returncode != 0 or store is None:
        raise DomainError(
            503,
            "ZINIAO_STORE_REQUIRED",
            "无法唯一确定紫鸟店铺，请配置 KEYWORD_ZINIAO_STORE_ID",
        )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "store_id").write_text(store, encoding="utf-8")
    return store


def _start_agent(cli: Path, store: str) -> None:
    command = [str(cli), "browser", "start", "--id", store, "--mode", "agent", "--json"]

    def run_agent() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    try:
        result = run_agent()
        detail = result.stderr or result.stdout or ""
        if result.returncode != 0 and "Local Ziniao CLI service failed to start" in detail:
            service = subprocess.run(
                [str(cli), "start", "--remote"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if service.returncode == 0:
                time.sleep(3)
                result = run_agent()
    except (OSError, subprocess.SubprocessError) as error:
        raise DomainError(503, "ZINIAO_START_FAILED", f"紫鸟店铺启动失败：{error}") from None
    output = (result.stdout or "").replace(" ", "")
    # 某些 CLI 版本启动成功却不返回正文；仍由 ensure_port 探测真实 CDP 决定是否成功。
    if result.returncode != 0 or (output and '"ok":true' not in output):
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        raise DomainError(
            503,
            "ZINIAO_START_FAILED",
            "紫鸟店铺启动失败" + (f"：{detail}" if detail else ""),
        )


def ensure_port(directory: Path, env_name: str, *, shared: Path | None = None) -> int:
    """复用可用端口；没有端口时自动启动唯一目标店铺并等待 CDP。"""

    with _START_LOCK:
        port = find_port(directory, env_name, shared=shared)
        if port is not None:
            return port
        cli = find_cli()
        store = _resolve_store(cli, directory, shared)
        _start_agent(cli, store)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "store_id").write_text(store, encoding="utf-8")
        for _ in range(30):
            port = find_port(directory, env_name, shared=shared)
            if port is not None:
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "cdp_port").write_text(str(port), encoding="utf-8")
                return port
            time.sleep(1)
    raise DomainError(503, "ZINIAO_CDP_UNAVAILABLE", "紫鸟已启动，但 30 秒内未发现调试端口")
