# 模块说明: 统一响应格式化、字符串截断和错误响应构造。
from __future__ import annotations

from typing import Any


def truncate_str(s: str, max_len: int = 5000) -> str:
    """Truncate a string to max_len with an indicator."""
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... ({len(s)} chars total)"


def error_response(
    error: Any,
    *,
    code: str | None = None,
    hint: str | None = None,
    details: Any | None = None,
) -> dict[str, Any]:
    """Build a consistent MCP error payload."""
    result: dict[str, Any] = {"error": str(error)}
    if code:
        result["code"] = code
    if hint:
        result["hint"] = hint
    if details is not None:
        result["details"] = details
    return result
