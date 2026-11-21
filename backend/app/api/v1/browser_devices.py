"""访问者浏览器设备登记与连通性检查。

页面在这里登记「我这台电脑上的 Agent 地址 + Token」，之后启动任务时选择它。
服务器只保存地址与凭据，不接触对方的紫鸟账号。
"""

import os
from pathlib import Path
from typing import cast

from fastapi import APIRouter
from pydantic import Field

from app.adapters.remote_browser import AgentChannel, AgentClient
from app.domain.errors import DomainError
from app.domain.schemas import Contract
from app.services.browser_devices import BrowserDeviceStore


class RegisterDeviceRequest(Contract):
    display_name: str = Field(min_length=1, max_length=200)
    agent_url: str = Field(min_length=8, max_length=500)
    agent_token: str = Field(min_length=32, max_length=500)
    owner_id: str = Field(default="", max_length=200)


class SelectStoreRequest(Contract):
    store_id: str = Field(min_length=1, max_length=40)


class VerifyResult(Contract):
    device: dict[str, object]
    reachable: bool
    detail: str


def _check(store: BrowserDeviceStore, device_id: str) -> VerifyResult:
    device = store.get(device_id)
    if device.agent_url.startswith("channel://"):
        # 纯反向长连接设备没有可探测的 HTTP 地址；在线状态由连接本身决定。
        return VerifyResult(
            device=device.public(),
            reachable=False,
            detail="等待采集端连上来（反向长连接）",
        )
    client = AgentClient(device.agent_url, token=device.agent_token)
    try:
        payload = client.call("GET", "/v1/status")
    except DomainError as error:
        store.mark(device_id, status="offline", agent_version=device.agent_version)
        return VerifyResult(
            device=store.get(device_id).public(),
            reachable=False,
            detail=error.message,
        )
    finally:
        client.close()
    version = str(cast(dict[str, object], payload).get("version", ""))
    state = str(cast(dict[str, object], payload).get("state", "online"))
    updated = store.mark(
        device_id, status="online" if state in ("online", "busy") else state, agent_version=version
    )
    return VerifyResult(device=updated.public(), reachable=True, detail=f"Agent 在线（{version}）")



def _version_tuple(value: str) -> tuple[int, ...]:
    """把 1.4.12 解析成可比较的版本号；无法解析时返回空元组。"""
    parts: list[int] = []
    for chunk in value.strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _current_release_version() -> str:
    """读取服务器上已发布的采集器版本（version.txt 第一段）。"""
    root = Path(os.getenv("KEYWORD_STORAGE_ROOT", "/app/data"))
    try:
        text = (root / "agent" / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.split()[0] if text else ""

def build_browser_device_router(
    store: BrowserDeviceStore, channel: AgentChannel | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/browser/devices", tags=["browser-devices"])

    @router.get("")
    def list_devices() -> list[dict[str, object]]:
        return [device.public() for device in store.list_devices()]

    @router.post("", status_code=201)
    def register(data: RegisterDeviceRequest) -> VerifyResult:
        device = store.register(
            display_name=data.display_name,
            agent_url=data.agent_url,
            agent_token=data.agent_token,
            owner_id=data.owner_id,
        )
        # 登记当场就探一次，避免把不可达地址存进去、等到跑任务才失败。
        return _check(store, device.device_id)

    @router.post("/{device_id}/verify")
    def verify(device_id: str) -> VerifyResult:
        return _check(store, device_id)

    @router.get("/versions")
    def versions() -> dict[str, object]:
        """当前发布的采集器版本；页面据此提示过旧设备升级。"""
        return {"current": _current_release_version()}

    @router.get("/{device_id}/version")
    def device_version(device_id: str) -> dict[str, object]:
        """对比设备上的采集器版本与当前发布版本，判断是否需要升级。"""
        try:
            device = store.get(device_id)
        except DomainError:
            raise DomainError(404, "BROWSER_DEVICE_NOT_FOUND", "未找到该浏览器设备") from None
        current = _current_release_version()
        running = str(device.agent_version or "").strip()
        outdated = bool(running and current and _version_tuple(running) < _version_tuple(current))
        return {
            "device_id": device_id,
            "running": running,
            "current": current,
            "outdated": outdated,
        }
    @router.get("/{device_id}/stores")
    def list_stores(device_id: str) -> dict[str, object]:
        """列出访问者电脑上的紫鸟店铺，供页面让用户选择。"""
        if channel is None:
            raise DomainError(503, "AGENT_CHANNEL_OFFLINE", "长连接通道未启用")
        if not channel.is_connected(device_id):
            raise DomainError(503, "AGENT_CHANNEL_OFFLINE", "该设备上的采集端当前离线")
        payload = channel.call(device_id, "GET", "/v1/ziniao/stores", None, timeout=30.0)
        if isinstance(payload, dict) and payload.get("error"):
            raise DomainError(503, "ZINIAO_STORE_LIST_FAILED", str(payload.get("message") or "读取紫鸟店铺失败"))
        return payload

    @router.post("/{device_id}/stores")
    def select_store(device_id: str, data: SelectStoreRequest) -> dict[str, object]:
        """在访问者电脑上选定紫鸟店铺；记住后采集连这个店铺。"""
        if channel is None:
            raise DomainError(503, "AGENT_CHANNEL_OFFLINE", "长连接通道未启用")
        if not channel.is_connected(device_id):
            raise DomainError(503, "AGENT_CHANNEL_OFFLINE", "该设备上的采集端当前离线")
        payload = channel.call(
            device_id, "POST", "/v1/ziniao/store", {"store_id": data.store_id}, timeout=30.0
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise DomainError(503, "ZINIAO_STORE_SELECT_FAILED", str(payload.get("message") or "选定紫鸟店铺失败"))
        return payload
    @router.delete("/{device_id}", status_code=200)
    def remove(device_id: str) -> dict[str, object]:
        store.delete(device_id)
        return {"ok": True}

    return router
