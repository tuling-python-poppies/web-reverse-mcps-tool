# 模块说明: 使用 camoufox-reverse 定制版浏览器做引擎级属性访问追踪。
"""Engine-level property access tracing tools (v1.1.0).

Requires camoufox-reverse custom browser build.
Falls back gracefully when using official Camoufox.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, Optional

from ..server import mcp, browser_manager
from ..property_trace import (
    CONTROL_DIR, TRACES_DIR,
    list_session_files, load_events,
    build_summary, build_timeline, build_sequence,
    filter_events, write_control_all, cleanup_traces,
)
from ..utils.response_fmt import error_response

_TRACE_LOCK = asyncio.Lock()


def _is_trace_enabled() -> bool:
    """Check if any control files exist (= custom browser with trace enabled)."""
    if not CONTROL_DIR.exists():
        return False
    return len(list(CONTROL_DIR.glob("control-*.cmd"))) > 0


def _diagnose_trace_unavailable() -> dict:
    """Explain why engine-level trace is not ready (do not always blame missing reverse build)."""
    from .environment import resolve_camoufox_binary

    binary = resolve_camoufox_binary()
    browser_running = browser_manager.browser is not None
    last_cfg = getattr(browser_manager, "_last_launch_config", None) or {}
    attached = bool(getattr(browser_manager, "_connected", False))
    enable_trace_requested = bool(last_cfg.get("enable_trace"))
    control_dir = str(CONTROL_DIR)
    control_count = (
        len(list(CONTROL_DIR.glob("control-*.cmd"))) if CONTROL_DIR.exists() else 0
    )

    base = {
        "error": "engine_trace_not_available",
        "control_dir": control_dir,
        "control_files": control_count,
        "binary": binary,
        "browser_running": browser_running,
        "attached": attached,
        "enable_trace_requested": enable_trace_requested,
        "install_guide": "https://github.com/WhiteNightShadow/camoufox-reverse/releases",
    }

    if not binary.get("available"):
        return {
            **base,
            "reason": "binary_missing",
            "message": (
                "未找到 camoufox 可执行文件，无法确认是否为 camoufox-reverse 定制版。"
                "请设置 CAMOUFOX_EXECUTABLE_PATH，或使用 sibling 布局"
                "（浏览器根目录下放 camoufox.exe，旁边是 camoufox-reverse-mcp）。"
            ),
            "next_step": (
                "安装/放置 camoufox-reverse 后重启 MCP，再 "
                "launch_browser(enable_trace=True)，然后调用 trace_property_access。"
            ),
        }

    if not browser_running:
        return {
            **base,
            "reason": "browser_not_running",
            "message": (
                f"已检测到浏览器二进制（{binary.get('path')}），但当前没有活动浏览器会话，"
                "引擎级 trace 控制文件尚未创建。"
            ),
            "next_step": (
                "先调用 launch_browser(enable_trace=True)，再调用 trace_property_access。"
            ),
        }

    if attached:
        return {
            **base,
            "reason": "attach_mode_without_trace_controls",
            "message": (
                "当前是 attach 模式（连到外部 Camoufox server）。"
                "引擎级 propertyTrace 必须在 server/浏览器启动阶段注入；"
                "仅 MCP attach 无法补开 C++ 层追踪。"
            ),
            "next_step": (
                "用 owned launch：close_browser() 后 launch_browser(enable_trace=True)；"
                "或在启动 server 时启用 propertyTrace 后再 attach。"
            ),
        }

    if not enable_trace_requested:
        return {
            **base,
            "reason": "enable_trace_not_requested",
            "message": (
                f"当前已是可解析的定制浏览器二进制（{binary.get('path')}），"
                "但本次启动未开启 enable_trace，因此没有引擎层 control 文件。"
                "这不等于“装错官方版”。"
            ),
            "next_step": (
                "close_browser() 后重新 launch_browser(enable_trace=True)，"
                "再调用 trace_property_access。"
            ),
        }

    return {
        **base,
        "reason": "trace_controls_missing_after_enable",
        "message": (
            f"已请求 enable_trace=True 且二进制可用（{binary.get('path')}），"
            f"但在 control 目录未找到 control-*.cmd：{control_dir}。"
            "可能是浏览器未写入 propertyTrace、路径不一致，或进程启动失败。"
        ),
        "next_step": (
            "检查 launch_browser 返回的 property_trace_merge_errors；"
            "确认使用的是 camoufox-reverse 构建；"
            "必要时 close_browser 后带 enable_trace=True 重试。"
        ),
    }


def _trace_files_snapshot() -> tuple[tuple[str, int, int], ...]:
    """Return a compact snapshot of current trace files for polling."""
    snapshot = []
    for f in list_session_files():
        try:
            st = f.stat()
            snapshot.append((str(f), int(st.st_size), int(st.st_mtime_ns)))
        except OSError:
            continue
    return tuple(snapshot)


async def _wait_trace_files_changed(
    before: tuple[tuple[str, int, int], ...],
    timeout: float = 1.0,
    interval: float = 0.05,
) -> bool:
    """Wait until trace files differ from a previous snapshot."""
    # 这里不是固定 sleep,而是带超时的轮询: 只要文件状态变化就立即继续。
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if _trace_files_snapshot() != before:
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_trace_files_stable(
    timeout: float = 2.0,
    interval: float = 0.05,
    stable_rounds: int = 2,
) -> bool:
    """Wait until trace files stop changing, bounded by timeout."""
    # “稳定”定义为连续若干轮 snapshot 完全一致。
    # 这样可以尽量等到 trace 文件 flush 完成,又不会无限阻塞。
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    stable = 0
    while asyncio.get_running_loop().time() < deadline:
        current = _trace_files_snapshot()
        if current and current == last:
            stable += 1
            if stable >= stable_rounds:
                return True
        else:
            stable = 0
            last = current
        await asyncio.sleep(interval)
    return False


@mcp.tool()
async def trace_property_access(
    duration: int = 10,
    mode: Literal["summary", "timeline", "sequence", "search"] = "summary",
    filter_object: Optional[str] = None,
    search_query: Optional[str] = None,
    limit: int = 1000,
    bucket_ms: int = 500,
    collect_values: bool = False,
    control_settle_timeout: float = 0.5,
    flush_timeout: float = 2.0,
    poll_interval: float = 0.05,
) -> dict:
    """Engine-level DOM property access tracing (JSVMP-undetectable).

    Traces which DOM properties (navigator, screen, window, canvas, webgl, etc.)
    are accessed by page JavaScript including JSVMP bytecode interpreters.
    Operates at the C++ SpiderMonkey engine level — completely invisible to JS.

    Requires camoufox-reverse custom browser launched with enable_trace=True.
    If control files are missing, returns a structured diagnosis (binary missing /
    browser not running / enable_trace not requested / attach mode / path issues)
    instead of always claiming the browser is not a reverse build.

    Args:
        duration: Trace duration in seconds (default 10).
            Set to 0 to read existing trace data from browser startup
            (useful when you want to capture navigate() events).
        mode: Aggregation view type:
            - "summary" (default): Property access frequency ranking.
              Best for deciding which properties to patch in env emulation.
            - "timeline": Time-bucketed view showing when properties are first accessed.
            - "sequence": Raw event sequence with timestamps.
            - "search": Same as sequence but filtered by search_query.
        filter_object: Only include events from this object (e.g. "navigator").
        search_query: Only include events matching this string in property/value.
        limit: Max events for sequence/search mode (default 1000).
        bucket_ms: Bucket size for timeline mode (default 500ms).
        collect_values: If True, after trace completes, use evaluate_js to read
            real values of all traced properties from the browser. Large values
            (Canvas dataURL, WebGL params etc.) are saved to files under
            .camoufox-runtime/ and returned as file paths.
        control_settle_timeout: Max seconds to wait for trace control changes.
        flush_timeout: Max seconds to wait for trace files to finish flushing.
        poll_interval: Polling interval for trace file state checks.

    Returns:
        summary mode: {mode, duration_s, total_events, unique_properties, by_property, by_object}
            If collect_values=True, adds "values" dict: {property_path: value_or_filepath}
        timeline mode: {mode, duration_s, bucket_ms, buckets}
        sequence mode: {mode, total_events, returned, truncated, events}
    """
    if duration < 0:
        return {"mode": "error", "reason": "duration must be >= 0"}
    if limit <= 0:
        return {"mode": "error", "reason": "limit must be > 0"}
    if bucket_ms <= 0:
        return {"mode": "error", "reason": "bucket_ms must be > 0"}
    if not _is_trace_enabled():
        return _diagnose_trace_unavailable()

    if control_settle_timeout < 0 or flush_timeout < 0:
        return {"mode": "error", "reason": "timeouts must be >= 0"}
    if poll_interval <= 0:
        return {"mode": "error", "reason": "poll_interval must be > 0"}

    async with _TRACE_LOCK:
        return await _run_trace_window(
            duration=duration,
            mode=mode,
            filter_object=filter_object,
            search_query=search_query,
            limit=limit,
            bucket_ms=bucket_ms,
            collect_values=collect_values,
            control_settle_timeout=control_settle_timeout,
            flush_timeout=flush_timeout,
            poll_interval=poll_interval,
        )


async def _run_trace_window(
    *,
    duration: int,
    mode: str,
    filter_object: str | None,
    search_query: str | None,
    limit: int,
    bucket_ms: int,
    collect_values: bool,
    control_settle_timeout: float,
    flush_timeout: float,
    poll_interval: float,
) -> dict:
    if duration > 0:
        write_control_all("off")
        await _wait_trace_files_stable(timeout=control_settle_timeout, interval=poll_interval)
        cleanup_traces()
        before_trace = _trace_files_snapshot()
        baseline_sizes = {path: size for path, size, _ in before_trace}
        write_control_all("on")
        await asyncio.sleep(duration)
        write_control_all("off")
        changed = await _wait_trace_files_changed(
            before_trace, timeout=control_settle_timeout, interval=poll_interval,
        )
        stable = await _wait_trace_files_stable(timeout=flush_timeout, interval=poll_interval)
    else:
        write_control_all("off")
        changed = None
        stable = await _wait_trace_files_stable(timeout=flush_timeout, interval=poll_interval)
        baseline_sizes = {}

    events: list[dict] = []
    for trace_file in list_session_files():
        remaining = 200_000 - len(events)
        if remaining <= 0:
            break
        events.extend(load_events(
            trace_file,
            max_events=remaining,
            start_offset=baseline_sizes.get(str(trace_file), 0),
        ))

    if not events:
        return {
            "mode": "error",
            "reason": "No trace events captured during the window.",
            "hint": "Ensure the page has loaded and JSVMP is executing.",
            "flush_stable": stable,
            "trace_files_changed": changed,
        }

    events = filter_events(events, filter_object, search_query)
    if mode == "summary":
        result = build_summary(events, duration)
    elif mode == "timeline":
        result = build_timeline(events, duration, bucket_ms)
    elif mode in ("sequence", "search"):
        result = build_sequence(events, limit)
    else:
        return {"mode": "error", "reason": f"Unknown mode: {mode}"}

    if collect_values and result.get("by_property"):
        result["values"] = await _collect_property_values(result["by_property"])
    result["flush_stable"] = stable
    result["trace_files_changed"] = changed
    return result


@mcp.tool()
async def list_trace_files(limit: int = 20) -> dict:
    """List all trace files on disk (for post-hoc analysis).

    Returns:
        dict with traces_dir, total file count, and file details.
    """
    if limit <= 0:
        return {"mode": "error", "reason": "limit must be > 0"}
    if not TRACES_DIR.exists():
        return {"files": [], "total": 0, "traces_dir": str(TRACES_DIR)}

    all_files = []
    for f in TRACES_DIR.glob("*.jsonl"):
        try:
            parts = f.stem.split("_")
            file_pid = int(parts[0]) if parts else -1
            session_id = int(parts[1]) if len(parts) > 1 else -1
        except (IndexError, ValueError):
            continue

        size_kb = f.stat().st_size / 1024
        all_files.append({
            "path": str(f),
            "pid": file_pid,
            "session_id": session_id,
            "size_kb": round(size_kb, 1),
            "mtime": f.stat().st_mtime,
        })

    all_files.sort(key=lambda x: x["mtime"], reverse=True)
    return {
        "traces_dir": str(TRACES_DIR),
        "total": len(all_files),
        "returned": min(len(all_files), limit),
        "files": all_files[:limit],
    }


@mcp.tool()
async def query_trace_file(
    file_path: str,
    mode: Literal["summary", "timeline", "sequence", "search"] = "summary",
    filter_object: Optional[str] = None,
    search_query: Optional[str] = None,
    limit: int = 1000,
    bucket_ms: int = 500,
) -> dict:
    """Query a specific historical trace file (post-hoc analysis).

    Args:
        file_path: Path to the .jsonl trace file.
        mode: Same as trace_property_access (summary/timeline/sequence/search).
        filter_object: Filter by object name.
        search_query: Filter by search string.
        limit: Max events for sequence mode.
        bucket_ms: Bucket size for timeline mode.
    """
    if limit <= 0:
        return {"mode": "error", "reason": "limit must be > 0"}
    if bucket_ms <= 0:
        return {"mode": "error", "reason": "bucket_ms must be > 0"}

    path = Path(file_path)
    if not path.is_absolute():
        path = TRACES_DIR / path
    path = path.resolve()
    try:
        path.relative_to(TRACES_DIR.resolve())
    except ValueError:
        return {
            "mode": "error",
            "reason": "Trace file must be inside the configured traces directory",
        }
    if path.suffix.lower() != ".jsonl":
        return {"mode": "error", "reason": "Trace file must use the .jsonl extension"}
    if not path.exists():
        return {"mode": "error", "reason": f"File not found: {file_path}"}

    events = load_events(path)
    events = filter_events(events, filter_object, search_query)

    duration_s = 0
    if events:
        duration_s = (events[-1].get("t", 0) // 1000) + 1

    if mode == "summary":
        return build_summary(events, duration_s)
    elif mode == "timeline":
        return build_timeline(events, duration_s, bucket_ms)
    elif mode in ("sequence", "search"):
        return build_sequence(events, limit)
    else:
        return {"mode": "error", "reason": f"Unknown mode: {mode}"}


async def _collect_property_values(by_property: list[dict]) -> dict:
    """Read real values of traced properties from the browser via evaluate_js.
    Large values (>500 chars) are saved to files."""
    import json as _json
    from ..property_trace import CACHE_DIR

    values_dir = CACHE_DIR / "values"
    values_dir.mkdir(parents=True, exist_ok=True)

    # Build JS expression to read all unique properties
    # Map trace paths to JS expressions
    path_to_js = {
        "navigator.userAgent": "navigator.userAgent",
        "navigator.platform": "navigator.platform",
        "navigator.language": "navigator.language",
        "navigator.languages": "JSON.stringify(navigator.languages)",
        "navigator.hardwareConcurrency": "navigator.hardwareConcurrency",
        "navigator.maxTouchPoints": "navigator.maxTouchPoints",
        "navigator.cookieEnabled": "navigator.cookieEnabled",
        "navigator.onLine": "navigator.onLine",
        "navigator.pdfViewerEnabled": "navigator.pdfViewerEnabled",
        "navigator.doNotTrack": "navigator.doNotTrack",
        "navigator.appVersion": "navigator.appVersion",
        "navigator.appCodeName": "navigator.appCodeName",
        "navigator.appName": "navigator.appName",
        "navigator.product": "navigator.product",
        "navigator.productSub": "navigator.productSub",
        "navigator.oscpu": "navigator.oscpu",
        "navigator.buildID": "navigator.buildID",
        "navigator.globalPrivacyControl": "navigator.globalPrivacyControl",
        "screen.rect": "JSON.stringify({w:screen.width,h:screen.height})",
        "screen.availRect": "JSON.stringify({w:screen.availWidth,h:screen.availHeight,l:screen.availLeft,t:screen.availTop})",
        "screen.pixelDepth": "screen.pixelDepth",
        "screen.colorDepth": "screen.colorDepth",
        "window.innerWidth": "window.innerWidth",
        "window.innerHeight": "window.innerHeight",
        "window.outerWidth": "window.outerWidth",
        "window.outerHeight": "window.outerHeight",
        "window.screenX": "window.screenX",
        "window.screenY": "window.screenY",
        "window.devicePixelRatio": "window.devicePixelRatio",
        "window.scrollX": "window.scrollX",
        "window.scrollY": "window.scrollY",
        "document.cookie.get": "document.cookie",
        "history.length": "history.length",
        "navigator.plugins.indexedGetter": "navigator.plugins.length",
        "navigator.mimeTypes.indexedGetter": "navigator.mimeTypes.length",
        "performance.timing": "JSON.stringify(performance.timing)",
        "canvas.toDataURL": "(()=>{var c=document.createElement('canvas');c.width=200;c.height=50;var x=c.getContext('2d');x.fillText('trace',10,30);return c.toDataURL()})()",
        "canvas2d.getImageData": "(()=>{var c=document.createElement('canvas');c.width=10;c.height=10;var x=c.getContext('2d');x.fillRect(0,0,5,5);return JSON.stringify(Array.from(x.getImageData(0,0,1,1).data))})()",
        "webgl.getParameter": "(()=>{var c=document.createElement('canvas');var g=c.getContext('webgl');if(!g)return null;return JSON.stringify({renderer:g.getParameter(g.RENDERER),vendor:g.getParameter(g.VENDOR)})})()",
        "webgl.getSupportedExtensions": "(()=>{var c=document.createElement('canvas');var g=c.getContext('webgl');if(!g)return null;return JSON.stringify(g.getSupportedExtensions())})()",
        "webgl.getShaderPrecisionFormat": "(()=>{var c=document.createElement('canvas');var g=c.getContext('webgl');if(!g)return null;var p=g.getShaderPrecisionFormat(g.VERTEX_SHADER,g.HIGH_FLOAT);return JSON.stringify({rangeMin:p.rangeMin,rangeMax:p.rangeMax,precision:p.precision})})()",
        "AudioContext.sampleRate": "(()=>{try{var a=new AudioContext();var r=a.sampleRate;a.close();return r}catch(e){return null}})()",
    }

    # Get unique property paths from trace
    paths = [p["path"] for p in by_property]

    # Build batch JS
    js_parts = []
    for path in paths:
        js_expr = path_to_js.get(path)
        if js_expr:
            safe_key = path.replace(".", "_").replace("-", "_")
            js_parts.append(f'try{{r.{safe_key}={js_expr}}}catch(e){{r.{safe_key}="ERROR:"+e.message}}')

    if not js_parts:
        return {}

    js_code = "(() => { var r = {}; " + ";".join(js_parts) + "; return r; })()"

    try:
        page = await browser_manager.get_active_page()
        raw = await page.evaluate(js_code)
    except Exception as e:
        return error_response(e, code="collect_property_values_failed")

    # Process results: save large values to files
    values = {}
    for path in paths:
        safe_key = path.replace(".", "_").replace("-", "_")
        val = raw.get(safe_key)
        if val is None:
            continue
        val_str = str(val)
        if len(val_str) > 500:
            # Save to file
            filename = f"{safe_key}.txt"
            filepath = values_dir / filename
            filepath.write_text(val_str, encoding="utf-8")
            values[path] = f"[file:{filepath}] ({len(val_str)} chars)"
        else:
            values[path] = val

    return values


async def _fallback_compare_env(reason: str) -> dict:
    """Fallback to compare_env when engine-level tracing is unavailable."""
    try:
        from .jsvmp import compare_env
        result = await compare_env()
    except Exception as e:
        result = {"error": f"compare_env also failed: {e}"}

    diagnosis = _diagnose_trace_unavailable()
    return {
        "mode": "fallback_compare_env",
        "reason": reason,
        "diagnosis": diagnosis,
        "install_hint": (
            "Engine-level tracing needs camoufox-reverse + enable_trace=True:\n"
            "1. Ensure CAMOUFOX_EXECUTABLE_PATH points to reverse camoufox.exe\n"
            "2. launch_browser(enable_trace=True)\n"
            "3. trace_property_access(duration=10)\n"
            f"Current diagnosis: {diagnosis.get('reason')} — {diagnosis.get('next_step')}"
        ),
        "releases_url": "https://github.com/WhiteNightShadow/camoufox-reverse/releases",
        "result": result,
    }
