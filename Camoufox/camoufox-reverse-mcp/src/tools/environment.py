# 模块说明: 汇总 MCP 运行环境、依赖和浏览器状态的自检信息。
"""Environment self-check tool (v1.0.0: session fields removed)."""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..runtime import executable_firefox_version, project_root
from ..server import mcp, browser_manager
from ..property_trace import CACHE_DIR, CONTROL_DIR, TRACES_DIR

_INSTALL_HINT = (
    "Download camoufox-reverse from "
    "https://github.com/WhiteNightShadow/camoufox-reverse/releases "
    "and set CAMOUFOX_EXECUTABLE_PATH, or place camoufox.exe beside the "
    "camoufox-reverse-mcp project (sibling layout)."
)


def _control_file_pid(path: Path) -> int | None:
    stem = path.stem
    if not stem.startswith("control-"):
        return None
    try:
        return int(stem.split("-", 1)[1])
    except (IndexError, ValueError):
        return None


def _windows_pid_is_running(pid: int) -> bool:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied generally means the process exists but is protected.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _camoufox_binary_names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("camoufox.exe",)
    if sys.platform == "darwin":
        return ("camoufox", "Camoufox")
    return ("camoufox",)


def _read_version_meta(browser_dir: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    version_file = browser_dir / "version.json"
    if version_file.is_file():
        try:
            data = json.loads(version_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "version" in data:
                    meta["version"] = str(data["version"])
                if "release" in data:
                    meta["release"] = str(data["release"])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if "version" not in meta:
        major = executable_firefox_version(browser_dir / _camoufox_binary_names()[0])
        if major is not None:
            meta["version"] = str(major)
    return meta


def _candidate_binary_paths() -> list[tuple[Path, str]]:
    """Return (path, source) candidates in priority order."""
    names = _camoufox_binary_names()
    out: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path, source: str) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        out.append((resolved, source))

    env_exe = os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip()
    if env_exe:
        add(Path(env_exe), "env")

    try:
        root = project_root()
    except Exception:
        root = None

    if root is not None:
        for name in names:
            add(root.parent / name, "sibling_layout")
            add(root / name, "project_root")
            add(root / "browser" / name, "project_browser")

    data_dir = os.environ.get("CAMOUFOX_DATA_DIR", "").strip()
    if data_dir:
        base = Path(data_dir)
        for name in names:
            add(base / name, "data_dir")
            add(base.parent / name, "data_dir_parent")

    return out


def resolve_camoufox_binary() -> dict[str, Any]:
    """Locate camoufox executable. installed semantics use binary.available only."""
    for path, source in _candidate_binary_paths():
        if path.is_file():
            browser_dir = path.parent
            meta = _read_version_meta(browser_dir)
            return {
                "available": True,
                "path": str(path),
                "version": meta.get("version"),
                "release": meta.get("release"),
                "source": source,
            }
    return {
        "available": False,
        "path": None,
        "version": None,
        "release": None,
        "source": None,
    }


def _trace_history() -> dict[str, Any]:
    ctrl_files = list(CONTROL_DIR.glob("control-*.cmd")) if CONTROL_DIR.exists() else []
    trace_files = list(TRACES_DIR.glob("*.jsonl")) if TRACES_DIR.exists() else []
    active_ctrl_files: list[tuple[Path, int]] = []
    stale_ctrl_files: list[Path] = []
    for f in ctrl_files:
        pid = _control_file_pid(f)
        if pid is not None and _pid_is_running(pid):
            active_ctrl_files.append((f, pid))
        else:
            stale_ctrl_files.append(f)

    history = {
        "control_files": len(ctrl_files),
        "active_control_files": len(active_ctrl_files),
        "stale_control_files": len(stale_ctrl_files),
        "trace_files": len(trace_files),
        "cache_dir": str(CACHE_DIR),
    }
    active_pids = [pid for _, pid in active_ctrl_files]
    note = None
    if active_ctrl_files:
        note = None
    elif stale_ctrl_files and not active_ctrl_files:
        note = (
            "Trace control files exist, but their browser processes are "
            "not running. Historical trace files can still be queried."
        )
    return {
        "active": bool(active_ctrl_files),
        "active_pids": active_pids,
        "history": history,
        "note": note,
        "has_history": bool(ctrl_files or trace_files),
    }


def _build_camoufox_reverse_section() -> dict[str, Any]:
    """
    installed := binary.available (not historical trace cache).

    Also returns nested binary/trace plus flat legacy keys for compatibility.
    """
    binary = resolve_camoufox_binary()
    trace_info = _trace_history()
    history = trace_info["history"]
    installed = bool(binary.get("available"))

    # capable: binary present; history is supporting evidence only
    if installed:
        capable = True
        capable_reason = f"binary_present:{binary.get('source') or 'unknown'}"
    elif trace_info["has_history"]:
        capable = None  # unknown: traces exist but binary not found now
        capable_reason = "history_only_no_binary"
    else:
        capable = False
        capable_reason = "binary_missing"

    section: dict[str, Any] = {
        # Compatibility: installed means binary is available, not "has run before"
        "installed": installed,
        "binary": binary,
        "trace": {
            "capable": capable,
            "capable_reason": capable_reason,
            "active": trace_info["active"],
            "active_pids": trace_info["active_pids"],
            "history": history,
        },
        # Flat legacy fields
        "trace_active": trace_info["active"],
        "control_files": history["control_files"],
        "active_control_files": history["active_control_files"],
        "stale_control_files": history["stale_control_files"],
        "trace_files": history["trace_files"],
        "cache_dir": history["cache_dir"],
    }
    if trace_info["active_pids"]:
        section["active_pids"] = trace_info["active_pids"]
    if trace_info["note"]:
        section["note"] = trace_info["note"]
    if not installed:
        section["install_hint"] = _INSTALL_HINT
        if trace_info["has_history"]:
            section["note"] = (
                (section.get("note") + " " if section.get("note") else "")
                + "Historical traces found, but camoufox binary was not resolved; "
                "installed=false until binary path is available."
            ).strip()
    return section


@mcp.tool()
async def check_environment() -> dict:
    """One-stop self-check of MCP environment, dependencies, and browser state.

    v1.0.0: session-related checks removed (session mechanism removed).
    Checks MCP version, critical dependencies (esprima, playwright),
    browser state (residuals, captures).

    camoufox_reverse.installed = binary available (not historical trace cache).

    Returns:
        dict with sections: mcp, deps, browser, camoufox_reverse, overall_ok, recommendations.
    """
    recommendations: list[str] = []

    # MCP version
    try:
        mod = importlib.import_module("camoufox_reverse_mcp")
        version = getattr(mod, "__version__", "unknown")
        parts = tuple(int(x) for x in version.split(".") if x.isdigit())
        version_ok = parts >= (1, 0, 0)
    except Exception:
        version = "unknown"
        version_ok = False
    if not version_ok:
        recommendations.append(f"MCP version is {version}, need >= 1.0.0.")

    # Dependencies
    deps: dict[str, dict] = {}
    for dep in ("esprima", "playwright"):
        try:
            m = importlib.import_module(dep)
            deps[dep] = {"installed": True, "version": getattr(m, "__version__", "unknown"), "ok": True}
        except ImportError:
            deps[dep] = {"installed": False, "version": None, "ok": False}

    # Browser state
    browser_state: dict[str, Any] = {"running": False}
    try:
        if browser_manager.browser is not None:
            browser_state["running"] = True
            ctx = browser_manager.contexts.get("default")
            pages = ctx.pages if ctx else []
            browser_state["page_count"] = len(pages)
            browser_state["persistent_scripts_count"] = len(browser_manager._persistent_scripts)
            browser_state["active_captures"] = browser_manager._capturing
            browser_state["captured_requests_count"] = len(browser_manager._network_requests)
            has_residuals = (
                browser_state["persistent_scripts_count"] > 0
                or browser_state["captured_requests_count"] > 0
            )
            browser_state["has_residuals"] = has_residuals
            if has_residuals:
                recommendations.append("Browser has residual state. Consider reset_browser_state().")
    except Exception as e:
        browser_state["error"] = str(e)

    overall_ok = version_ok and all(d["ok"] for d in deps.values())

    try:
        custom_browser = _build_camoufox_reverse_section()
        if not custom_browser.get("installed"):
            recommendations.append(
                "camoufox binary not found (installed=false). "
                "Set CAMOUFOX_EXECUTABLE_PATH or use sibling camoufox.exe layout."
            )
    except Exception as e:
        custom_browser = {"installed": False, "error": str(e), "install_hint": _INSTALL_HINT}

    return {
        "mcp": {"version": version, "version_ok": version_ok},
        "deps": deps,
        "browser": browser_state,
        "camoufox_reverse": custom_browser,
        "overall_ok": overall_ok,
        "recommendations": recommendations,
    }
