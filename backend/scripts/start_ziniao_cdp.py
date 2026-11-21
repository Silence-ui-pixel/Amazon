"""一键把紫鸟店铺以 agent 模式挂起，并记录 CDP 端口到 data/aba/cdp_port。

用法（在 backend 目录下）：
    .venv/Scripts/python.exe scripts/start_ziniao_cdp.py [storeId]

参数与配置（优先级从高到低）：
- CLI 路径：KEYWORD_ZINIAO_CLI 环境变量，否则自动探测
  %APPDATA%/ziniaobrowser/cli/ziniao.exe。
- 店铺：命令行参数 > KEYWORD_ABA_STORE_ID 环境变量 > browser list 取第一个。

只负责「把浏览器带调试端口挂在那里」，不负责登录卖家后台；登录仍在紫鸟窗口中完成。
脚本幂等：若已存在可连的 CDP 端口则直接复用，不重复启动。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data" / "aba"
CDP_PORT_FILE = DATA / "cdp_port"


def find_cli() -> Path:
    env = os.environ.get("KEYWORD_ZINIAO_CLI", "").strip()
    if env:
        path = Path(env)
        if path.is_file():
            return path
        raise SystemExit(f"KEYWORD_ZINIAO_CLI 指向的文件不存在：{env}")
    default = Path.home() / "AppData" / "Roaming" / "ziniaobrowser" / "cli" / "ziniao.exe"
    if default.is_file():
        return default
    raise SystemExit("找不到紫鸟 CLI，请设置 KEYWORD_ZINIAO_CLI 环境变量")


def resolve_store(cli: Path, argv_store: str | None) -> str:
    if argv_store:
        return argv_store
    env = os.environ.get("KEYWORD_ABA_STORE_ID", "").strip()
    if env:
        return env
    try:
        result = subprocess.run(
            [str(cli), "browser", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"browser list 调用失败：{exc}") from exc
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip().isdigit():
            return parts[0].strip()
    raise SystemExit("browser list 没有店铺，请传入 storeId 或设置 KEYWORD_ABA_STORE_ID")


def start_agent(cli: Path, store: str) -> None:
    print(f"CLI:  {cli}")
    print(f"店铺: {store}")
    try:
        result = subprocess.run(
            [str(cli), "browser", "start", "--id", store, "--mode", "agent", "--json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"browser start 调用失败：{exc}") from exc
    if '"ok":true' not in result.stdout:
        raise SystemExit(
            "browser start 未返回成功："
            + (result.stdout or result.stderr or "").strip()[-400:]
        )
    print("agent 模式启动完成")


def discover_port() -> int:
    # 复用后端探测逻辑（显式端口 → cdp_port 文件 → 38979 → netstat 扫描 Chrome/129）。
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(DATA)
    last_error: Exception | None = None
    for _ in range(30):
        try:
            port = session.discover_port()
        except Exception as exc:  # noqa: BLE001 - 多个店铺时会抛 ambiguous，重试没有意义
            last_error = exc
            raise SystemExit(f"发现 CDP 端口失败：{exc}") from exc
        if port is not None:
            return port
        time.sleep(1)
    detail = f"（{last_error}）" if last_error else ""
    raise SystemExit(f"agent 启动完成但 30 秒内未发现 CDP 端口{detail}")


def main() -> None:
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))
    cli = find_cli()
    argv_store = sys.argv[1] if len(sys.argv) > 1 else None

    # 已挂好则复用，不重复启动。
    try:
        existing = discover_port()
    except SystemExit:
        existing = None
    if existing is not None:
        CDP_PORT_FILE.write_text(str(existing), encoding="utf-8")
        print(f"已存在可用 CDP 端口 {existing}，无需重启（已写入 data/aba/cdp_port）")
        return

    store = resolve_store(cli, argv_store)
    start_agent(cli, store)
    port = discover_port()
    CDP_PORT_FILE.write_text(str(port), encoding="utf-8")
    print(f"CDP 端口 {port} 已写入 data/aba/cdp_port")


if __name__ == "__main__":
    main()
