# 模块说明: 提供函数级 hook、预设 hook 注入与 hook 卸载能力。
from __future__ import annotations

import json
import os
from typing import Literal

from ..server import mcp, browser_manager
from ..utils.js_helpers import render_trace_template, render_persistent_trace_template
from ..utils.response_fmt import error_response


@mcp.tool()
async def hook_function(
    function_path: str,
    mode: Literal["intercept", "trace"] = "intercept",
    hook_code: str = "",
    position: Literal["before", "after", "replace"] = "before",
    non_overridable: bool = False,
    persistent: bool = False,
    log_args: bool = True,
    log_return: bool = True,
    log_stack: bool = False,
    max_captures: int = 50,
) -> dict:
    """Hook or trace a function (v0.9.0 unified).

    Replaces hook_function + trace_function.

    Args:
        function_path: Full path like "window.encrypt",
            "XMLHttpRequest.prototype.open", "JSON.stringify".
        mode:
          "intercept" — inject custom JS before/after/replace the function.
                        Requires hook_code. (was: hook_function)
          "trace"     — non-invasive trace logging args, return values,
                        and optionally call stacks. (was: trace_function)
        hook_code: JS code for "intercept" mode. Context vars:
            - arguments: original args
            - __this: the 'this' context
            - __result: return value (only in position="after")
        position: For "intercept": "before", "after", or "replace".
        non_overridable: For "intercept": use Object.defineProperty to lock.
        persistent: If True, survives page navigation.
        log_args: For "trace": record arguments (default True).
        log_return: For "trace": record return values (default True).
        log_stack: For "trace": record call stacks (default False).
        max_captures: For "trace": max calls to record (default 50).

    Returns:
        dict with status, target, mode.
    """
    if mode == "trace":
        return await _trace_function(
            function_path, persistent, log_args, log_return, log_stack, max_captures
        )
    elif mode == "intercept":
        return await _hook_function(
            function_path, hook_code, position, non_overridable, persistent
        )
    else:
        return error_response(f"unknown mode: {mode}. Use 'intercept' or 'trace'")


async def _trace_function(
    function_path: str, persistent: bool,
    log_args: bool, log_return: bool, log_stack: bool, max_captures: int,
) -> dict:
    try:
        if persistent:
            trace_js = render_persistent_trace_template(
                function_path=function_path, max_captures=max_captures,
                log_args=log_args, log_return=log_return, log_stack=log_stack,
            )
            trace_name = f"trace:{function_path}"
            added = await browser_manager.add_persistent_script(trace_name, trace_js)
            if not added:
                return {"status": "already_tracing", "target": function_path, "persistent": True}
            page = await browser_manager.get_active_page()
            await page.evaluate(trace_js)
            return {"status": "tracing", "target": function_path, "persistent": True}
        else:
            page = await browser_manager.get_active_page()
            trace_js = render_trace_template(
                function_path=function_path, max_captures=max_captures,
                log_args=log_args, log_return=log_return, log_stack=log_stack,
            )
            await page.evaluate(trace_js)
            return {"status": "tracing", "target": function_path, "persistent": False}
    except Exception as e:
        return error_response(e)


async def _hook_function(
    function_path: str,
    hook_code: str,
    position: Literal["before", "after", "replace"],
    non_overridable: bool,
    persistent: bool = False,
) -> dict:
    try:
        page = await browser_manager.get_active_page()
        installer_js = r"""({functionPath, hookCode, position, nonOverridable}) => {
    try {
        const parts = String(functionPath || '').split('.').filter(Boolean);
        if (!parts.length) return { ok: false, error: 'function_path is required' };
        let parent = window;
        const start = parts[0] === 'window' ? 1 : 0;
        for (let i = start; i < parts.length - 1; i++) {
            parent = parent[parts[i]];
            if (!parent) return { ok: false, error: 'path not found: ' + parts.slice(0, i + 1).join('.') };
        }
        const fn = parts[parts.length - 1];
        if (!parent || !fn) return { ok: false, error: 'invalid function_path: ' + functionPath };

        const makeHook = new Function('__this', '__result',
            'return function() {\n' + String(hookCode || '') + '\n};');
        const freezeTarget = () => {
            if (!nonOverridable) return;
            try {
                Object.defineProperty(parent, fn, {
                    value: parent[fn], writable: false, configurable: false
                });
            } catch(e) {}
        };

        const _orig = parent[fn];
        let wrapper;
        if (position === 'before') {
            if (typeof _orig !== 'function') return { ok: false, error: functionPath + ' is not a function' };
            wrapper = function(...args) {
                const __this = this;
                makeHook(__this, undefined).apply(__this, args);
                return _orig.apply(this, args);
            };
            wrapper.toString = function() { return _orig.toString(); };
        } else if (position === 'after') {
            if (typeof _orig !== 'function') return { ok: false, error: functionPath + ' is not a function' };
            wrapper = function(...args) {
                const __this = this;
                const __result = _orig.apply(this, args);
                makeHook(__this, __result).apply(__this, args);
                return __result;
            };
            wrapper.toString = function() { return _orig.toString(); };
        } else if (position === 'replace') {
            wrapper = function(...args) {
                const __this = this;
                return makeHook(__this, undefined).apply(__this, args);
            };
        } else {
            return { ok: false, error: "Invalid position: " + position + ". Use 'before', 'after', or 'replace'." };
        }

        parent[fn] = wrapper;
        window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};
        const hookKey = 'function:' + functionPath;
        window.__mcp_hook_uninstallers[hookKey] = function() {
            const restored = [];
            if (!nonOverridable && parent[fn] === wrapper) {
                parent[fn] = _orig;
                restored.push(functionPath);
            }
            delete window.__mcp_hook_uninstallers[hookKey];
            return restored;
        };
        freezeTarget();
        return { ok: true };
    } catch (e) {
        return { ok: false, error: String(e && e.message || e) };
    }
}"""
        payload = {
            "functionPath": function_path,
            "hookCode": hook_code,
            "position": position,
            "nonOverridable": non_overridable,
        }
        init_script = None
        if persistent:
            encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            init_script = f"({installer_js})({encoded_payload});"
            script_name = f"function_hook:{function_path}"
            existing = next(
                (item for item in browser_manager._persistent_scripts if item["name"] == script_name),
                None,
            )
            if existing:
                if existing["content"] == init_script:
                    return {
                        "status": "already_hooked", "target": function_path,
                        "position": position, "non_overridable": non_overridable,
                        "persistent": True,
                    }
                return error_response(
                    f"persistent hook for {function_path!r} already exists; remove hooks before replacing it"
                )
        result = await page.evaluate(installer_js, payload)
        if isinstance(result, dict) and not result.get("ok"):
            return error_response(result.get("error", "failed to install hook"))
        if persistent:
            await browser_manager.add_persistent_script(
                f"function_hook:{function_path}",
                init_script or "",
            )
        return {"status": "hooked", "target": function_path, "position": position,
                "non_overridable": non_overridable, "persistent": persistent}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def inject_hook_preset(preset: str, persistent: bool = True) -> dict:
    """Inject a pre-built hook template for common reverse engineering tasks.

    Available presets:
        - "xhr": Hook XMLHttpRequest to log all XHR requests.
        - "fetch": Hook window.fetch to log all fetch requests.
        - "crypto": Hook btoa/atob/JSON.stringify to capture encryption I/O.
        - "websocket": Hook WebSocket to log all WS messages.
        - "debugger_bypass": Bypass anti-debugging traps.
        - "cookie": Hook document.cookie writes.
        - "runtime_probe": Full runtime probe.

    Args:
        preset: One of the above preset names.
        persistent: If True (default), survives page navigation.

    Returns:
        dict with status and the preset name.
    """
    preset_map = {
        "xhr": "xhr_hook.js",
        "fetch": "fetch_hook.js",
        "crypto": "crypto_hook.js",
        "websocket": "websocket_hook.js",
        "debugger_bypass": "debugger_trap.js",
        "cookie": "cookie_hook.js",
        "runtime_probe": "runtime_probe.js",
    }
    if preset not in preset_map:
        return error_response(f"Unknown preset: {preset}. Available: {list(preset_map.keys())}")
    try:
        hooks_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks")
        hook_file = os.path.join(hooks_dir, preset_map[preset])
        with open(hook_file, "r", encoding="utf-8") as f:
            hook_js = f.read()
        if persistent:
            script_name = f"preset:{preset}"
            added = await browser_manager.add_persistent_script(script_name, hook_js)
            if not added:
                return {"status": "already_injected", "preset": preset, "persistent": True}
            page = await browser_manager.get_active_page()
            await page.evaluate(hook_js)
        else:
            page = await browser_manager.get_active_page()
            await page.evaluate(hook_js)
        browser_manager._init_scripts.append(f"preset:{preset}")
        return {"status": "injected", "preset": preset, "persistent": persistent}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def remove_hooks(keep_persistent: bool = False) -> dict:
    """Remove installed hooks and restore original objects in-place.

    Args:
        keep_persistent: If True, keep persistent init_scripts registered.

    Returns:
        dict with status, restored_objects, cleared counts.
    """
    try:
        active_page = await browser_manager.get_active_page()
        warnings: list[str] = []
        restored: list[str] = []

        uninstall_js = r"""
        (function() {
          var out = { uninstalled: [], errors: [] };
          var uninstallers = window.__mcp_hook_uninstallers || {};
          Object.keys(uninstallers).forEach(function(name) {
            try {
              var r = uninstallers[name]();
              out.uninstalled.push({ hook: name, restored: r || [] });
            } catch (e) { out.errors.push(name + ': ' + e.message); }
          });
          if (typeof window.__mcp_jsvmp_uninstall === 'function') {
            try {
              var r = window.__mcp_jsvmp_uninstall();
              out.uninstalled.push({ hook: 'jsvmp_proxy',
                                     restored: (r && r.restored) || [] });
            } catch (e) { out.errors.push('jsvmp_uninstall: ' + e.message); }
          }
          if (typeof window.__mcp_transparent_uninstall === 'function') {
            try {
              var r = window.__mcp_transparent_uninstall();
              out.uninstalled.push({ hook: 'jsvmp_transparent',
                                     restored: (r && r.restored) || [] });
            } catch (e) { out.errors.push('transparent_uninstall: ' + e.message); }
          }
          if (window.__mcp_hook_uninstallers &&
              Object.keys(window.__mcp_hook_uninstallers).length === 0) {
            try { delete window.__mcp_hook_uninstallers; } catch(e) {}
          }
          return out;
        })();
        """
        pages = list(browser_manager.pages.items())
        if not pages:
            pages = [("active", active_page)]
        for page_name, page in pages:
            try:
                in_page = await page.evaluate(uninstall_js)
                for item in (in_page.get("uninstalled") or []):
                    hook = item.get("hook")
                    items = item.get("restored") or []
                    if items:
                        restored.extend([f"{hook}:{n}" for n in items])
                    else:
                        restored.append(hook)
                for err in (in_page.get("errors") or []):
                    warnings.append(f"in-page uninstall ({page_name}): {err}")
            except Exception as e:
                warnings.append(f"in-page uninstall eval failed ({page_name}): {e}")

        cleared_init = len(browser_manager._init_scripts)
        browser_manager._init_scripts.clear()
        cleared_persistent = 0
        if not keep_persistent:
            cleared_persistent = len(browser_manager._persistent_scripts)
            browser_manager._persistent_scripts.clear()
            if cleared_persistent:
                recreated = await browser_manager.recreate_contexts()
                warnings.extend(recreated.get("warnings") or [])

        return {
            "status": "hooks_removed",
            "restored_objects": restored,
            "cleared_init_scripts": cleared_init,
            "cleared_persistent_scripts": cleared_persistent if not keep_persistent else 0,
            "persistent_kept": keep_persistent,
            "contexts_recreated": cleared_persistent > 0 and not keep_persistent,
            "warnings": warnings if warnings else None,
        }
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def get_console_logs(
    level: str | None = None,
    keyword: str | None = None,
    clear: bool = False,
) -> list[dict]:
    """Get console output collected from the page.

    Args:
        level: Filter by log level - "log", "warn", "error", or "info".
        keyword: Filter logs containing this keyword in the text.
        clear: If True, clear the log buffer after retrieval.

    Returns:
        List of dicts with level, text, timestamp, and location.
    """
    try:
        logs = list(browser_manager._console_logs)
        if level:
            logs = [l for l in logs if l["level"] == level]
        if keyword:
            logs = [l for l in logs if keyword in (l.get("text") or "")]
        if clear:
            browser_manager._console_logs.clear()
        return logs
    except Exception as e:
        return [error_response(e)]
