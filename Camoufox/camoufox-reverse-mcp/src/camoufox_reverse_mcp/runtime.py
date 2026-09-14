"""Project-local runtime paths for Camoufox and Playwright.

Default layout (browser_root = MCP parent / camoufox.exe directory)::

    {browser_root}/
      camoufox.exe
      addons/
      Profiles/default/          # portable user profile (-profile)
      .camoufox-data/            # CAMOUFOX_DATA_DIR
      .camoufox-runtime/         # MCP/Playwright runtime
      camoufox-reverse-mcp/      # this project
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

DATA_DIR_NAME = ".camoufox-data"
RUNTIME_DIR_NAME = ".camoufox-runtime"
PROFILES_DIR_NAME = "Profiles"
DEFAULT_PROFILE_NAME = "default"


def project_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    executable = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
    if executable:
        browser = Path(executable).expanduser().resolve().parent
        sibling_project = browser / "camoufox-reverse-mcp"
        if (sibling_project / "pyproject.toml").is_file():
            return sibling_project
        return browser
    return Path(tempfile.gettempdir()).resolve() / "camoufox-reverse-mcp"


def browser_root(start: Path | None = None) -> Path:
    """Resolve the Camoufox browser install root (parent of MCP by default)."""
    configured = os.environ.get("CAMOUFOX_BROWSER_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()

    executable = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
    if executable:
        return Path(executable).expanduser().resolve().parent

    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "camoufox.exe").is_file() or (candidate / "camoufox-bin").is_file():
            return candidate

    root = project_root(start)
    parent = root.parent
    if (parent / "camoufox.exe").is_file() or (parent / "camoufox-bin").is_file():
        return parent
    return parent


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runtime_root() -> Path:
    configured = os.environ.get("CAMOUFOX_REVERSE_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return browser_root() / RUNTIME_DIR_NAME


def data_dir() -> Path:
    configured = os.environ.get("CAMOUFOX_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return browser_root() / DATA_DIR_NAME


def profiles_dir() -> Path:
    return browser_root() / PROFILES_DIR_NAME


def default_profile_dir() -> Path:
    return profiles_dir() / DEFAULT_PROFILE_NAME


def ensure_default_profile_dir() -> Path:
    path = default_profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def instance_root() -> Path:
    return runtime_root() / "instances" / f"mcp-{os.getpid()}"


def configure_runtime_environment(executable_path: Path | None = None) -> dict[str, Path]:
    if executable_path is not None:
        browser = Path(executable_path).expanduser().resolve().parent
    else:
        browser = browser_root()

    if os.environ.get("CAMOUFOX_REVERSE_RUNTIME_DIR"):
        root = Path(os.environ["CAMOUFOX_REVERSE_RUNTIME_DIR"]).expanduser().resolve()
    else:
        root = browser / RUNTIME_DIR_NAME

    if os.environ.get("CAMOUFOX_DATA_DIR"):
        data = Path(os.environ["CAMOUFOX_DATA_DIR"]).expanduser().resolve()
    else:
        data = browser / DATA_DIR_NAME

    profiles = browser / PROFILES_DIR_NAME
    default_profile = profiles / DEFAULT_PROFILE_NAME
    instance = root / "instances" / f"mcp-{os.getpid()}"

    paths: dict[str, Path] = {
        "browser_root": browser,
        "data_dir": data,
        "profiles": profiles,
        "default_profile": default_profile,
        "root": root,
        "browser_downloads": root / "browser-downloads",
        "playwright_browsers": root / "playwright-browsers",
        "temp": root / "temp",
        "instance": instance,
        "cache": instance / "cache",
        "sessions": instance / "sessions",
    }

    for key in (
        "data_dir",
        "profiles",
        "default_profile",
        "root",
        "browser_downloads",
        "playwright_browsers",
        "temp",
        "instance",
        "cache",
        "sessions",
    ):
        paths[key].mkdir(parents=True, exist_ok=True)

    instances_root = root / "instances"
    cutoff = time.time() - 7 * 86400
    if instances_root.is_dir():
        for stale in instances_root.glob("mcp-*"):
            try:
                pid = int(stale.name.removeprefix("mcp-"))
                if (
                    stale != paths["instance"]
                    and not _pid_is_running(pid)
                    and stale.stat().st_mtime < cutoff
                ):
                    shutil.rmtree(stale)
            except (OSError, ValueError):
                pass

    os.environ["CAMOUFOX_REVERSE_RUNTIME_DIR"] = str(root)
    os.environ["CAMOUFOX_DATA_DIR"] = str(data)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(paths["playwright_browsers"])
    os.environ["CAMOUFOX_REVERSE_CACHE_DIR"] = str(paths["cache"])
    os.environ["TEMP"] = str(paths["temp"])
    os.environ["TMP"] = str(paths["temp"])

    exe = executable_path
    if exe is None:
        for name in ("camoufox.exe", "camoufox-bin"):
            candidate = browser / name
            if candidate.is_file():
                exe = candidate
                break
    if exe is not None:
        os.environ.setdefault(
            "CAMOUFOX_EXECUTABLE_PATH",
            str(Path(exe).expanduser().resolve()),
        )

    return paths


def create_browser_session_dir() -> Path:
    paths = configure_runtime_environment()
    return Path(tempfile.mkdtemp(prefix="browser-", dir=paths["sessions"]))


def executable_firefox_version(executable_path: str | os.PathLike[str]) -> int | None:
    browser_dir = Path(executable_path).expanduser().resolve().parent
    version_file = browser_dir / "version.json"
    if version_file.is_file():
        try:
            version = str(json.loads(version_file.read_text(encoding="utf-8"))["version"])
            return int(version.split(".", 1)[0])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass

    application_ini = browser_dir / "application.ini"
    if application_ini.is_file():
        try:
            for line in application_ini.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("Version="):
                    return int(line.split("=", 1)[1].strip().split(".", 1)[0])
        except (OSError, ValueError):
            pass
    return None
