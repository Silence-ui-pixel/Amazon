"""同时管理临时前后端服务的生命周期，避免验收中服务提前退出。"""

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[2]
environment = {**os.environ, "PYTHONPATH": "."}
processes: list[subprocess.Popen[bytes]] = []
try:
    for command, cwd in [
        (
            [str(root / "backend/.venv/Scripts/python.exe"), "tests/serve_library_browser.py"],
            root / "backend",
        ),
        (
            [shutil.which("npm.cmd") or "npm.cmd", "run", "dev", "--", "--port", "5175"],
            root / "frontend",
        ),
    ]:
        processes.append(
            subprocess.Popen(
                command, cwd=cwd, env=environment, creationflags=subprocess.CREATE_NO_WINDOW
            )
        )
    for url in ("http://127.0.0.1:8014/__test__/seed", "http://127.0.0.1:5175/"):
        for attempt in range(60):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                if attempt == 59:
                    raise
                time.sleep(0.5)
    subprocess.run(
        ["node", sys.argv[1] if len(sys.argv) > 1 else "tests/browser_library_smoke.cjs"],
        cwd=root / "backend",
        check=True,
    )
finally:
    for process in processes:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
        )
