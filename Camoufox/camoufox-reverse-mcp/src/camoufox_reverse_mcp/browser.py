# 模块说明: 统一管理 Camoufox 浏览器生命周期、窗口行为、上下文和页面级监听器。
from __future__ import annotations

import asyncio
import json as _json
import os as _os
import platform
import shutil
import inspect
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence as SequenceABC
from pathlib import Path
from typing import Any, Sequence

from playwright.async_api import Page, BrowserContext, Browser as PlaywrightBrowser, Request as PWRequest, Response as PWResponse, ConsoleMessage

from .runtime import (
    create_browser_session_dir,
    ensure_default_profile_dir,
    executable_firefox_version,
)

MAX_LOG_SIZE = 2000
MAX_BODY_SIZE = 200_000
MAX_BODY_LOAD_SIZE = 5_000_000
MAX_NETWORK_BODY_BYTES = 25_000_000
MAX_REQUEST_BODY_SIZE = 50_000
MAX_CONSOLE_TEXT_SIZE = 20_000
MAX_PERSISTENT_TRACE_PATHS = 100
MAX_PERSISTENT_TRACES_PER_PATH = 1000
MAX_HEADER_VALUE_SIZE = 20_000
MAX_HEADERS_TOTAL_SIZE = 100_000
CAMOU_CONFIG_CHUNK_SIZE_WINDOWS = 2047
CAMOU_CONFIG_CHUNK_SIZE_OTHER = 32767

_DYNAMIC_DISPLAY_CONFIG_KEYS = (
    "window.outerWidth",
    "window.outerHeight",
    "window.screenX",
    "window.screenY",
    "screen.width",
    "screen.height",
    "screen.availWidth",
    "screen.availHeight",
    "screen.availLeft",
    "screen.availTop",
)


def _camou_config_keys(env: dict[str, Any]) -> list[str]:
    """Return CAMOU_CONFIG_N keys in numeric order."""
    prefix = "CAMOU_CONFIG_"

    def sort_key(key: str) -> tuple[int, str]:
        suffix = key[len(prefix):]
        try:
            return (int(suffix), key)
        except ValueError:
            return (10**9, key)

    return sorted([k for k in env if k.startswith(prefix)], key=sort_key)


def _camou_config_chunk_size() -> int:
    return CAMOU_CONFIG_CHUNK_SIZE_WINDOWS if detect_host_os() == "windows" else CAMOU_CONFIG_CHUNK_SIZE_OTHER


def _read_camou_config(env: dict[str, Any]) -> dict[str, Any]:
    config_keys = _camou_config_keys(env)
    if config_keys:
        raw = "".join(str(env[key]) for key in config_keys)
    elif "CAMOU_CONFIG" in env:
        raw = str(env["CAMOU_CONFIG"])
    else:
        return {}

    config = _json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("CAMOU_CONFIG must contain a JSON object")
    return config


def _write_camou_config(
    env: dict[str, Any],
    config: dict[str, Any],
    chunk_size: int | None = None,
) -> None:
    if chunk_size is None:
        chunk_size = _camou_config_chunk_size()
    if chunk_size <= 0:
        raise ValueError("CAMOU_CONFIG chunk size must be positive")

    for key in _camou_config_keys(env):
        env.pop(key, None)
    env.pop("CAMOU_CONFIG", None)

    encoded = _json.dumps(config, separators=(",", ":"))
    for index, start in enumerate(range(0, len(encoded), chunk_size), start=1):
        env[f"CAMOU_CONFIG_{index}"] = encoded[start:start + chunk_size]


def _strip_dynamic_window_fingerprint(
    env: dict[str, Any],
    chunk_size: int | None = None,
    *,
    preserve_screen: bool = False,
) -> dict[str, Any]:
    """Remove engine-level dimensions that prevent a headful window from resizing."""
    config = _read_camou_config(env)
    keys = _DYNAMIC_DISPLAY_CONFIG_KEYS[:4] if preserve_screen else _DYNAMIC_DISPLAY_CONFIG_KEYS
    removed = {key: config.pop(key) for key in keys if key in config}
    _write_camou_config(env, config, chunk_size)
    return removed


def _normalize_viewport(value: Any) -> dict[str, int]:
    """Normalize CLI dict and MCP tuple/list viewport inputs."""
    try:
        if isinstance(value, Mapping):
            width = int(value["width"])
            height = int(value["height"])
        elif isinstance(value, SequenceABC) and not isinstance(value, (str, bytes)):
            width = int(value[0])
            height = int(value[1])
        else:
            raise TypeError("expected a mapping or two-item sequence")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid viewport {value!r}: {exc}") from exc

    if width <= 0 or height <= 0:
        raise ValueError(f"invalid viewport {value!r}: dimensions must be positive")
    return {"width": width, "height": height}


def _request_identity(request: PWRequest) -> int:
    return id(getattr(request, "_impl_obj", request))


def _clear_stale_profile_lock(profile_path: str | None) -> None:
    """Drop a leftover parent.lock when no live process still holds it.

    This build can leave parent.lock behind after a clean exit. A stale lock is
    normally ignored, but a recycled PID can make Firefox report "already
    running". Unlink only succeeds when no process keeps the file open, so a
    running instance is never affected.
    """
    if not profile_path:
        return
    lock = Path(profile_path) / "parent.lock"
    try:
        if lock.is_file():
            lock.unlink()
    except OSError:
        pass


def _bounded_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    total = 0
    for key, raw_value in headers.items():
        value = str(raw_value)
        remaining = MAX_HEADERS_TOTAL_SIZE - total
        if remaining <= 0:
            break
        value = value[:min(MAX_HEADER_VALUE_SIZE, remaining)]
        result[str(key)] = value
        total += len(str(key)) + len(value)
    return result


def _merge_property_trace_config(
    env: dict[str, Any],
    trace_config: dict[str, Any],
    chunk_size: int | None = None,
) -> list[str]:
    """Inject propertyTrace into Camoufox's chunked CAMOU_CONFIG_N JSON."""
    errors: list[str] = []
    chunk_size = chunk_size or _camou_config_chunk_size()
    try:
        config = _read_camou_config(env)
        config["propertyTrace"] = trace_config
        _write_camou_config(env, config, chunk_size)
    except (ValueError, TypeError) as e:
        errors.append(f"CAMOU_CONFIG: {e}")

    return errors
MAX_CAPTURE_TASKS = 32
MAX_TRACE_PATHS = 128
MAX_TRACE_EVENTS = 2000
MAX_TRACE_MESSAGE_SIZE = 32_000


def detect_host_os() -> str:
    """Return the Camoufox os identifier matching the current host."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return "windows"


def detect_system_locale() -> str:
    """Best-effort detection of the host's locale (e.g. 'zh-CN')."""
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        val = _os.environ.get(var, "")
        if val:
            locale = val.split(".", 1)[0]
            if locale not in ("C", "POSIX"):
                return locale.replace("_", "-")
    return "en-US"


class BrowserManager:
    """Manages the Camoufox browser lifecycle, contexts, and pages."""

    default_config: dict[str, Any] = {}

    def __init__(self) -> None:
        self.browser: PlaywrightBrowser | None = None
        self._browser_instance_id: str | None = None
        self.contexts: dict[str, BrowserContext] = {}
        self.pages: dict[str, Page] = {}
        self.active_page_name: str | None = None
        self._cm: Any = None  # AsyncCamoufox context manager (owned-launch mode)
        self._pw: Any = None  # async_playwright instance (connect/attach mode)
        self._connected = False  # True when attached to an external Camoufox server
        self._console_logs: deque[dict] = deque(maxlen=MAX_LOG_SIZE)
        self._network_requests: deque[dict] = deque(maxlen=MAX_LOG_SIZE)
        self._request_id_counter = 0
        self._capturing = False
        self._capture_pattern: str = "**/*"
        self._capture_body = False
        self._capture_page_id: int | None = None
        self._capture_body_limit = MAX_BODY_SIZE
        self._request_entries: dict[int, tuple[Any, dict]] = {}
        self._capture_tasks: dict[int, asyncio.Task] = {}
        self._capture_dropped = 0
        self._init_scripts: list[str] = []
        self._context_init_scripts: list[str] = []
        self._persistent_scripts: list[dict] = []
        self._retired_hook_scripts = False
        self._persistent_traces: dict[str, deque[dict]] = {}
        self._persistent_trace_order: deque[tuple[str, dict]] = deque()
        self._nav_responses: list[dict] = []  # 最近一次 navigate 记录到的响应链路
        self._context_kwargs: dict[str, Any] = {}  # 新建 context 时复用的 viewport 等参数
        self._route_handlers: dict[tuple[int, str], dict[str, Any]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._runtime_session_dir = None
        self._last_launch_config: dict[str, Any] | None = None
        self._runtime_browser: dict[str, Any] | None = None
        self._trace_base_dir = None
        self._trace_max_events = 100000
        self._trace_objects: list[str] = []
        self._trace_started_at: float | None = None
        self._trace_action_lock = asyncio.Lock()

    async def launch(self, config: dict | None = None) -> dict:
        async with self._lifecycle_lock:
            return await self._launch_unlocked(config)

    async def _launch_unlocked(self, config: dict | None = None) -> dict:
        """Launch the Camoufox browser with the given or default config."""
        if self.browser is not None:
            pages_info = {}
            for name, p in self.pages.items():
                try:
                    pages_info[name] = p.url
                except Exception:
                    pages_info[name] = "unknown"
            result = {
                "status": "already_running",
                "active_page": self.active_page_name,
                "pages": pages_info,
                "contexts": list(self.contexts.keys()),
                "capturing": self._capturing,
            }
            if self._runtime_browser is not None:
                result["browser_runtime"] = self._runtime_browser
            return result

        cfg = {**self.default_config, **(config or {})}
        self._browser_instance_id = uuid.uuid4().hex

        # Attach mode: connect to an already-running Camoufox Playwright server
        # (started via `python -m camoufox server`, which prints a ws:// endpoint)
        # instead of launching a new browser. Fingerprint spoofing stays intact
        # because the server itself was launched through Camoufox's launch_options.
        ws_endpoint = cfg.get("ws_endpoint")
        if ws_endpoint:
            return await self._connect(str(ws_endpoint))

        from camoufox.async_api import AsyncCamoufox

        kwargs: dict[str, Any] = {}
        launch_warnings: list[str] = []
        runtime_browser: dict[str, Any] | None = None

        # Optional one-shot selection of an already-installed Camoufox 0.5+
        # browser (repo-qualified selector). Camoufox 0.4.x raises a clear error
        # from launch_overrides; the flat portable install used by this fork
        # keeps working when no browser_version is passed.
        browser_version = cfg.get("browser_version")
        if browser_version:
            from .camoufox_runtime import launch_overrides

            overrides, runtime_browser = launch_overrides(str(browser_version))
            kwargs.update(overrides)

        # Camoufox 0.4.5+ exposes an opt-in ``mw:`` Playwright evaluate prefix.
        # Keep the declared 0.4.0 dependency usable by feature-detecting the
        # launch option; older builds use the explicit wrappedJSObject fallback.
        main_world_eval_supported = False
        try:
            from camoufox.utils import launch_options as _capability_launch_options

            main_world_eval_supported = (
                "main_world_eval"
                in inspect.signature(_capability_launch_options).parameters
            )
        except Exception:
            pass
        if main_world_eval_supported:
            # Opening the channel does not change ordinary evaluate() semantics;
            # each tool must still explicitly request world="main".
            kwargs["main_world_eval"] = True

        if cfg.get("proxy"):
            kwargs["proxy"] = cfg["proxy"]

        os_type = cfg.get("os", "auto")
        host_os = detect_host_os()
        if os_type == "auto":
            os_type = host_os
        kwargs["os"] = os_type

        if cfg.get("humanize"):
            kwargs["humanize"] = True
        if cfg.get("geoip"):
            kwargs["geoip"] = True
        if cfg.get("block_images"):
            kwargs["block_images"] = True
        if cfg.get("block_webrtc"):
            kwargs["block_webrtc"] = True
        if cfg.get("block_webgl"):
            kwargs["block_webgl"] = True
        if cfg.get("disable_coop") is not None:
            kwargs["disable_coop"] = bool(cfg.get("disable_coop"))
        if cfg.get("webgl_config"):
            try:
                webgl_config = cfg["webgl_config"]
                kwargs["webgl_config"] = (str(webgl_config[0]), str(webgl_config[1]))
            except (TypeError, ValueError, IndexError) as e:
                raise ValueError(f"invalid webgl_config {cfg.get('webgl_config')!r}: {e}") from e
        if cfg.get("addons"):
            kwargs["addons"] = list(cfg["addons"])
        if cfg.get("exclude_addons"):
            kwargs["exclude_addons"] = list(cfg["exclude_addons"])
        if cfg.get("fonts"):
            kwargs["fonts"] = list(cfg["fonts"])
        if cfg.get("custom_fonts_only") is not None:
            kwargs["custom_fonts_only"] = bool(cfg.get("custom_fonts_only"))
        if cfg.get("enable_cache") is not None:
            kwargs["enable_cache"] = bool(cfg.get("enable_cache"))
        if cfg.get("main_world_eval") is not None:
            kwargs["main_world_eval"] = bool(cfg.get("main_world_eval"))
        if cfg.get("firefox_user_prefs"):
            kwargs["firefox_user_prefs"] = dict(cfg["firefox_user_prefs"])
        if cfg.get("args"):
            kwargs["args"] = list(cfg["args"])

        # Handle new advanced parameters
        if cfg.get("ff_version"):
            kwargs["ff_version"] = int(cfg["ff_version"])
        if cfg.get("virtual_display"):
            kwargs["virtual_display"] = str(cfg["virtual_display"])

        # Handle screen constraints for fingerprint generation
        screen_cfg = cfg.get("screen")
        if screen_cfg:
            try:
                from browserforge.fingerprints import Screen
                kwargs["screen"] = Screen(
                    min_width=screen_cfg.get("min_width") or 800,
                    max_width=screen_cfg.get("max_width") or 2560,
                    min_height=screen_cfg.get("min_height") or 600,
                    max_height=screen_cfg.get("max_height") or 1440,
                )
            except ImportError:
                # browserforge is a dependency of camoufox, should not fail
                pass
            except (TypeError, ValueError, KeyError) as e:
                raise ValueError(f"invalid screen config {screen_cfg!r}: {e}") from e

        # Handle custom fingerprint
        fingerprint_cfg = cfg.get("fingerprint")
        if fingerprint_cfg:
            try:
                from browserforge.fingerprints import Fingerprint
                kwargs["fingerprint"] = Fingerprint(**fingerprint_cfg)
            except ImportError:
                pass
            except (TypeError, ValueError, KeyError) as e:
                raise ValueError(f"invalid fingerprint config: {e}") from e

        locale = cfg.get("locale", "auto")
        if locale == "auto":
            locale = detect_system_locale()
        kwargs["locale"] = locale

        headless = cfg.get("headless", True)
        kwargs["headless"] = headless

        exe_path = cfg.get("executable_path") or _os.environ.get("CAMOUFOX_EXECUTABLE_PATH")

        # Portable profile under browser_root/Profiles/default.
        # Playwright 1.60+ rejects -profile/--profile in launch args; use XRE_PROFILE_PATH.
        # Applied later onto kwargs["env"] after the process env snapshot is built.
        profile_dir = cfg.get("profile_dir")
        existing_args = [str(a) for a in (kwargs.get("args") or [])]
        has_profile_arg = any(
            a in ("-profile", "-P") or a.startswith("-profile=") or a.startswith("-P=")
            for a in existing_args
        )
        if profile_dir:
            profile_path = str(Path(str(profile_dir)).expanduser().resolve())
            Path(profile_path).mkdir(parents=True, exist_ok=True)
        elif not has_profile_arg:
            if exe_path:
                profile_path = str(
                    Path(str(exe_path)).expanduser().resolve().parent / "Profiles" / "default"
                )
                Path(profile_path).mkdir(parents=True, exist_ok=True)
            else:
                profile_path = str(ensure_default_profile_dir())
        else:
            profile_path = None

        if exe_path and not browser_version:
            kwargs["executable_path"] = exe_path
            if not cfg.get("ff_version"):
                detected_version = executable_firefox_version(exe_path)
                if detected_version is not None:
                    kwargs["ff_version"] = detected_version
                    kwargs["i_know_what_im_doing"] = True

            # Upstream resolves default addons through CAMOUFOX_DATA_DIR and downloads
            # a full browser when that project-local directory is empty. Load bundled
            # addons beside the explicit executable and exclude package-managed copies.
            from camoufox import DefaultAddons

            requested_exclusions = {
                str(item).rsplit(".", 1)[-1].upper()
                for item in (cfg.get("exclude_addons") or [])
            }
            kwargs["exclude_addons"] = list(DefaultAddons)
            bundled_addons = list(kwargs.get("addons") or [])
            for addon in DefaultAddons:
                addon_dir = _os.path.join(_os.path.dirname(str(exe_path)), "addons", addon.name)
                if addon.name.upper() not in requested_exclusions and _os.path.isfile(
                    _os.path.join(addon_dir, "manifest.json")
                ):
                    bundled_addons.append(addon_dir)
                elif addon.name.upper() not in requested_exclusions:
                    launch_warnings.append(
                        f"bundled default addon {addon.name} was not found beside the executable"
                    )
            if bundled_addons:
                kwargs["addons"] = bundled_addons

        # 窗口尺寸: Camoufox 接受 window=(width, height) 设定一个固定的真实窗口
        # 尺寸(并据此生成匹配的指纹)。不传则随机生成。支持 window 元组或
        # window_width/window_height 两种写法。
        window_size = cfg.get("window")
        if not window_size:
            w = cfg.get("window_width")
            h = cfg.get("window_height")
            if w and h:
                window_size = (w, h)
        if window_size:
            try:
                kwargs["window"] = (int(window_size[0]), int(window_size[1]))
            except (TypeError, ValueError, IndexError) as e:
                raise ValueError(f"invalid window size {window_size!r}: {e}") from e

        # 黑边/无法自由调整窗口大小的根因修复:
        # Playwright 默认给新建 context 锁定 1280x720 的固定 viewport。有头模式下
        # 真实 Firefox 窗口往往更大,内容被钉死在 1280x720,窗口其余区域显示为黑/灰边,
        # 且无法拖动调整大小。no_viewport=True 让页面 viewport 跟随真实窗口尺寸,
        # 既消除黑边,也恢复自由缩放。用户显式传 viewport 时则改用固定 viewport。
        context_kwargs: dict[str, Any] = {}
        explicit_viewport = cfg.get("viewport")
        if explicit_viewport:
            context_kwargs["viewport"] = _normalize_viewport(explicit_viewport)
            context_kwargs["no_viewport"] = False
        elif cfg.get("no_viewport", True):
            context_kwargs["no_viewport"] = True
        self._context_kwargs = context_kwargs

        session_dir = create_browser_session_dir()
        for child in ("downloads", "traces", "artifacts"):
            (session_dir / child).mkdir(parents=True, exist_ok=True)
        kwargs["downloads_path"] = str(session_dir / "downloads")
        kwargs["traces_dir"] = str(session_dir / "traces")
        kwargs["artifacts_dir"] = str(session_dir / "artifacts")
        env = dict(_os.environ)
        if profile_path is not None:
            env.setdefault("XRE_PROFILE_PATH", profile_path)
            env.setdefault("XRE_PROFILE_LOCAL_PATH", profile_path)
        kwargs["env"] = env

        # Property trace support. Explicit official/multiversion builds fail
        # closed without changing the normal launch or browser sandbox.
        enable_trace = bool(cfg.get("enable_trace", False))
        dynamic_viewport = bool(not headless and context_kwargs.get("no_viewport"))
        requested_window = kwargs.get("window")
        if dynamic_viewport and requested_window:
            kwargs.pop("window", None)
            launch_warnings.append(
                "window size was ignored because no_viewport dynamic mode uses the native "
                "resizable window; use a fixed viewport when exact dimensions are required"
            )
        native_display_metrics: dict[str, Any] = {}
        property_trace_merge_errors: list[str] = []
        trace_warnings: list[str] = []
        trace_base_dir = None
        trace_objects: list[str] = []
        trace_max_events = 100000
        trace_runtime = runtime_browser
        if enable_trace and trace_runtime is None:
            try:
                from .camoufox_runtime import inspect_camoufox_runtime

                trace_runtime = inspect_camoufox_runtime().get("active")
            except Exception:
                trace_runtime = None
        if enable_trace and trace_runtime is not None:
            marker = trace_runtime.get("capabilities_marker")
            known_official = str(trace_runtime.get("repo") or "").lower() == "official"
            if (marker or known_official) and not trace_runtime.get("property_trace", False):
                enable_trace = False
                trace_warnings.append(
                    "enable_trace was ignored because the selected browser capability "
                    "marker does not declare PropertyTracer support."
                )
            elif (
                trace_runtime.get("property_trace_protocol") is not None
                and not trace_runtime.get("property_trace_compatible", False)
            ):
                enable_trace = False
                trace_warnings.append(
                    "enable_trace was ignored because the selected browser uses an "
                    "unsupported PropertyTracer protocol."
                )

        if enable_trace:
            from .property_trace import (
                build_property_trace_config,
                cleanup_old_runs,
                cleanup_old_traces,
                create_trace_run,
            )
            cleanup_old_traces(keep_days=7)
            cleanup_old_runs(keep_days=7)
            trace_objects = cfg.get("trace_objects") or []
            if not isinstance(trace_objects, list) or not all(
                isinstance(item, str) and item.strip() for item in trace_objects
            ):
                raise ValueError("trace_objects must be a list of non-empty object names")
            trace_objects = [item.strip() for item in trace_objects]
            trace_max_events = int(cfg.get("trace_max_events", 100000))
            if not 1 <= trace_max_events <= 200_000:
                raise ValueError("trace_max_events must be between 1 and 200000")
            trace_base_dir = create_trace_run()
            trace_config = build_property_trace_config(
                trace_base_dir,
                objects=trace_objects,
                max_events=trace_max_events,
            )

        if enable_trace or dynamic_viewport:
            from camoufox.utils import launch_options as _cfx_launch_options

            from_options = _cfx_launch_options(headless=headless, **{
                key: value for key, value in kwargs.items() if key != "headless"
            })
            env = from_options.setdefault("env", {})
            if dynamic_viewport:
                native_display_metrics = _strip_dynamic_window_fingerprint(
                    env,
                    preserve_screen=bool(kwargs.get("screen") or kwargs.get("fingerprint")),
                )
            if enable_trace:
                property_trace_merge_errors.extend(
                    _merge_property_trace_config(env, trace_config)
                )
                env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
            kwargs["from_options"] = from_options

        if profile_path is not None:
            _clear_stale_profile_lock(profile_path)

        manager = AsyncCamoufox(**kwargs)
        try:
            browser = await manager.__aenter__()
            if browser is None:
                raise RuntimeError("Camoufox launch returned no browser instance")
            ctx = (
                browser.contexts[0]
                if browser.contexts
                else await browser.new_context(**context_kwargs)
            )

            self._context_init_scripts.clear()
            if os_type != host_os:
                from .utils.js_helpers import get_font_fallback_script

                self._context_init_scripts.append(get_font_fallback_script())

            for script in self._context_init_scripts:
                await ctx.add_init_script(script)
            for script_info in self._persistent_scripts:
                await ctx.add_init_script(script=script_info["content"])

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            self._attach_listeners(page, "default")
        except Exception as launch_error:
            try:
                await manager.__aexit__(type(launch_error), launch_error, launch_error.__traceback__)
            except Exception:
                pass
            shutil.rmtree(session_dir, ignore_errors=True)
            if trace_base_dir is not None:
                shutil.rmtree(trace_base_dir, ignore_errors=True)
                trace_base_dir = None
            raise

        self._cm = manager
        self.browser = browser
        self._runtime_browser = trace_runtime if trace_base_dir is not None else runtime_browser
        self._trace_base_dir = trace_base_dir
        if trace_base_dir is not None:
            self._trace_max_events = trace_max_events
            self._trace_objects = trace_objects
        # Owned launch creates a fresh context without retired init scripts.
        self._retired_hook_scripts = False
        self.contexts["default"] = ctx
        self.pages["default"] = page
        self.active_page_name = "default"
        self._runtime_session_dir = session_dir
        self._last_launch_config = dict(cfg)

        result = {
            "status": "launched",
            "headless": headless,
            "os": os_type,
            "locale": locale,
            "profile_dir": profile_path,
            "window": list(kwargs["window"]) if kwargs.get("window") else (
                "native" if dynamic_viewport else "auto"
            ),
            "requested_window": list(requested_window) if requested_window else None,
            "advanced_options": {
                "block_webgl": kwargs.get("block_webgl"),
                "webgl_config": list(kwargs["webgl_config"]) if kwargs.get("webgl_config") else None,
                "addons": kwargs.get("addons"),
                "exclude_addons": [
                    getattr(item, "name", str(item))
                    for item in (kwargs.get("exclude_addons") or [])
                ],
                "fonts": kwargs.get("fonts"),
                "custom_fonts_only": kwargs.get("custom_fonts_only"),
                "enable_cache": kwargs.get("enable_cache"),
                "main_world_eval": kwargs.get("main_world_eval"),
                "disable_coop": kwargs.get("disable_coop"),
                "ff_version": kwargs.get("ff_version"),
                "virtual_display": kwargs.get("virtual_display"),
                "screen_constraints": bool(kwargs.get("screen")),
                "custom_fingerprint": bool(kwargs.get("fingerprint")),
            },
            "no_viewport": context_kwargs.get("no_viewport", False),
            "viewport": context_kwargs.get("viewport"),
            "dynamic_viewport": dynamic_viewport,
            "native_display_metrics": sorted(native_display_metrics),
            "warnings": launch_warnings or None,
            "pages": list(self.pages.keys()),
            "property_trace_merge_errors": property_trace_merge_errors or None,
            "main_world_eval": main_world_eval_supported,
        }
        if runtime_browser is not None:
            result["browser_runtime"] = runtime_browser
        result["engine_trace"] = {
            "requested": bool(cfg.get("enable_trace", False)),
            "enabled": trace_base_dir is not None,
            "run_dir": str(trace_base_dir) if trace_base_dir is not None else None,
            "objects": self._trace_objects if trace_base_dir is not None else [],
            "max_events_per_process": (
                self._trace_max_events if trace_base_dir is not None else None
            ),
        }
        if trace_base_dir is not None:
            trace_warnings.append(
                "Engine trace temporarily disables the Firefox content sandbox so "
                "content processes can write the private trace run; use it only for "
                "short analysis sessions."
            )
        if trace_warnings:
            result["warnings"] = trace_warnings
        return result

    async def _connect(self, ws_endpoint: str) -> dict:
        """Attach to an already-running Camoufox Playwright server via its ws:// endpoint.

        The server is started externally with `python -m camoufox server`, which prints
        a `Websocket endpoint: ws://127.0.0.1:<port>/<guid>` line. Pass that full URL here.

        Note: fingerprint config (including os) belongs to the running server. Unlike
        owned launch(), this path cannot inject the host/os font-fallback shim, so the
        server should be started with an os fingerprint matching the host for parity.
        """
        from playwright.async_api import async_playwright

        async def _teardown() -> None:
            # Disconnect the local client only — never browser.close(), which would
            # kill the user's external server. Stopping the driver tears down the
            # client transport while the server keeps running.
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self.browser = None
            self._connected = False
            self._browser_instance_id = None
            self.contexts.clear()
            self.pages.clear()
            self.active_page_name = None
            self._context_kwargs = {}
            self._runtime_session_dir = None

        self._pw = await async_playwright().start()
        try:
            self.browser = await self._pw.firefox.connect(ws_endpoint)
        except Exception:
            # Handshake failed — nothing attached yet, just drop the driver.
            try:
                await self._pw.stop()
            finally:
                self._pw = None
                self.browser = None
            raise

        # From here the browser is live; any failure must disconnect cleanly so we
        # don't leak the driver+connection or wedge the next launch into already_running.
        try:
            ctx = (
                self.browser.contexts[0]
                if self.browser.contexts
                else await self.browser.new_context()
            )
            self.contexts["default"] = ctx

            for script in self._context_init_scripts:
                await ctx.add_init_script(script)
            for script_info in self._persistent_scripts:
                await ctx.add_init_script(script=script_info["content"])

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            self._attach_listeners(page, "default")
            self.pages["default"] = page
            self.active_page_name = "default"
            self._connected = True  # only flip once fully wired
            # Recovery may re-attach; never store a full owned-launch config here.
            self._last_launch_config = {"ws_endpoint": ws_endpoint}
        except Exception:
            await _teardown()
            raise

        result = {
            "status": "connected",
            "mode": "attach",
            "ws_endpoint": ws_endpoint,
            "contexts": list(self.contexts.keys()),
            "pages": list(self.pages.keys()),
        }
        # Persistent scripts only run on the NEXT navigation; if we attached to a page
        # already on a real site, existing hooks aren't live yet — tell the caller.
        try:
            current = page.url
        except Exception:
            current = ""
        if current and current != "about:blank":
            result["warnings"] = [
                f"attached to a page already at {current}; persistent scripts and hooks "
                "apply on the next navigation — call reload() to activate them now."
            ]
        return result

    async def _ensure_browser(self) -> None:
        """Lazy-launch the browser if not already running."""
        await self.launch()

    async def add_persistent_script(self, name: str, content: str) -> bool:
        """Register a script that persists across all navigations via context-level injection."""
        for s in self._persistent_scripts:
            if s["name"] == name:
                if s["content"] == content:
                    return False
                raise ValueError(
                    f"persistent script {name!r} already exists; remove hooks before replacing it"
                )
        else:
            self._persistent_scripts.append({"name": name, "content": content})
        for ctx in self.contexts.values():
            await ctx.add_init_script(script=content)
        return True

    def remove_persistent_script(self, name: str) -> bool:
        """Remove a persistent script by name. Returns True if found."""
        before = len(self._persistent_scripts)
        self._persistent_scripts = [s for s in self._persistent_scripts if s["name"] != name]
        return len(self._persistent_scripts) < before

    def _attach_listeners(self, page: Page, page_name: str | None = None) -> None:
        """Attach console, network, and trace-collection listeners to a page."""
        page.on("console", lambda msg: self._on_console(msg, page_name))
        page.on("request", self._on_request)
        page.on("response", self._on_response_async)
        page.on("response", self._on_response_for_nav)
        page.on("requestfailed", self._on_request_failed)

    @staticmethod
    def _url_matches_pattern(url: str, pattern: str) -> bool:
        """Match URL against a Playwright-style glob pattern.

        Supports ** (matches anything including path separators) and
        * (matches anything except path separators), which differs from
        Python's fnmatch (where * matches everything and ** is not special).
        """
        if pattern == "**/*" or pattern == "**":
            return True
        import re
        # Convert Playwright glob to regex
        regex_parts = []
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == '*':
                if i + 1 < len(pattern) and pattern[i + 1] == '*':
                    # ** matches anything (including /)
                    regex_parts.append(".*")
                    i += 2
                    # Skip optional trailing /
                    if i < len(pattern) and pattern[i] == '/':
                        regex_parts.append("/?")
                        i += 1
                else:
                    # * matches anything except /
                    regex_parts.append("[^/]*")
                    i += 1
            elif c == '?':
                regex_parts.append("[^/]")
                i += 1
            elif c in r'\.+^${}()|[]':
                regex_parts.append('\\' + c)
                i += 1
            else:
                regex_parts.append(c)
                i += 1
        regex_str = "^" + "".join(regex_parts) + "$"
        return bool(re.match(regex_str, url))

    def _on_console(self, msg: ConsoleMessage, page_name: str | None = None) -> None:
        text = msg.text
        if len(text) > MAX_CONSOLE_TEXT_SIZE:
            text = text[:MAX_CONSOLE_TEXT_SIZE] + "...(truncated)"
        if text and text.startswith("__MCP_TRACE__:"):
            try:
                import json

                if len(text) > MAX_TRACE_MESSAGE_SIZE:
                    return
                payload = json.loads(text[len("__MCP_TRACE__:"):])
                path = payload.pop("__path__", "unknown")
                if (
                    not isinstance(payload, dict)
                    or not isinstance(path, str)
                    or not path
                    or len(path) > 512
                ):
                    return
                if path not in self._persistent_traces:
                    if len(self._persistent_traces) >= MAX_TRACE_PATHS:
                        oldest_path = next(iter(self._persistent_traces))
                        self._persistent_traces.pop(oldest_path, None)
                        self._persistent_trace_order = deque(
                            (saved_path, entry)
                            for saved_path, entry in self._persistent_trace_order
                            if saved_path != oldest_path
                        )
                    self._persistent_traces[path] = deque()
                else:
                    # Keep active paths at the end so path-cap eviction is LRU-like.
                    cache = self._persistent_traces.pop(path)
                    self._persistent_traces[path] = cache
                self._persistent_traces[path].append(payload)
                self._persistent_trace_order.append((path, payload))
                while len(self._persistent_trace_order) > MAX_TRACE_EVENTS:
                    old_path, old_entry = self._persistent_trace_order.popleft()
                    cache = self._persistent_traces.get(old_path)
                    if cache and cache[0] is old_entry:
                        cache.popleft()
                        if not cache:
                            self._persistent_traces.pop(old_path, None)
            except Exception as e:
                self._console_logs.append({
                    "level": "warn",
                    "text": f"failed to parse __MCP_TRACE__ console payload: {e}",
                    "timestamp": int(time.time() * 1000),
                    "location": str(msg.location) if hasattr(msg, "location") else None,
                    "page": page_name,
                })
            return

        self._console_logs.append({
            "level": msg.type,
            "text": text,
            "timestamp": int(time.time() * 1000),
            "location": str(msg.location) if hasattr(msg, "location") else None,
            "page": page_name,
        })

    def clear_network_capture(self) -> int:
        """Clear only this manager's capture and cancel its outstanding work.

        IDs remain monotonic until close, so an old cursor cannot match a new
        request after clear/reset/navigation.
        """
        count = len(self._network_requests)
        for task in self._capture_tasks.values():
            task.cancel()
        self._capture_tasks.clear()
        self._request_entries.clear()
        self._network_requests.clear()
        self._capture_dropped = 0
        return count

    def capture_status(self) -> dict:
        return {
            "active": self._capturing,
            "pattern": self._capture_pattern,
            "capture_body": self._capture_body,
            "max_body_size": self._capture_body_limit,
            "buffer_size": len(self._network_requests),
            "max_requests": self._network_requests.maxlen,
            "dropped_requests": self._capture_dropped,
            "pending_requests": len(self._request_entries),
            "pending_responses": len(self._capture_tasks),
            "first_request_id": self._network_requests[0]["id"] if self._network_requests else None,
            "last_request_id": self._request_id_counter,
        }

    def _on_request(self, req) -> None:
        if not self._capturing:
            return
        if self._capture_page_id is not None:
            try:
                request_page = req.frame.page
            except Exception:
                request_page = None
            if request_page is None or id(request_page) != self._capture_page_id:
                return
        import fnmatch
        if not fnmatch.fnmatchcase(req.url, self._capture_pattern):
            return
        self._request_id_counter += 1
        try:
            post_data = req.post_data
        except Exception as exc:
            post_data = None
            post_data_error = str(exc)
        else:
            post_data_error = None
        entry = {
            "id": self._request_id_counter,
            "url": req.url,
            "method": req.method,
            "resource_type": req.resource_type,
            "request_headers": dict(req.headers),
            "request_post_data": post_data,
            "request_post_data_error": post_data_error,
            "timestamp": int(time.time() * 1000),
            "status": None,
            "response_headers": None,
            "response_body": None,
            "duration": None,
            "state": "pending",
            "headers_complete": False,
            "capture_body_limit": self._capture_body_limit,
            "body_state": "pending" if self._capture_body else "not_captured",
        }
        if len(self._network_requests) == self._network_requests.maxlen:
            evicted = self._network_requests[0]
            for key, (_, pending) in list(self._request_entries.items()):
                if pending is evicted:
                    self._request_entries.pop(key, None)
                    break
            task = self._capture_tasks.pop(evicted["id"], None)
            if task:
                task.cancel()
            self._capture_dropped += 1
        self._network_requests.append(entry)
        # Retain the Request object until completion/eviction to prevent id reuse.
        self._request_entries[id(req)] = (req, entry)

    def _take_request_entry(self, req) -> dict | None:
        saved = self._request_entries.pop(id(req), None)
        return saved[1] if saved and saved[0] is req else None

    def _on_request_failed(self, req) -> None:
        entry = self._take_request_entry(req)
        if entry is not None:
            entry.update(state="failed", failure=req.failure,
                         body_state="failed",
                         duration=int(time.time() * 1000) - entry["timestamp"])

    def _on_response_async(self, resp) -> None:
        # Stop prevents new captures, but already captured requests still finish.
        entry = self._take_request_entry(resp.request)
        if entry is None:
            return
        entry.update(status=resp.status, response_headers=dict(resp.headers),
                     state="responded",
                     duration=int(time.time() * 1000) - entry["timestamp"])
        if len(self._capture_tasks) >= MAX_CAPTURE_TASKS:
            entry["capture_warning"] = "response_capture_capacity_reached"
            if entry["body_state"] == "pending":
                entry["body_state"] = "skipped_capacity"
            return
        task = asyncio.create_task(self._complete_response(resp, entry, entry["capture_body_limit"]))
        self._capture_tasks[entry["id"]] = task
        def done(completed):
            if self._capture_tasks.get(entry["id"]) is completed:
                self._capture_tasks.pop(entry["id"], None)
        task.add_done_callback(done)

    async def _complete_response(self, resp, entry: dict, body_limit: int) -> None:
        try:
            # The synchronous headers properties omit Cookie/Set-Cookie.
            entry["request_headers"] = await resp.request.all_headers()
            entry["response_headers"] = await resp.all_headers()
            entry["headers_complete"] = True
        except Exception as exc:
            entry["headers_error"] = str(exc)
        if entry["body_state"] == "pending":
            await self._fetch_response_body(resp, entry, body_limit)

    async def _fetch_response_body(self, resp, entry: dict, limit: int | None = None) -> None:
        """Retain bounded text; record source, stored and returned sizes separately."""
        try:
            declared_size = (getattr(resp, "headers", None) or {}).get("content-length")
            if declared_size and int(declared_size) > MAX_BODY_LOAD_SIZE:
                entry.update(
                    response_body=None,
                    body_state="skipped_oversize",
                    response_body_error=(
                        f"body skipped because content-length exceeds {MAX_BODY_LOAD_SIZE} bytes"
                    ),
                )
                return
            body_bytes = await resp.body()
            if len(body_bytes) > MAX_BODY_LOAD_SIZE:
                entry.update(
                    response_body=None,
                    body_state="skipped_oversize",
                    response_body_error=(
                        f"body skipped because decoded size exceeds {MAX_BODY_LOAD_SIZE} bytes"
                    ),
                )
                return
            try:
                body_text = body_bytes.decode("utf-8")
                body_encoding = "utf-8"
            except UnicodeDecodeError:
                body_text = body_bytes.decode("latin-1")
                body_encoding = "latin-1"
            limit = self._capture_body_limit if limit is None else limit
            truncated = len(body_text) > limit
            entry.update(
                response_body=body_text[:limit],
                response_body_encoding=body_encoding,
                response_body_truncated=truncated,
                response_body_capture_truncated=truncated,
                response_body_total_size=len(body_text),
                response_body_original_size=len(body_text),
                response_body_stored_size=min(len(body_text), limit),
                response_body_total_bytes=len(body_bytes),
                body_state="truncated" if truncated else "complete",
            )
        except Exception as exc:
            entry.update(response_body=None, body_state="failed", body_error=str(exc))

    def _on_response_for_nav(self, resp: PWResponse) -> None:
        """Record every response during a navigation for final_status resolution."""
        try:
            self._nav_responses.append({
                "url": resp.url,
                "status": resp.status,
                "resource_type": getattr(resp.request, "resource_type", None) if resp.request else None,
                "ts": int(time.time() * 1000),
            })
            # Keep only the last 100
            if len(self._nav_responses) > 100:
                del self._nav_responses[:-100]
        except Exception as e:
            self._console_logs.append({
                "level": "warn",
                "text": f"failed to record navigation response: {e}",
                "timestamp": int(time.time() * 1000),
                "location": None,
            })

    def reset_nav_responses(self) -> None:
        self._nav_responses.clear()

    async def create_context(
        self,
        name: str,
        cookies: Sequence[dict[str, Any]] | None = None,
        storage_state: str | None = None,
    ) -> dict:
        """Create a new isolated browser context with optional cookies."""
        await self._ensure_browser()
        async with self._lifecycle_lock:
            if self.browser is None:
                raise RuntimeError("No browser available after launch")
            if name in self.contexts:
                raise ValueError(f"browser context {name!r} already exists")
            context_options = dict(self._context_kwargs)
            if storage_state is not None:
                context_options["storage_state"] = storage_state
            ctx = await self.browser.new_context(**context_options)
            try:
                if cookies:
                    await ctx.add_cookies(cookies)  # type: ignore[arg-type]
                for script in self._context_init_scripts:
                    await ctx.add_init_script(script)
                for script_info in self._persistent_scripts:
                    await ctx.add_init_script(script=script_info["content"])
                page = await ctx.new_page()
                self._attach_listeners(page, name)
            except Exception:
                await ctx.close()
                raise
            self.contexts[name] = ctx
            self.pages[name] = page
            self.active_page_name = name
            return {"status": "created", "context": name}

    async def recreate_contexts(self) -> dict[str, Any]:
        """Recreate contexts so removed Playwright init scripts cannot run again."""
        await self._ensure_browser()
        async with self._lifecycle_lock:
            if self.browser is None:
                raise RuntimeError("No browser available after launch")

            previous_active_name = self.active_page_name
            captured_page_name = next(
                (
                    name for name, page in self.pages.items()
                    if self._capture_page_id is not None and id(page) == self._capture_page_id
                ),
                None,
            )
            snapshots: list[tuple[str, dict | None]] = []
            warnings: list[str] = []
            for name, ctx in list(self.contexts.items()):
                try:
                    state = await ctx.storage_state()
                except Exception as exc:
                    state = None
                    warnings.append(f"failed to preserve storage for {name}: {exc}")
                snapshots.append((name, state))

            for name, ctx in list(self.contexts.items()):
                try:
                    await ctx.close()
                except Exception as exc:
                    warnings.append(f"failed to close context {name}: {exc}")

            self.contexts.clear()
            self.pages.clear()
            self.active_page_name = None
            self._route_handlers.clear()
            try:
                from .tools.instrumentation import _active_routes

                _active_routes.clear()
            except ImportError:
                pass

            for name, state in snapshots or [("default", None)]:
                options = dict(self._context_kwargs)
                if state is not None:
                    options["storage_state"] = state
                ctx = None
                try:
                    ctx = await self.browser.new_context(**options)
                    for script in self._context_init_scripts:
                        await ctx.add_init_script(script)
                    for script_info in self._persistent_scripts:
                        await ctx.add_init_script(script=script_info["content"])
                    page = await ctx.new_page()
                    self._attach_listeners(page, name)
                except Exception as exc:
                    warnings.append(f"failed to recreate context {name}: {exc}")
                    try:
                        if ctx is not None:
                            await ctx.close()
                    except Exception:
                        pass
                    continue
                self.contexts[name] = ctx
                self.pages[name] = page

            if self.pages:
                preferred = previous_active_name or (snapshots[0][0] if snapshots else "default")
                self.active_page_name = preferred if preferred in self.pages else next(iter(self.pages))
            if self._capturing:
                if captured_page_name in self.pages:
                    self._capture_page_id = id(self.pages[captured_page_name])
                else:
                    self._capturing = False
                    self._capture_body = False
                    self._capture_page_id = None
            return {
                "status": "recreated",
                "contexts": list(self.contexts),
                "warnings": warnings or None,
            }

    async def get_active_page(self) -> Page:
        """Get the currently active page, launching the browser if needed."""
        await self._ensure_browser()
        if self.active_page_name and self.active_page_name in self.pages:
            return self.pages[self.active_page_name]
        raise RuntimeError("No active page available. Call launch_browser first.")

    async def clear_route_handlers(self) -> dict[str, Any]:
        """Remove all page-level request interception routes tracked by the manager."""
        entries = list(self._route_handlers.items())
        errors: list[dict[str, str]] = []
        removed = 0
        for key, info in entries:
            try:
                await info["owner"].unroute(info["pattern"], info["handler"])
                self._route_handlers.pop(key, None)
                removed += 1
            except Exception as exc:
                errors.append({"pattern": info["pattern"], "error": str(exc)})
        return {"removed": removed, "errors": errors}

    async def close(self) -> dict:
        async with self._lifecycle_lock:
            return await self._close_unlocked()

    async def _close_unlocked(self, *, preserve_recovery_state: bool = False) -> dict:
        """Close the browser and clean up all resources.

        Owned-launch mode: fully shuts the browser down.
        Attach mode (connected to an external server): only disconnects the local
        Playwright client — the user's browser and server are left running.
        """
        close_error = None
        was_attach = bool(self._connected and self._pw is not None)
        self._capturing = False
        self.clear_network_capture()
        owned_context_closed = False
        trace_base_dir = self._trace_base_dir
        if trace_base_dir is not None:
            try:
                from .property_trace import set_trace_state

                await set_trace_state(
                    "off",
                    trace_base_dir,
                    features=(self._runtime_browser or {}).get(
                        "property_trace_features", []
                    ),
                    timeout=2.0,
                )
            except Exception:
                pass
        if was_attach:
            # Disconnect without closing the remote browser: stopping the driver
            # tears down the client transport but leaves the external server alive.
            try:
                await self._pw.stop()
            except Exception as e:
                close_error = str(e)
        elif self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception as e:
                close_error = str(e)
            else:
                owned_context_closed = True
        stale_controls_removed = 0
        if trace_base_dir is not None:
            try:
                from .property_trace import cleanup_stale_controls

                stale_controls_removed = cleanup_stale_controls(trace_base_dir)
            except Exception:
                pass
        self.browser = None
        self._browser_instance_id = None
        self.contexts.clear()
        self.pages.clear()
        self.active_page_name = None
        self._cm = None
        self._pw = None
        self._connected = False
        self._runtime_browser = None
        self._trace_base_dir = None
        self._trace_max_events = 100000
        self._trace_objects = []
        self._trace_started_at = None
        self._console_logs.clear()
        self._network_requests.clear()
        self._request_entries.clear()
        self._request_id_counter = 0
        self._capturing = False
        self._capture_body = False
        self._capture_page_id = None
        if not preserve_recovery_state:
            self._init_scripts.clear()
        self._context_init_scripts.clear()
        if not preserve_recovery_state:
            self._persistent_scripts.clear()
        # Disconnecting leaves external contexts and their init scripts alive.
        if owned_context_closed:
            self._retired_hook_scripts = False
        self._persistent_traces.clear()
        self._persistent_trace_order.clear()
        self._nav_responses.clear()
        self._context_kwargs = {}
        self._route_handlers.clear()
        runtime_session_dir = self._runtime_session_dir
        self._runtime_session_dir = None
        if runtime_session_dir is not None:
            shutil.rmtree(runtime_session_dir, ignore_errors=True)
        if not preserve_recovery_state:
            self._last_launch_config = None
        try:
            from .tools.instrumentation import _active_routes

            _active_routes.clear()
        except ImportError:
            pass
        result = {
            "status": "closed",
            "mode": "attach" if was_attach else "owned",
            "stale_trace_controls_removed": stale_controls_removed,
        }
        if close_error:
            result["close_error"] = close_error
        return result
