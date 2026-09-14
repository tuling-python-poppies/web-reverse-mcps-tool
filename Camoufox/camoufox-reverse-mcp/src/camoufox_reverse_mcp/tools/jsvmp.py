# 模块说明: 提供 JSVMP 运行时探针与浏览器环境对照采集工具。
from __future__ import annotations

import json
import os
from typing import Literal

from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response
from ..utils.worlds import evaluate_in_world, wrapped_main_world_init_script


@mcp.tool()
async def hook_jsvmp_interpreter(
    script_url: str = "",
    persistent: bool = True,
    mode: Literal["proxy", "transparent"] = "proxy",
    track_calls: bool = True,
    track_props: bool = True,
    track_reflect: bool = True,
    proxy_objects: list[str] | None = None,
    max_entries: int = 10000,
) -> dict:
    """Install a JSVMP runtime probe.

    Multi-path instrumentation for JSVMP interpreters. Wraps Reflect.get/apply,
    installs Proxies on globals (navigator, screen, etc.), intercepts timing APIs.

    LIMITATIONS: "proxy" mode is DETECTABLE by RS/AK-style signature-based anti-bot.
    For those, use instrumentation(action='install') (source-level rewrite) or
    mode='transparent' instead.

    IMPORTANT — timing for sync-loaded SDKs (e.g. webmssdk):
        JSVMP interpreters capture native references at startup via closures.
        If you install hooks AFTER the SDK has loaded, the SDK's closures
        already hold the original (un-hooked) references — your hooks will
        never fire. You MUST install hooks BEFORE navigate():
          1. launch_browser()
          2. hook_jsvmp_interpreter(mode='transparent', persistent=True)
          3. navigate("https://www.douyin.com/...")
        If already navigated, call instrumentation(action='reload') after
        installing hooks to force a page reload with hooks active.

    Args:
        script_url: Target script URL substring for stack filtering.
        persistent: Survive navigation (default True).
        mode: "proxy" (full coverage, detectable) or "transparent" (safe, lower coverage).
        track_calls, track_props, track_reflect: Only for mode="proxy".
        proxy_objects: Objects to proxy (default: navigator, screen, etc.).
        max_entries: Log buffer cap (default 10000).

    Returns:
        dict with status, mode, coverage summary.
    """
    if not 1 <= max_entries <= 100_000:
        return error_response("max_entries must be between 1 and 100000")
    try:
        hooks_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks")
        page = await browser_manager.get_active_page()

        if mode == "transparent":
            hook_path = os.path.join(hooks_dir, "jsvmp_transparent_hook.js")
            if not os.path.exists(hook_path):
                return error_response("jsvmp_transparent_hook.js not found")
            with open(hook_path, "r", encoding="utf-8") as f:
                template = f.read()
            hook_js = (template
                .replace("{{SCRIPT_URL}}", script_url.replace('"', '\\"').replace("'", "\\'"))
                .replace("{{MAX_ENTRIES}}", str(max_entries)))
            persistent_js = wrapped_main_world_init_script(hook_js)
            if persistent:
                await browser_manager.add_persistent_script(
                    f"jsvmp_transparent:{script_url or 'all'}", persistent_js)
            # v1.0.1: detect if page already loaded (timing issue)
            page_already_loaded = page.url and page.url != "about:blank"
            try:
                await evaluate_in_world(page, hook_js, "main", script_is_function=False)
            except Exception as e:
                return {"status": "partial", "mode": "transparent",
                        "warning": f"Evaluate failed: {e}", "persistent": persistent}
            result = {
                "status": "instrumented", "mode": "transparent",
                "script_url": script_url or "(all)", "persistent": persistent,
                "data_location": "window.__mcp_jsvmp_log",
            }
            if page_already_loaded:
                result["warnings"] = [
                    "Hooks installed on already-loaded page. If the target "
                    "SDK (e.g. webmssdk) was loaded BEFORE this call, it "
                    "likely captured native references at startup (closure "
                    "capture) and won't trigger your hooks. Call "
                    "instrumentation(action='reload') or re-navigate to "
                    "force SDK re-init with hooks in place."
                ]
            return result

        elif mode == "proxy":
            if proxy_objects is None:
                proxy_objects = ["navigator", "screen", "history",
                                 "localStorage", "sessionStorage", "performance"]
            with open(os.path.join(hooks_dir, "jsvmp_hook.js"), "r", encoding="utf-8") as f:
                template = f.read()
            hook_js = (template
                .replace("{{SCRIPT_URL}}", script_url.replace('"', '\\"').replace("'", "\\'"))
                .replace("{{MAX_ENTRIES}}", str(max_entries))
                .replace("{{TRACK_CALLS}}", "true" if track_calls else "false")
                .replace("{{TRACK_PROPS}}", "true" if track_props else "false")
                .replace("{{TRACK_REFLECT}}", "true" if track_reflect else "false")
                .replace("'{{PROXY_OBJECTS}}'", json.dumps(json.dumps(proxy_objects))))
            persistent_js = wrapped_main_world_init_script(hook_js)
            if persistent:
                await browser_manager.add_persistent_script(
                    f"jsvmp_probe:{script_url or 'all'}", persistent_js)
            # v1.0.1: detect if page already loaded (timing issue)
            page_already_loaded = page.url and page.url != "about:blank"
            try:
                await evaluate_in_world(page, hook_js, "main", script_is_function=False)
            except Exception as e:
                return {"status": "partial", "mode": "proxy",
                        "warning": f"Evaluate failed: {e}", "persistent": persistent}
            result = {
                "status": "instrumented", "mode": "proxy",
                "script_url": script_url or "(all)", "persistent": persistent,
                "data_location": "window.__mcp_jsvmp_log",
                "warning": "proxy mode is detectable by RS/AK-style anti-bot.",
            }
            if page_already_loaded:
                result.setdefault("warnings", [])
                if isinstance(result.get("warning"), str):
                    result["warnings"].append(result.pop("warning"))
                result["warnings"].append(
                    "Hooks installed on already-loaded page. If the target "
                    "SDK was loaded BEFORE this call, it likely captured "
                    "native references at startup (closure capture) and "
                    "won't trigger your hooks. Call "
                    "instrumentation(action='reload') to re-trigger."
                )
            return result
        else:
            return error_response(f"unknown mode '{mode}', use 'proxy' or 'transparent'")
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def compare_env(properties: list[str] | None = None) -> dict:
    """Collect browser environment fingerprint data for comparison with Node.js/jsdom.

    Args:
        properties: Optional list of specific properties to check.
            If omitted, checks navigator, screen, canvas, WebGL, audio, timing.

    Returns:
        dict with categorized environment data and their values.
    """
    try:
        page = await browser_manager.get_active_page()
        result = await page.evaluate("""(customProps) => {
            const result = { navigator: {}, screen: {}, canvas: {}, webgl: {},
                             audio: {}, timing: {}, misc: {}, custom: {} };
            const readPath = (path) => {
                if (!/^[A-Za-z_$][\\w$]*(\\.[A-Za-z_$][\\w$]*|\\[(\\d+|"[^"]+"|'[^']+')\\])*$/.test(path)) {
                    throw new Error('__UNSUPPORTED_PATH__');
                }
                const tokens = [];
                path.replace(/([A-Za-z_$][\\w$]*)|\\[(\\d+|"[^"]+"|'[^']+')\\]/g, (m, ident, bracket) => {
                    if (ident) tokens.push(ident);
                    else if (/^\\d+$/.test(bracket)) tokens.push(Number(bracket));
                    else tokens.push(bracket.slice(1, -1));
                    return m;
                });
                let cur = window;
                for (const token of tokens) cur = cur[token];
                return cur;
            };
            const navProps = ['userAgent', 'platform', 'language', 'languages',
                'hardwareConcurrency', 'deviceMemory', 'maxTouchPoints',
                'vendor', 'cookieEnabled', 'webdriver'];
            for (const p of navProps) {
                try { result.navigator[p] = { value: String(navigator[p]), type: typeof navigator[p] }; }
                catch(e) { result.navigator[p] = { value: null, error: e.message }; }
            }
            const screenProps = ['width', 'height', 'availWidth', 'availHeight', 'colorDepth'];
            for (const p of screenProps) {
                try { result.screen[p] = { value: screen[p], type: typeof screen[p] }; }
                catch(e) { result.screen[p] = { value: null, error: e.message }; }
            }
            result.screen.devicePixelRatio = { value: window.devicePixelRatio, type: 'number' };
            result.timing.timezoneOffset = { value: new Date().getTimezoneOffset(), type: 'number' };
            result.timing.timezone = { value: Intl.DateTimeFormat().resolvedOptions().timeZone, type: 'string' };
            for (const prop of customProps || []) {
                try {
                    let val;
                    try {
                        val = readPath(prop);
                    } catch (pathErr) {
                        if (String(pathErr && pathErr.message) !== '__UNSUPPORTED_PATH__') throw pathErr;
                        val = Function('return (' + prop + ');')();
                    }
                    result.custom[prop] = {
                        value: typeof val === 'object' ? JSON.stringify(val).substring(0, 500) : String(val),
                        type: typeof val
                    };
                } catch(e) {
                    result.custom[prop] = { value: null, error: e.message };
                }
            }
            return result;
        }""", properties or [])
        return result
    except Exception as e:
        return error_response(e)
