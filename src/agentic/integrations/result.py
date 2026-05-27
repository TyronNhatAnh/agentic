from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error_code: str | None = None
    user_message: str | None = None
    retryable: bool = False

    @classmethod
    def success(cls, data: Any) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error_code: str, user_message: str, retryable: bool = False) -> "ToolResult":
        return cls(ok=False, error_code=error_code, user_message=user_message, retryable=retryable)

    def display(self) -> str:
        if not self.ok:
            return self.user_message or ""
        # Structured successes carry a user-facing "message" key alongside machine
        # fields (e.g. worktree_path). Render only the message to Slack.
        if isinstance(self.data, dict):
            return str(self.data.get("message", ""))
        return self.data


def classify_exception(exc: BaseException, *, service: str) -> ToolResult:
    """Map raw exceptions to a deterministic ToolResult. Never include raw stacktrace text."""
    if isinstance(exc, httpx.TimeoutException):
        return ToolResult.failure("TIMEOUT", f"{service} timeout, thử lại sau.", retryable=True)
    if isinstance(exc, httpx.NetworkError):
        return ToolResult.failure("NETWORK", f"Không kết nối được {service}.", retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        s = exc.response.status_code
        if s in (401, 403):
            return ToolResult.failure("AUTH", f"{service} từ chối auth (token sai hoặc hết quyền).")
        if s == 404:
            return ToolResult.failure("NOT_FOUND", f"{service}: không tìm thấy resource.")
        if s == 429:
            return ToolResult.failure("RATE_LIMIT", f"{service} rate-limit, đợi rồi thử lại.", retryable=True)
        if 500 <= s < 600:
            return ToolResult.failure("SERVER", f"{service} lỗi server ({s}).", retryable=True)
        return ToolResult.failure("HTTP", f"{service} HTTP {s}.")
    if isinstance(exc, (KeyError, ValueError)):
        return ToolResult.failure("VALIDATION", f"Thiếu hoặc sai field: {exc}.")
    if isinstance(exc, RuntimeError):
        return ToolResult.failure("CONFIG", str(exc))
    return ToolResult.failure("UNKNOWN", f"{service} lỗi bất ngờ.")
