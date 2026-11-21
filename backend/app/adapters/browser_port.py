"""浏览器调试端口解析：环境变量优先，其次复用同目录的 cdp_port 文件。"""

import os
from pathlib import Path


def resolve_cdp_port(directory: Path, env_name: str, default: int | None = 9222) -> int | None:
    value = os.environ.get(env_name, "").strip()
    if not value:
        try:
            value = (directory / "cdp_port").read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    if value.isdecimal() and 1 <= int(value) <= 65535:
        return int(value)
    return default
