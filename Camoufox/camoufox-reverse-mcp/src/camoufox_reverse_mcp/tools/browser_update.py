# Module: MCP-facing wrapper around the transactional Camoufox browser updater.
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ..runtime import project_root
from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response, truncate_str

_UPDATER: ModuleType | None = None


def _load_updater() -> ModuleType:
    global _UPDATER
    if _UPDATER is not None:
        return _UPDATER

    root = project_root()
    script_path = root / "scripts" / "update_camoufox_reverse.py"
    if not script_path.is_file():
        raise RuntimeError(f"Camoufox updater script not found: {script_path}")

    spec = importlib.util.spec_from_file_location(
        "camoufox_reverse_mcp_update_camoufox_reverse",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Camoufox updater script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _UPDATER = module
    return module


def _normalize_tag(version: str | None) -> str | None:
    if version is None:
        return None
    value = version.strip()
    if not value:
        return None
    return value if value.startswith("v") else f"v{value}"


def _release_payload(release: Any) -> dict[str, Any]:
    return {
        "tag": release.tag,
        "version": release.version,
        "release": release.release,
        "version_string": release.version_string,
        "asset_url": release.asset_url,
        "sha256": release.sha256,
        "size": release.size,
    }


def _resolve_paths(updater: ModuleType, browser_root: str | None) -> tuple[Path, Path]:
    mcp_root = updater.mcp_root_from_script()
    resolved_browser_root = updater.derive_browser_root(mcp_root, browser_root)
    return mcp_root, resolved_browser_root


def _check_update(version: str | None, browser_root: str | None) -> dict[str, Any]:
    updater = _load_updater()
    tag = _normalize_tag(version)
    mcp_root, resolved_browser_root = _resolve_paths(updater, browser_root)
    updater.validate_browser_root(resolved_browser_root)
    release = updater.fetch_release(tag)
    current = updater.read_current_version(resolved_browser_root) or "unknown"
    protected_names = sorted(updater.protected_entry_names(resolved_browser_root, mcp_root))
    journal_path = resolved_browser_root / f".{resolved_browser_root.name}.update.json"
    archive_path = updater.release_archive_cache_path(mcp_root, release)
    running = updater.running_browser_processes()

    return {
        "supported": sys.platform == "win32",
        "update_available": current != release.version_string,
        "current_version": current,
        "target_version": release.version_string,
        "latest_version": release.version_string if tag is None else None,
        "browser_root": str(resolved_browser_root),
        "mcp_root": str(mcp_root),
        "download_archive_path": str(archive_path),
        "protected_paths": protected_names,
        "release": _release_payload(release),
        "running_browser_processes": running,
        "stale_update_journal": str(journal_path) if journal_path.exists() else None,
    }


async def _run_updater(args: list[str], cwd: Path, timeout_s: int = 1800) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Camoufox browser update timed out")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


@mcp.tool()
async def check_browser_update(
    version: str | None = None,
    browser_root: str | None = None,
) -> dict:
    """Check whether the local camoufox-reverse browser binary can be updated.

    Args:
        version: Optional release version or tag, e.g. "135.0.1-beta.25" or
          "v135.0.1-beta.25". Omit to check GitHub latest.
        browser_root: Optional Camoufox browser root. Defaults to the MCP parent
          or CAMOUFOX_EXECUTABLE_PATH-derived root.
    """
    try:
        return _check_update(version, browser_root)
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def update_browser_binary(
    version: str | None = None,
    browser_root: str | None = None,
    archive_path: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    close_running_browser: bool = False,
    keep_backup: bool = False,
) -> dict:
    """Safely overwrite-update the camoufox-reverse browser binary.

    This tool delegates to the existing transactional updater. The updater keeps
    camoufox-reverse-mcp, Profiles, .camoufox-data and .camoufox-runtime protected.

    Args:
        version: Optional release version or tag. Omit to install GitHub latest.
        browser_root: Optional Camoufox browser root.
        archive_path: Optional local camoufox-reverse-win.x86_64.zip path. When
          provided, skips network download but still verifies size/SHA-256 and
          package version before overwrite-installing.
        force: Reinstall even when the target version is already installed.
        dry_run: Report the planned update without downloading or writing files.
        close_running_browser: Close the MCP-managed browser before updating.
        keep_backup: Keep the old browser backup after a successful update.
    """
    try:
        check = _check_update(version, browser_root)
        if dry_run:
            return {
                **check,
                "status": "dry_run",
                "note": "Dry run did not download or write files.",
            }
        if not check["update_available"] and not force:
            return {**check, "status": "skipped"}

        if browser_manager.browser is not None:
            if not close_running_browser:
                return {
                    **check,
                    "status": "blocked",
                    "reason": (
                        "The MCP-managed Camoufox browser is running. "
                        "Re-run with close_running_browser=true before updating."
                    ),
                }
            await browser_manager.close()

        updater = _load_updater()
        remaining_processes = updater.running_browser_processes()
        if remaining_processes:
            return {
                **check,
                "status": "blocked",
                "reason": "Close Camoufox browser processes before updating.",
                "running_browser_processes": remaining_processes,
            }

        mcp_root = Path(check["mcp_root"])
        script_path = mcp_root / "scripts" / "update_camoufox_reverse.py"
        command = [sys.executable, str(script_path), "--yes"]
        if browser_root:
            command.extend(["--browser-root", browser_root])
        tag = _normalize_tag(version)
        if tag:
            command.extend(["--tag", tag])
        if archive_path:
            command.extend(["--archive", archive_path])
        if force:
            command.append("--force")
        if keep_backup:
            command.append("--keep-backup")

        returncode, stdout, stderr = await _run_updater(command, cwd=mcp_root)
        after = _check_update(version, browser_root)
        payload = {
            **after,
            "status": "updated" if returncode == 0 else "failed",
            "returncode": returncode,
            "stdout": truncate_str(stdout, 12000),
            "stderr": truncate_str(stderr, 12000),
        }
        if returncode != 0:
            return error_response(
                "Camoufox browser update failed",
                details=payload,
            )
        return payload
    except Exception as e:
        return error_response(e)
