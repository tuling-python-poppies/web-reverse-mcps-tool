"""Start a Camoufox Playwright server using the local reverse browser binary.

Compatible with Playwright 1.60+ (uses scripts/launchServer_pw160.js).
Does not download daijro/camoufox packages when CAMOUFOX_DATA_DIR / executable
are pinned to the browser root layout.

Usage:
  python scripts/start_camoufox_server.py
  python scripts/start_camoufox_server.py --headless
  python scripts/start_camoufox_server.py --os windows

Then attach from MCP:
  launch_browser(ws_endpoint="ws://...")
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

import orjson

SCRIPT_DIR = Path(__file__).resolve().parent
MCP_ROOT = SCRIPT_DIR.parent
SHIM = SCRIPT_DIR / "launchServer_pw160.js"
WS_RE = re.compile(r"ws://[^\s]+")


def _browser_root() -> Path:
    env = os.environ.get("CAMOUFOX_BROWSER_ROOT") or os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
    if env:
        path = Path(env).expanduser().resolve()
        return path if path.is_dir() else path.parent
    parent = MCP_ROOT.parent
    if (parent / "camoufox.exe").is_file() or (parent / "camoufox-bin").is_file():
        return parent
    return parent


def _configure_env(browser: Path, exe: Path) -> None:
    os.environ.setdefault("CAMOUFOX_BROWSER_ROOT", str(browser))
    os.environ.setdefault("CAMOUFOX_EXECUTABLE_PATH", str(exe))
    os.environ.setdefault("CAMOUFOX_DATA_DIR", str(browser / ".camoufox-data"))
    os.environ.setdefault("CAMOUFOX_REVERSE_RUNTIME_DIR", str(browser / ".camoufox-runtime"))
    Path(os.environ["CAMOUFOX_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["CAMOUFOX_REVERSE_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)


def _ff_version(browser: Path) -> int | None:
    version_file = browser / "version.json"
    if version_file.is_file():
        try:
            data = orjson.loads(version_file.read_bytes())
            return int(str(data["version"]).split(".", 1)[0])
        except Exception:
            return None
    return None


def _to_camel_case_dict(data: dict) -> dict:
    from camoufox.server import to_camel_case_dict

    return to_camel_case_dict(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start local Camoufox Playwright server")
    parser.add_argument("--headless", action="store_true", help="Run headless (default: headful)")
    parser.add_argument(
        "--os",
        default="windows" if os.name == "nt" else "linux",
        choices=["windows", "macos", "linux"],
    )
    parser.add_argument("--browser-root", default=None, help="Override browser root directory")
    args = parser.parse_args()

    browser = Path(args.browser_root).expanduser().resolve() if args.browser_root else _browser_root()
    exe_name = "camoufox.exe" if os.name == "nt" else "camoufox-bin"
    exe = browser / exe_name
    if not exe.is_file():
        print(f"error: browser executable not found: {exe}", file=sys.stderr)
        return 1
    if not SHIM.is_file():
        print(f"error: missing shim: {SHIM}", file=sys.stderr)
        return 1

    _configure_env(browser, exe)

    # Import after env is pinned so camoufox.pkgman.INSTALL_DIR is not needed
    # for a full fetch (we still exclude default addons and load local ones).
    from camoufox.addons import DefaultAddons
    from camoufox.server import get_nodejs
    from camoufox.utils import launch_options

    profile = browser / "Profiles" / "default"
    profile.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict = {
        "headless": bool(args.headless),
        "os": args.os,
        "executable_path": str(exe),
        "exclude_addons": list(DefaultAddons),
        "i_know_what_im_doing": True,
        # Playwright rejects -profile in args; use Mozilla profile env instead.
        "env": {
            "XRE_PROFILE_PATH": str(profile),
            "XRE_PROFILE_LOCAL_PATH": str(profile),
        },
    }
    version = _ff_version(browser)
    if version is not None:
        launch_kwargs["ff_version"] = version

    # Prefer bundled addons beside the executable
    addons = []
    for addon in DefaultAddons:
        addon_dir = browser / "addons" / addon.name
        if (addon_dir / "manifest.json").is_file():
            addons.append(str(addon_dir))
    if addons:
        launch_kwargs["addons"] = addons

    config = launch_options(**launch_kwargs)
    for key in list(config.keys()):
        if config[key] is None:
            config.pop(key)

    nodejs = get_nodejs()
    cwd = Path(nodejs).parent / "package"
    payload = base64.b64encode(orjson.dumps(_to_camel_case_dict(config))).decode()

    print(f"browser_root={browser}")
    print(f"executable={exe}")
    print(f"CAMOUFOX_DATA_DIR={os.environ['CAMOUFOX_DATA_DIR']}")
    print(f"profile={profile}")
    print("starting server (Playwright 1.60 shim)...", flush=True)

    proc = subprocess.Popen(
        [nodejs, str(SHIM)],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(payload)
    proc.stdin.close()

    endpoint = None
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            sys.stdout.write(line)
            sys.stdout.flush()
            match = WS_RE.search(line)
            if match and endpoint is None:
                endpoint = match.group(0)
                print(f"\n=== attach with ===\nlaunch_browser(ws_endpoint={endpoint!r})\n", flush=True)
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
