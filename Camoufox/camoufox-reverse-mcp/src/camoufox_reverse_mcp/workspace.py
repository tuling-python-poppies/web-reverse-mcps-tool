"""Constrain MCP file tools to an explicit local workspace."""
from __future__ import annotations

import os
import stat
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def workspace_root() -> Path:
    configured = os.environ.get("CAMOUFOX_WORKSPACE_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def resolve_workspace_path(raw_path: str, *, for_write: bool) -> Path:
    if not raw_path or not raw_path.strip():
        raise ValueError("path is required")
    root = workspace_root()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must stay inside workspace root: {root}") from exc

    current = root
    if _is_link_or_reparse(current):
        raise ValueError(f"workspace root cannot be a symlink or reparse point: {root}")
    relative_parts = resolved.relative_to(root).parts
    for part in relative_parts:
        current /= part
        if current.exists() and _is_link_or_reparse(current):
            raise ValueError(f"path crosses a symlink or reparse point: {current}")
    return resolved
