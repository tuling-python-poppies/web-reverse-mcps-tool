"""Unified deprecation warning mechanism for v0.9.0 tool migration.

Old tool functions are kept as plain Python async functions (not registered
as MCP tools) so that any existing code importing them still works. But
they are NOT visible to the AI model — only the new merged tools are.
"""
from __future__ import annotations

import time

_GLOBAL_DEPRECATION_LOG: list[dict] = []
_MAX_LOG = 100


def log_deprecated_call(tool_name: str, alternative: str, removed_in: str = "0.10.0") -> str:
    """Record a deprecated tool call and return the warning message."""
    msg = (
        f"\u26a0\ufe0f Tool '{tool_name}' is deprecated and will be removed in "
        f"v{removed_in}. Use: {alternative}"
    )
    _GLOBAL_DEPRECATION_LOG.append({
        "tool": tool_name,
        "alternative": alternative,
        "removed_in": removed_in,
        "called_at": time.time(),
    })
    if len(_GLOBAL_DEPRECATION_LOG) > _MAX_LOG:
        _GLOBAL_DEPRECATION_LOG.pop(0)
    return msg


def get_deprecation_log() -> list[dict]:
    """Return recent deprecation calls (for check_environment reporting)."""
    return list(_GLOBAL_DEPRECATION_LOG)
