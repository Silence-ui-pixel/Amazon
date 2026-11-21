"""Public, secret-free status contract for the Windows browser Agent."""

from typing import Literal

from pydantic import BaseModel


class BrowserAgentStatus(BaseModel):
    state: Literal["online", "busy", "attention_required"] = "online"
    operation: str = ""
    operation_id: str = ""
    started_at: str | None = None
    last_error_code: str = ""
    version: str = "1"
