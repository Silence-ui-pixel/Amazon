"""大模型供应商配置：页面一键切换模型，写入受控目录后立即生效。

配置分两个文件：
- model-config.json 只放字符串（QwenConfig.load 用 dict[str, str] 严格校验）；
- model-catalog.json 放模型清单（字典数组）。
密钥单独放 model-key.txt，页面只回显脱敏值；留空表示沿用现有密钥。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter
from pydantic import Field

from app.domain.errors import DomainError
from app.domain.schemas import Contract
from app.services.qwen import QwenConfig

PRESET_MODELS: list[dict[str, str]] = [
    {"id": "mimo-v2.6-flash", "name": "MiMo V2.6 Flash", "context": "1M", "output": "128K"},
    {"id": "mimo-v2.6-pro", "name": "MiMo V2.6 Pro", "context": "1M", "output": "128K"},
    {
        "id": "openai/mimo-v2.6-flash",
        "name": "MiMo V2.6 Flash (OpenAI)",
        "context": "1M",
        "output": "128K",
    },
    {
        "id": "openai/mimo-v2.6-pro",
        "name": "MiMo V2.6 Pro (OpenAI)",
        "context": "1M",
        "output": "128K",
    },
]


class ModelEntry(Contract):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)
    context: str = Field(default="1M", max_length=50)
    output: str = Field(default="128K", max_length=50)


class ModelConfigUpdate(Contract):
    provider_id: str = Field(default="mimo", max_length=100)
    name: str = Field(default="Mimo 网关", max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    protocol: Literal["openai-chat", "anthropic", "openai-responses"] = "openai-chat"
    model: str = Field(min_length=1, max_length=200)
    vision_model: str = Field(default="", max_length=200)
    output_limit: int = Field(default=20000, ge=1000, le=200000)
    api_key: str = Field(default="", max_length=4000)
    models: list[ModelEntry] = Field(default_factory=list)


class TestResult(Contract):
    ok: bool
    message: str
    latency_ms: int
    models: list[str] = Field(default_factory=list)


def _providers_path(storage_root: Path) -> Path:
    return storage_root / "model-providers.json"


def _load_providers(storage_root: Path) -> list[dict[str, Any]]:
    path = _providers_path(storage_root)
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, list) and value:
                return [dict(item) for item in value if isinstance(item, dict)]
        except (OSError, ValueError):
            pass
    return []


def _save_providers(storage_root: Path, providers: list[dict[str, Any]]) -> None:
    _providers_path(storage_root).write_text(
        json.dumps(providers, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _check_url(base: str) -> None:
    url = urlsplit(base)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.query
        or url.fragment
    ):
        raise DomainError(422, "MODEL_CONFIG_INVALID", "接口地址必须是无凭据的 HTTPS 地址")



def _probe_request(protocol: str, model: str) -> tuple[dict[str, object], dict[str, str], str]:
    """按协议构造最小探测请求；返回体、请求头、端点路径。"""
    if protocol == "anthropic":
        return (
            {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
            {"x-api-key": "", "anthropic-version": "2023-06-01"},
            "/messages",
        )
    if protocol == "openai-responses":
        return (
            {"model": model, "input": "ping", "max_output_tokens": 16},
            {},
            "/responses",
        )
    return (
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16},
        {},
        "/chat/completions",
    )

def build_model_config_router(storage_root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/model-config", tags=["model-config"])
    config_path = storage_root / "model-config.json"
    catalog_path = storage_root / "model-catalog.json"
    key_path = storage_root / "model-key.txt"

    def read_config() -> dict[str, Any]:
        if config_path.is_file():
            try:
                return json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return {}

    def read_catalog() -> list[dict[str, str]]:
        if catalog_path.is_file():
            try:
                value = json.loads(catalog_path.read_text(encoding="utf-8"))
                if isinstance(value, list) and value:
                    return [dict(item) for item in value if isinstance(item, dict)]
            except (OSError, ValueError):
                pass
        return [dict(item) for item in PRESET_MODELS]

    def current_key(data: dict[str, Any] | None = None) -> str:
        info = data if data is not None else read_config()
        key_file = info.get("key_file") or os.getenv("DASHSCOPE_API_KEY_FILE", "")
        if key_file and Path(key_file).is_file():
            try:
                # 密钥文件可能是百炼导出的 CSV（带中文表头），必须复用官方解析。
                return QwenConfig.from_key_file(Path(key_file)).api_key
            except (OSError, DomainError, ValueError):
                pass
        return str(info.get("api_key") or os.getenv("DASHSCOPE_API_KEY", "")).strip()

    def view() -> dict[str, Any]:
        cfg = QwenConfig.load(storage_root)
        data = read_config()
        key = cfg.api_key
        masked = (
            key[:4] + "*" * 12 + key[-4:]
            if len(key) > 8
            else ("*" * 8 if key else "")
        )
        try:
            output_limit = int(data.get("output_limit", 20000))
        except (TypeError, ValueError):
            output_limit = 20000
        return {
            "provider_id": str(data.get("provider_id", "mimo")),
            "name": str(data.get("name", "Mimo 网关")),
            "base_url": cfg.base_url,
            "protocol": str(read_config().get("protocol") or "openai-chat"),
            "model": cfg.model,
            "vision_model": cfg.vision_model or "",
            "output_limit": output_limit,
            "api_key_masked": masked,
            "has_api_key": bool(key),
            "models": read_catalog(),
            "presets": [dict(item) for item in PRESET_MODELS],
            "providers": [dict(item) for item in _load_providers(storage_root)],
        }

    @router.get("")
    def get_config() -> dict[str, Any]:
        return view()

    @router.put("")
    def update(data: ModelConfigUpdate) -> dict[str, Any]:
        base = data.base_url.strip().rstrip("/")
        _check_url(base)
        old = read_config()
        payload: dict[str, str] = {
            "provider_id": data.provider_id,
            "name": data.name,
            "base_url": base,
            "protocol": data.protocol,
            "model": data.model,
            "vision_model": data.vision_model or data.model,
            "output_limit": str(data.output_limit),
        }
        if data.api_key.strip():
            key_path.write_text(data.api_key.strip(), encoding="utf-8")
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            payload["key_file"] = str(key_path)
        else:
            existing = old.get("key_file") or os.getenv("DASHSCOPE_API_KEY_FILE", "")
            if existing:
                payload["key_file"] = str(existing)
        config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 供应商列表：保存时把当前供应商 upsert 进去（按 provider_id）。
        providers = [
            item for item in _load_providers(storage_root)
            if item.get("provider_id") != data.provider_id
        ]
        providers.insert(0, {
            "provider_id": data.provider_id,
            "name": data.name,
            "base_url": base,
            "protocol": data.protocol,
            "model": data.model,
            "output_limit": data.output_limit,
            "has_key": bool(data.api_key.strip()) or bool(old.get("key_file") or os.getenv("DASHSCOPE_API_KEY_FILE", "")),
            "models": [item.model_dump() for item in data.models],
        })
        _save_providers(storage_root, providers)
        catalog = [item.model_dump() for item in data.models] or [
            dict(item) for item in PRESET_MODELS
        ]
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return view()

    @router.delete("/providers/{provider_id}")
    def delete_provider(provider_id: str) -> dict[str, Any]:
        active = str(read_config().get("provider_id") or "mimo")
        if provider_id == active:
            raise DomainError(422, "MODEL_PROVIDER_ACTIVE", "不能删除当前使用的供应商，请先切换")
        remaining = [
            item for item in _load_providers(storage_root)
            if item.get("provider_id") != provider_id
        ]
        _save_providers(storage_root, remaining)
        return {"providers": remaining, "active": active}

    @router.post("/providers/{provider_id}/clone")
    def clone_provider(provider_id: str) -> dict[str, Any]:
        source = next(
            (item for item in _load_providers(storage_root) if item.get("provider_id") == provider_id),
            None,
        )
        if source is None:
            raise DomainError(404, "MODEL_PROVIDER_NOT_FOUND", "未找到该供应商")
        new_id = f"{provider_id}-copy"
        existing_ids = {item.get("provider_id") for item in _load_providers(storage_root)}
        suffix = 2
        while new_id in existing_ids:
            new_id = f"{provider_id}-copy{suffix}"
            suffix += 1
        clone = dict(source)
        clone["provider_id"] = new_id
        clone["name"] = str(source.get("name") or new_id) + " 副本"
        clone["has_key"] = False  # 克隆不复制密钥，需单独填写。
        providers = [dict(item) for item in _load_providers(storage_root)]
        providers.insert(0, clone)
        _save_providers(storage_root, providers)
        return {"providers": providers, "cloned": new_id}

    @router.post("/providers/{provider_id}/activate")
    def activate_provider(provider_id: str) -> dict[str, Any]:
        hit = next(
            (item for item in _load_providers(storage_root) if item.get("provider_id") == provider_id),
            None,
        )
        if hit is None:
            raise DomainError(404, "MODEL_PROVIDER_NOT_FOUND", "未找到该供应商")
        data = read_config()
        data["provider_id"] = str(hit.get("provider_id"))
        data["name"] = str(hit.get("name") or provider_id)
        data["base_url"] = str(hit.get("base_url") or data.get("base_url") or "")
        data["protocol"] = str(hit.get("protocol") or "openai-chat")
        data["model"] = str(hit.get("model") or data.get("model") or "")
        data["vision_model"] = str(hit.get("model") or data.get("vision_model") or "")
        if not data["model"]:
            raise DomainError(422, "MODEL_CONFIG_INVALID", "该供应商缺少模型配置")
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return view()
    @router.post("/test")
    def test(data: ModelConfigUpdate) -> TestResult:
        base = data.base_url.strip().rstrip("/")
        _check_url(base)
        key = data.api_key.strip() or current_key()
        if not key:
            raise DomainError(422, "MODEL_KEY_MISSING", "请先填写 API Key")
        headers = {"Authorization": "Bearer " + key}
        if data.protocol == "anthropic":
            # Anthropic 用 x-api-key，不接受 Bearer。
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        started = time.monotonic()
        elapsed = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        try:
            response = httpx.get(
                base + "/models", headers=headers, timeout=25.0, trust_env=False
            )
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, (dict, list)):
                    rows = payload.get("data") if isinstance(payload, dict) else payload
                    if not isinstance(rows, list):
                        rows = []
                    names = [
                        str(item.get("id", ""))
                        for item in rows
                        if isinstance(item, dict) and item.get("id")
                    ]
                    return TestResult(
                        ok=True,
                        message=f"连接成功，发现 {len(names)} 个模型",
                        latency_ms=elapsed(),
                        models=names[:60],
                    )
            probe_body, probe_headers, probe_path = _probe_request(data.protocol, data.model)
            probe = httpx.post(
                base + probe_path,
                headers={**headers, **{k: v for k, v in probe_headers.items() if v}},
                json=probe_body,
                timeout=60.0,
                trust_env=False,
            )
            if probe.status_code == 200:
                return TestResult(
                    ok=True, message="连接成功（chat 探测）", latency_ms=elapsed()
                )
            return TestResult(
                ok=False,
                message=f"HTTP {probe.status_code}: {probe.text[:200]}",
                latency_ms=elapsed(),
            )
        except httpx.HTTPError as error:
            return TestResult(
                ok=False,
                message=f"{type(error).__name__}: {str(error)[:180]}",
                latency_ms=elapsed(),
            )
        except Exception as error:  # noqa: BLE001 - 探测接口不应抛 500
            return TestResult(
                ok=False,
                message=f"{type(error).__name__}: {str(error)[:180]}",
                latency_ms=elapsed(),
            )

    return router
