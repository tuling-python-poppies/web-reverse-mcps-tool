# 模块说明: MCP 进程启动入口,负责预加载 Camoufox、修补 Playwright 并启动 FastMCP。
import argparse
import json
import os
from pathlib import Path

from .runtime import configure_runtime_environment


def _ensure_version_file(browser_dir: Path) -> None:
    version_file = browser_dir / "version.json"
    if version_file.exists():
        return

    app_ini = browser_dir / "application.ini"
    if not app_ini.exists():
        return

    raw_version = None
    for line in app_ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("Version="):
            raw_version = line.split("=", 1)[1].strip()
            break

    if not raw_version:
        return

    if "-" in raw_version:
        version, release = raw_version.split("-", 1)
    else:
        version, release = raw_version, "0"

    version_file.write_text(
        json.dumps({"version": version, "release": release}),
        encoding="utf-8",
    )


def _configure_local_camoufox_defaults() -> None:
    """Pin Camoufox/Playwright data under the browser root (MCP parent).

    Layout::
        {browser_root}/
          camoufox.exe
          Profiles/default
          .camoufox-data          # CAMOUFOX_DATA_DIR
          .camoufox-runtime       # MCP runtime
          camoufox-reverse-mcp/
    """
    from .runtime import browser_root

    browser_dir = browser_root()
    exe_path = None
    for name in ("camoufox.exe", "camoufox-bin"):
        candidate = browser_dir / name
        if candidate.is_file():
            exe_path = candidate
            break
    if exe_path is not None:
        _ensure_version_file(browser_dir)
    configure_runtime_environment(exe_path)


_configure_local_camoufox_defaults()

# CRITICAL: import camoufox BEFORE entering asyncio event loop.
# camoufox/__init__.py imports playwright.sync_api at top level.
# If that import happens inside a running asyncio loop (e.g. when
# launch_browser lazily triggers `from camoufox.async_api import ...`),
# Playwright's sync bootstrap deadlocks for 60s+.
# Pre-importing here ensures the module is cached in sys.modules
# before FastMCP starts its event loop.
import camoufox  # noqa: F401

from .server import mcp
from ._playwright_patch import patch_playwright_pageerror


def main():
    # Fix the Playwright Firefox-driver pageError crash (issue #5) before the
    # browser is ever launched. No-op on Playwright versions without the bug.
    patch_playwright_pageerror()

    parser = argparse.ArgumentParser(description="Camoufox Reverse Engineering MCP Server")
    parser.add_argument("--proxy", type=str, help="Proxy server URL (e.g. http://127.0.0.1:7890)")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--os", type=str, default="auto",
                        choices=["auto", "windows", "macos", "linux"],
                        help="OS fingerprint to emulate (default: auto-detect host OS)")
    parser.add_argument("--locale", type=str, default="auto",
                        help="Browser locale, e.g. zh-CN, en-US (default: auto-detect)")
    parser.add_argument("--geoip", action="store_true", help="Enable GeoIP inference from proxy")
    parser.add_argument("--humanize", action="store_true", help="Enable humanized mouse movement")
    parser.add_argument("--block-images", action="store_true", help="Block image loading")
    parser.add_argument("--block-webrtc", action="store_true", help="Block WebRTC")
    parser.add_argument("--window-size", type=str, default=None,
                        help="Initial window size as WIDTHxHEIGHT (e.g. 1440x900). "
                             "Applied only with --fixed-viewport; dynamic no_viewport "
                             "mode uses the native resizable window.")
    parser.add_argument("--fixed-viewport", type=str, default=None,
                        help="Use a fixed emulated viewport WIDTHxHEIGHT instead of "
                             "tracking the real window. Omit this to keep the default "
                             "no_viewport behaviour that fixes the headful black-border bug.")
    args = parser.parse_args()

    def _parse_dims(value: str | None, flag: str):
        if not value:
            return None
        try:
            w, h = value.lower().split("x", 1)
            return (int(w), int(h))
        except ValueError:
            parser.error(f"{flag} must be WIDTHxHEIGHT, e.g. 1440x900 (got {value!r})")

    window = _parse_dims(args.window_size, "--window-size")
    fixed_vp = _parse_dims(args.fixed_viewport, "--fixed-viewport")

    from .browser import BrowserManager
    BrowserManager.default_config = {
        "proxy": {"server": args.proxy} if args.proxy else None,
        "headless": args.headless,
        "os": args.os,
        "locale": args.locale,
        "geoip": args.geoip,
        "humanize": args.humanize,
        "block_images": args.block_images,
        "block_webrtc": args.block_webrtc,
        "window": window,
        "viewport": ({"width": fixed_vp[0], "height": fixed_vp[1]} if fixed_vp else None),
        "no_viewport": fixed_vp is None,
    }

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
