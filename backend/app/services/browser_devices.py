"""访问者浏览器设备登记表。

每个使用者在**自己电脑**上跑一个 Agent（带自己的紫鸟），向服务器登记一个可达地址
和一枚 Token。工作流启动时绑定其中一台，之后该任务的所有浏览器操作都只发往这台设备，
绝不会借用别人的紫鸟。

存储用单个 JSON 文件而不是数据库表：设备数量是个位数、部署是单进程，避免为此改迁移；
写入走临时文件 + 原子替换，并在 POSIX 上收紧到 0600（文件里含调用凭据）。
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

from app.domain.errors import DomainError

MIN_TOKEN_LENGTH = 32


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BrowserDevice:
    device_id: str
    display_name: str
    agent_url: str
    agent_token: str
    owner_id: str
    status: str
    agent_version: str
    created_at: str
    updated_at: str
    last_seen_at: str | None = None

    def public(self) -> dict[str, object]:
        """给页面看的安全字段：绝不回传 Token。"""
        payload = asdict(self)
        payload.pop("agent_token", None)
        payload["registered"] = bool(self.agent_url)
        return payload


def validate_device_input(agent_url: str, agent_token: str) -> tuple[str, str]:
    url = agent_url.strip().rstrip("/")
    token = agent_token.strip()
    if url.startswith("channel://"):
        # 纯反向长连接设备：没有 HTTP 地址，客户端连上来之后才能采集。
        url = "channel://" + (url[len("channel://") :] or "agent")
    else:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DomainError(
                422,
                "BROWSER_DEVICE_URL_INVALID",
                "Agent 地址必须是 http(s) 完整地址，或 channel:// 表示只用反向长连接",
            )
    if len(token) < MIN_TOKEN_LENGTH:
        raise DomainError(
            422, "BROWSER_DEVICE_TOKEN_WEAK", f"Agent Token 至少 {MIN_TOKEN_LENGTH} 个字符"
        )
    return url, token


class BrowserDeviceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = RLock()

    # ---- 持久化 -------------------------------------------------------
    def _load(self) -> list[BrowserDevice]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 文件损坏时按空表启动，人工重新登记即可；不能让整个服务起不来。
            return []
        if not isinstance(raw, dict):
            return []
        rows = raw.get("devices")
        if not isinstance(rows, list):
            return []
        devices: list[BrowserDevice] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = {
                key: str(row.get(key, ""))
                for key in (
                    "device_id",
                    "display_name",
                    "agent_url",
                    "agent_token",
                    "owner_id",
                    "status",
                    "agent_version",
                    "created_at",
                    "updated_at",
                )
            }
            if not values["device_id"] or not values["agent_url"]:
                continue
            last_seen = row.get("last_seen_at")
            devices.append(
                BrowserDevice(
                    **values,
                    last_seen_at=str(last_seen) if last_seen else None,
                )
            )
        return devices

    def _save(self, devices: list[BrowserDevice]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"devices": [asdict(d) for d in devices]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    # ---- 读写 ---------------------------------------------------------
    def register(
        self,
        *,
        display_name: str,
        agent_url: str,
        agent_token: str,
        owner_id: str = "",
    ) -> BrowserDevice:
        url, token = validate_device_input(agent_url, agent_token)
        name = display_name.strip() or "未命名设备"
        stamp = _now()
        with self.lock:
            devices = self._load()
            # 同一地址重复登记视为更新凭据，避免页面刷新造出一堆重复设备。
            existing = next((d for d in devices if d.agent_url == url), None)
            if existing is not None:
                existing.display_name = name
                existing.agent_token = token
                existing.owner_id = owner_id or existing.owner_id
                existing.updated_at = stamp
                self._save(devices)
                return existing
            device = BrowserDevice(
                device_id=str(uuid4()),
                display_name=name,
                agent_url=url,
                agent_token=token,
                owner_id=owner_id,
                status="unknown",
                agent_version="",
                created_at=stamp,
                updated_at=stamp,
            )
            devices.append(device)
            self._save(devices)
            return device

    def list_devices(self) -> list[BrowserDevice]:
        with self.lock:
            return sorted(self._load(), key=lambda d: d.created_at)

    def get(self, device_id: str) -> BrowserDevice:
        with self.lock:
            device = next((d for d in self._load() if d.device_id == device_id), None)
        if device is None:
            raise DomainError(404, "BROWSER_DEVICE_NOT_FOUND", "未找到该浏览器设备，请重新登记")
        return device

    def mark(self, device_id: str, *, status: str, agent_version: str) -> BrowserDevice:
        with self.lock:
            devices = self._load()
            device = next((d for d in devices if d.device_id == device_id), None)
            if device is None:
                raise DomainError(404, "BROWSER_DEVICE_NOT_FOUND", "未找到该浏览器设备")
            device.status = status
            device.agent_version = agent_version
            device.last_seen_at = _now()
            device.updated_at = device.last_seen_at
            self._save(devices)
            return device

    def delete(self, device_id: str) -> None:
        with self.lock:
            devices = self._load()
            remaining = [d for d in devices if d.device_id != device_id]
            if len(remaining) == len(devices):
                raise DomainError(404, "BROWSER_DEVICE_NOT_FOUND", "未找到该浏览器设备")
            self._save(remaining)
