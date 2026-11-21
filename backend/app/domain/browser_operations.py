"""Persistent identity and status for one remote browser collection unit."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserOperation:
    operation_id: str
    workflow_id: str
    run_id: str
    node_key: str
    source: str
    idempotency_key: str
    status: str
    attempts: int
    auto_resume_remaining: int
    request_summary: str
    result_ref: str
    error_code: str
    created_at: str
    updated_at: str
