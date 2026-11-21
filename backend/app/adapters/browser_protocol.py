"""服务器与本地浏览器 Agent 之间的 JSON 信封。

一次调用返回一个 JSON 对象，三选一：

    {"result": {...}}                         # 成功（无文件）
    {"result": {...}, "files": {"a.png": b64}}  # 成功（带证据/原件文件）
    {"error": {...}, "files": {...}}          # 失败（可带失败证据）

文件一律 base64 内联，避免 multipart 的解析复杂度；浏览器证据与卖家精灵/POE 原件
都受现有大小上限约束，v1 用 base64 可接受。错误码在两端用同一套 encode/raise 还原。
"""

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from app.domain.errors import DomainError
from app.services.search_policy import SearchFailure


def b64_files(files: Mapping[str, bytes]) -> dict[str, str]:
    return {name: base64.b64encode(data).decode("ascii") for name, data in files.items()}


def decode_files(payload: Mapping[str, Any]) -> dict[str, bytes]:
    files_raw = payload.get("files")
    if not isinstance(files_raw, dict):
        return {}
    files = cast(dict[str, object], files_raw)
    decoded: dict[str, bytes] = {}
    for name, value in files.items():
        if isinstance(value, str):
            try:
                decoded[name] = base64.b64decode(value)
            except ValueError:
                # 坏数据不阻塞结果解析；调用方按缺失文件处理。
                continue
    return decoded


def write_files(files: Mapping[str, bytes], directory: Path) -> None:
    """把 Agent 返回的文件落盘到服务器自己的证据目录，文件名与 result/error 引用一致。"""
    if not files:
        return
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        if name and "/" not in name and "\\" not in name:
            (directory / name).write_bytes(data)


def encode_error(error: DomainError) -> dict[str, object]:
    payload: dict[str, object] = {
        "status_code": error.status_code,
        "code": error.code,
        "message": error.message,
    }
    # SearchFailure 的失败证据文件名与阶段，随错误一起回传，服务器端据此还原。
    for field in ("stage", "screenshot", "diagnostic"):
        value = getattr(error, field, "")
        if value:
            payload[field] = str(value)
    retry_after = getattr(error, "retry_after", 0)
    if retry_after:
        payload["retry_after"] = float(retry_after)
    return payload


def raise_error(payload: Mapping[str, Any]) -> None:
    status = int(payload.get("status_code", 502))
    code = str(payload.get("code", "AGENT_ERROR"))
    message = str(payload.get("message", "浏览器 Agent 执行失败"))
    stage = str(payload.get("stage", "") or "")
    screenshot = str(payload.get("screenshot", "") or "")
    diagnostic = str(payload.get("diagnostic", "") or "")
    retry_after = float(payload.get("retry_after", 0) or 0)
    if stage or screenshot or diagnostic or retry_after:
        raise SearchFailure(
            code,
            message,
            stage=stage,
            screenshot=screenshot,
            diagnostic=diagnostic,
            retry_after=retry_after,
        ) from None
    raise DomainError(status, code, message) from None
