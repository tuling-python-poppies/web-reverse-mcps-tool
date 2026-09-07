# 模块说明: 浏览器启动、导航、截图、点击、输入和状态重置等基础交互工具。
from __future__ import annotations

import asyncio
import base64
import json as _json
import os
from typing import Literal

from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response

_PRE_INJECT_REGISTER_TIMEOUT = 10.0


def _validate_dimension_pair(
    width: int | None,
    height: int | None,
    width_name: str,
    height_name: str,
) -> str | None:
    if (width is None) != (height is None):
        return f"{width_name} and {height_name} must be provided together"
    if width is not None and height is not None and (width <= 0 or height <= 0):
        return f"{width_name} and {height_name} must be positive"
    return None


@mcp.tool()
async def launch_browser(
    headless: bool = True,
    os_type: str = "auto",
    locale: str = "auto",
    proxy: str | None = None,
    humanize: bool = False,
    geoip: bool = False,
    block_images: bool = False,
    block_webrtc: bool = False,
    block_webgl: bool = False,
    webgl_config: list[str] | None = None,
    addons: list[str] | None = None,
    exclude_addons: list[str] | None = None,
    fonts: list[str] | None = None,
    custom_fonts_only: bool | None = None,
    enable_cache: bool | None = None,
    main_world_eval: bool | None = None,
    firefox_user_prefs: dict | None = None,
    args: list[str] | None = None,
    enable_trace: bool = False,
    window_width: int | None = None,
    window_height: int | None = None,
    no_viewport: bool = True,
    disable_coop: bool | None = None,
    ff_version: int | None = None,
    screen_min_width: int | None = None,
    screen_max_width: int | None = None,
    screen_min_height: int | None = None,
    screen_max_height: int | None = None,
    custom_fingerprint: dict | None = None,
    virtual_display: str | None = None,
    viewport_width: int | None = None,
    viewport_height: int | None = None,
    ws_endpoint: str | None = None,
) -> dict:
    """Launch the Camoufox anti-detection browser, or attach to a running one.

    Args:
        headless: Run in headless mode (default True). Set False only when a visible browser is needed.
        os_type: OS fingerprint - "auto", "windows", "macos", or "linux".
        locale: Browser locale (e.g. "zh-CN"). "auto" detects system locale.
        proxy: Proxy server URL (e.g. "http://127.0.0.1:7890").
        humanize: Enable humanized mouse movement.
        geoip: Auto-infer geolocation from proxy IP.
        block_images: Block image loading.
        block_webrtc: Block WebRTC to prevent IP leaks.
        block_webgl: Disable WebGL APIs.
        webgl_config: [vendor, renderer] override for WebGL fingerprint.
        addons: Firefox addon paths to load.
        exclude_addons: Built-in Camoufox addons to exclude.
        fonts: Custom font list for font fingerprint control.
        custom_fonts_only: Use only the provided custom fonts.
        enable_cache: Enable/disable browser cache.
        main_world_eval: Enable main-world evaluation when supported by Camoufox.
        firefox_user_prefs: Extra Firefox user prefs.
        args: Extra browser process args.
        enable_trace: Enable engine-level property access tracing.
            Requires camoufox-reverse custom browser build.
            When enabled, use trace_property_access() to capture DOM access.
        window_width: Initial browser window width in px. Pass with window_height to
            request a fixed OS window size. Dynamic no_viewport mode ignores this pair
            so the page can follow native window resizing.
        window_height: Initial browser window height in px (see window_width).
        no_viewport: When True (default), the page viewport tracks the real window size.
            This fixes the headful "black border" / non-resizable window bug. Set False
            only if you need a fixed emulated viewport.
        disable_coop: Disable Cross-Origin-Opener-Policy to allow clicking elements in
            cross-origin iframes (e.g., Turnstile checkbox, reCAPTCHA).
        ff_version: Firefox version to emulate (e.g., 115). Use cautiously to prevent
            fingerprint leaks. Only use for special cases.
        screen_min_width: Minimum screen width constraint for fingerprint generation.
        screen_max_width: Maximum screen width constraint for fingerprint generation.
        screen_min_height: Minimum screen height constraint for fingerprint generation.
        screen_max_height: Maximum screen height constraint for fingerprint generation.
        custom_fingerprint: Custom BrowserForge fingerprint dict. Use to replicate a
            known-working fingerprint configuration.
        virtual_display: Virtual display number (e.g., ':99'). Linux only, for headless
            environments without X11.
        viewport_width: Fixed viewport width in px (disables no_viewport).
        viewport_height: Fixed viewport height in px (disables no_viewport).
        ws_endpoint: Attach to an already-running Camoufox server instead of
            launching a new browser. Start the server with
            `python -m camoufox server`, copy its "Websocket endpoint:
            ws://127.0.0.1:<port>/<guid>" line, and pass that full URL here.
            When set, all other launch args (os_type/locale/proxy/...) are
            ignored — fingerprint config is owned by the running server. Start the
            server with an os fingerprint matching the host for font-metric parity
            (attach mode cannot inject the host/os font-fallback shim that launch
            mode does). close_browser() will only disconnect; the server keeps running.

    Returns:
        dict with status, config, and page list.
    """
    try:
        if ws_endpoint:
            # Attach mode: only the endpoint matters; the server owns the config.
            result = await browser_manager.launch({"ws_endpoint": ws_endpoint})
            if result.get("status") == "already_running":
                result.setdefault("warnings", []).append(
                    "A browser is already active. Call close_browser() before attaching."
                )
            return result

        dimension_error = _validate_dimension_pair(
            window_width, window_height, "window_width", "window_height"
        ) or _validate_dimension_pair(
            viewport_width, viewport_height, "viewport_width", "viewport_height"
        )
        if dimension_error:
            return error_response(dimension_error)

        config = {
            "headless": headless, "os": os_type, "locale": locale,
            "humanize": humanize, "geoip": geoip,
            "block_images": block_images, "block_webrtc": block_webrtc,
            "block_webgl": block_webgl, "webgl_config": webgl_config,
            "addons": addons, "exclude_addons": exclude_addons,
            "fonts": fonts, "custom_fonts_only": custom_fonts_only,
            "enable_cache": enable_cache, "main_world_eval": main_world_eval,
            "firefox_user_prefs": firefox_user_prefs, "args": args,
            "enable_trace": enable_trace, "no_viewport": no_viewport,
            "disable_coop": disable_coop,
            "ff_version": ff_version,
            "virtual_display": virtual_display,
        }

        # Handle screen constraints for fingerprint generation
        if any([screen_min_width, screen_max_width, screen_min_height, screen_max_height]):
            config["screen"] = {
                "min_width": screen_min_width,
                "max_width": screen_max_width,
                "min_height": screen_min_height,
                "max_height": screen_max_height,
            }

        # Handle custom fingerprint
        if custom_fingerprint:
            config["fingerprint"] = custom_fingerprint

        # Handle window size
        if window_width and window_height:
            config["window"] = (window_width, window_height)

        # Handle viewport size (mutually exclusive with no_viewport)
        if viewport_width and viewport_height:
            config["viewport"] = (viewport_width, viewport_height)
            config["no_viewport"] = False  # viewport overrides no_viewport

        if proxy:
            config["proxy"] = {"server": proxy}
        result = await browser_manager.launch(config)

        if result.get("status") == "already_running":
            result["persistent_scripts_count"] = len(browser_manager._persistent_scripts)
            result["active_captures"] = browser_manager._capturing
            result["captured_requests_count"] = len(browser_manager._network_requests)
            from .instrumentation import _active_routes
            result["active_routes"] = len(_active_routes)
            has_residuals = (
                len(browser_manager._persistent_scripts) > 0
                or len(browser_manager._network_requests) > 0
                or len(_active_routes) > 0
            )
            if has_residuals:
                result.setdefault("warnings", []).append(
                    "browser already running with residual state. "
                    "Call reset_browser_state() or close_browser() + launch_browser()."
                )

        return result
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def close_browser() -> dict:
    """Close the Camoufox browser and release all resources.

    Owned-launch mode fully shuts the browser down. Attach mode only disconnects
    the local Playwright client; the external Camoufox server keeps running.
    """
    try:
        return await browser_manager.close()
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def navigate(
    url: str,
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
    pre_inject_hooks: list[str] | None = None,
    collect_response_chain: bool = True,
    clear_network_capture: bool = True,
) -> dict:
    """Navigate to a URL, with optional hook pre-injection and redirect tracing.

    Args:
        url: Target URL.
        wait_until: "load", "domcontentloaded", or "networkidle".
        pre_inject_hooks: Hook preset names to register before navigation.
        collect_response_chain: Record responses for final_status resolution.
        clear_network_capture: Clear stale network buffer before navigating.

    Returns:
        dict with url, title, initial_status, final_status, redirect_chain,
        hooks_injected, reloaded, warnings.
    """
    try:
        page = await browser_manager.get_active_page()
        warnings: list[str] = []
        hooks_injected: list[str] = []

        if clear_network_capture:
            try:
                cleared_count = await browser_manager.clear_network_capture(stop=False)
                if cleared_count > 0:
                    warnings.append(f"cleared {cleared_count} stale network requests")
            except Exception as e:
                warnings.append(f"network capture clear failed: {e}")

        if collect_response_chain:
            browser_manager.reset_nav_responses()

        # Register hooks BEFORE navigation so they fire on the first page load.
        # Context-level init_scripts execute before any page JS on each navigation.
        if pre_inject_hooks:
            for name in pre_inject_hooks:
                ok, msg = await _inject_hook_by_name(name)
                if ok:
                    hooks_injected.append(name)
                else:
                    warnings.append(f"hook '{name}' failed: {msg}")

        navigation_timed_out = False
        try:
            resp = await page.goto(url, wait_until=wait_until, timeout=30000)
        except Exception as e:
            msg = str(e).lower()
            if "timeout" in msg or "exceeded" in msg or "waiting" in msg:
                warnings.append(f"goto timeout for '{wait_until}'; checking usability")
                try:
                    dom_ready = await page.evaluate("document.readyState")
                    current_url = page.url
                    if dom_ready in ("interactive", "complete") and current_url != "about:blank":
                        warnings.append(f"page usable (readyState={dom_ready})")
                        resp = None
                        navigation_timed_out = True
                    else:
                        raise
                except Exception:
                    raise
            else:
                raise
        initial_status = resp.status if resp else None

        # No longer auto-reload after hook injection. Hooks are registered as
        # context-level init_scripts which fire automatically on page.goto().
        # The old auto-reload caused double navigation, which could trigger
        # anti-bot detection and lose 302-chain cookies.
        reloaded = False

        final_status = None
        chain = []
        if collect_response_chain:
            chain = list(browser_manager._nav_responses)
            for r in reversed(chain):
                if r["url"] == page.url or r.get("resource_type") == "document":
                    final_status = r["status"]
                    break

        return {
            "url": page.url, "title": await page.title(),
            "initial_status": initial_status,
            "final_status": final_status if final_status is not None else initial_status,
            "redirect_chain": chain if collect_response_chain else None,
            "hooks_injected": hooks_injected, "reloaded": reloaded,
            "navigation_timed_out": navigation_timed_out,
            "warnings": warnings if warnings else None,
        }
    except Exception as e:
        return error_response(e)


async def _inject_hook_by_name(name: str) -> tuple[bool, str]:
    """Register a hook as a persistent context-level script."""
    hooks_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks")
    preset_files = {
        "xhr": "xhr_hook.js", "fetch": "fetch_hook.js",
        "crypto": "crypto_hook.js", "websocket": "websocket_hook.js",
        "debugger_bypass": "debugger_trap.js",
        "cookie": "cookie_hook.js", "cookie_hook": "cookie_hook.js",
        "runtime_probe": "runtime_probe.js",
    }
    try:
        if name == "jsvmp_probe":
            with open(os.path.join(hooks_dir, "jsvmp_hook.js"), "r", encoding="utf-8") as f:
                tpl = f.read()
            default_proxy = ["navigator", "screen", "history", "localStorage",
                             "sessionStorage", "performance"]
            js = (tpl.replace("{{SCRIPT_URL}}", "").replace("{{MAX_ENTRIES}}", "10000")
                .replace("{{TRACK_CALLS}}", "true").replace("{{TRACK_PROPS}}", "true")
                .replace("{{TRACK_REFLECT}}", "true")
                .replace("'{{PROXY_OBJECTS}}'", _json.dumps(_json.dumps(default_proxy))))
            persist_name = "pre_inject:jsvmp_probe"
        elif name == "jsvmp_probe_transparent":
            hook_path = os.path.join(hooks_dir, "jsvmp_transparent_hook.js")
            if not os.path.exists(hook_path):
                return False, "jsvmp_transparent_hook.js not found"
            with open(hook_path, "r", encoding="utf-8") as f:
                tpl = f.read()
            js = tpl.replace("{{SCRIPT_URL}}", "").replace("{{MAX_ENTRIES}}", "10000")
            persist_name = "pre_inject:jsvmp_probe_transparent"
        elif name in preset_files:
            fpath = os.path.join(hooks_dir, preset_files[name])
            if not os.path.exists(fpath):
                return False, f"hook file not found: {preset_files[name]}"
            with open(fpath, "r", encoding="utf-8") as f:
                js = f.read()
            persist_name = f"pre_inject:{name}"
        else:
            return False, f"unknown hook name: {name}"
    except Exception as e:
        return False, f"prepare failed: {e}"
    try:
        added = await asyncio.wait_for(
            browser_manager.add_persistent_script(persist_name, js),
            timeout=_PRE_INJECT_REGISTER_TIMEOUT,
        )
        return True, "already registered" if added is False else "ok"
    except asyncio.TimeoutError:
        return False, "add_persistent_script timed out (10s)"
    except Exception as e:
        return False, f"add_persistent_script failed: {e}"


@mcp.tool()
async def reload(wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load") -> dict:
    """Reload the current page, preserving any init scripts."""
    try:
        page = await browser_manager.get_active_page()
        current_url = page.url
        if not current_url or current_url == "about:blank":
            return error_response("No page loaded to reload")
        resp = await page.reload(wait_until=wait_until)
        return {
            "url": page.url,
            "title": await page.title(),
            "status": resp.status if resp else None,
        }
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def take_screenshot(full_page: bool = False, selector: str | None = None) -> dict:
    """Take a screenshot of the current page or a specific element.

    Args:
        full_page: Capture the entire scrollable page.
        selector: CSS selector of a specific element to capture.
    """
    try:
        page = await browser_manager.get_active_page()
        if selector:
            elem = await page.query_selector(selector)
            if not elem:
                return error_response(f"Element not found: {selector}")
            data = await elem.screenshot()
        else:
            data = await page.screenshot(full_page=full_page)
        return {"screenshot_base64": base64.b64encode(data).decode(), "format": "png"}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def take_snapshot() -> dict:
    """Get the accessibility tree of the current page (token-efficient)."""
    try:
        page = await browser_manager.get_active_page()
        try:
            snapshot = await getattr(page, "accessibility").snapshot()
        except AttributeError:
            snapshot = await page.evaluate("""() => {
                function walk(node) {
                    if (!node) return null;
                    const item = {};
                    const tag = node.tagName ? node.tagName.toLowerCase() : '';
                    const role = node.getAttribute ? (node.getAttribute('role') || tag) : '';
                    if (role) item.role = role;
                    const name = node.getAttribute ? (node.getAttribute('aria-label')
                        || node.getAttribute('alt') || node.getAttribute('title')
                        || (node.tagName === 'INPUT' ? node.getAttribute('placeholder') : '')
                        || '') : '';
                    if (name) item.name = name;
                    if (['INPUT','TEXTAREA','SELECT'].includes(node.tagName)) item.value = node.value || '';
                    const text = [], children = [];
                    for (const child of (node.childNodes || [])) {
                        if (child.nodeType === 3) { const t = child.textContent.trim(); if (t) text.push(t); }
                        else if (child.nodeType === 1) { const c = walk(child); if (c) children.push(c); }
                    }
                    if (text.length && !children.length) item.text = text.join(' ');
                    if (children.length) item.children = children;
                    if (!item.role && !item.name && !item.text && !children.length) return null;
                    return item;
                }
                return walk(document.body);
            }""")
        return {"snapshot": snapshot}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def click(selector: str) -> dict:
    """Click on a page element."""
    try:
        page = await browser_manager.get_active_page()
        await page.click(selector)
        return {"status": "clicked", "selector": selector}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def type_text(selector: str, text: str, delay: int = 50) -> dict:
    """Type text into an input field with realistic keystroke delays."""
    try:
        page = await browser_manager.get_active_page()
        await page.type(selector, text, delay=delay)
        return {"status": "typed", "selector": selector, "character_count": len(text)}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def wait_for(
    selector: str | None = None,
    url_pattern: str | None = None,
    timeout: int = 30000,
    mode: Literal["navigation", "request", "response"] = "request",
) -> dict:
    """Wait for an element to appear or a network request/response matching a URL pattern.

    Args:
        selector: CSS selector to wait for.
        url_pattern: URL substring (or glob for navigation mode) to wait for.
        timeout: Timeout in milliseconds (default 30000).
        mode: For url_pattern only.
          "request"    — wait for any outgoing request whose URL contains
                         url_pattern. Uses Playwright request events. (default)
          "response"   — wait for any network response matching url_pattern.
                         Uses Playwright response events.
          "navigation" — wait for the page URL itself to change (old behaviour).
                         Uses page.wait_for_url(). Only meaningful after a link
                         click or form submit that triggers a full navigation.

    Returns:
        dict with status and matched url / selector.
    """
    try:
        page = await browser_manager.get_active_page()
        if selector:
            await page.wait_for_selector(selector, timeout=timeout)
            return {"status": "found", "selector": selector}
        elif url_pattern:
            if mode == "navigation":
                # Original behaviour: wait for page URL to match (navigation).
                await page.wait_for_url(url_pattern, timeout=timeout)
                return {"status": "matched", "url_pattern": url_pattern,
                        "mode": "navigation", "page_url": page.url}
            elif mode in ("request", "response"):
                start_id = max((r.get("id", 0) for r in browser_manager._network_requests), default=0)
                was_capturing = browser_manager._capturing
                old_pattern = browser_manager._capture_pattern
                old_capture_body = browser_manager._capture_body
                old_capture_page_id = browser_manager._capture_page_id
                if not was_capturing:
                    browser_manager._capturing = True
                    browser_manager._capture_pattern = "**/*"
                    browser_manager._capture_body = False
                    browser_manager._capture_page_id = id(page)

                async def wait_from_event():
                    event_name = "response" if mode == "response" else "request"
                    obj = await page.wait_for_event(
                        event_name,
                        predicate=lambda r: url_pattern in r.url,
                        timeout=timeout,
                    )
                    result = {"status": "matched", "url_pattern": url_pattern,
                              "mode": mode, "matched_url": obj.url}
                    if mode == "response":
                        result["status_code"] = obj.status
                    else:
                        result["method"] = obj.method
                    return result

                async def wait_from_capture():
                    deadline = asyncio.get_running_loop().time() + timeout / 1000
                    while asyncio.get_running_loop().time() < deadline:
                        for req in browser_manager._network_requests:
                            if req.get("id", 0) <= start_id:
                                continue
                            if url_pattern not in req.get("url", ""):
                                continue
                            if mode == "response" and req.get("status") is None:
                                continue
                            result = {"status": "matched", "url_pattern": url_pattern,
                                      "mode": mode, "matched_url": req.get("url")}
                            if mode == "response":
                                result["status_code"] = req.get("status")
                            else:
                                result["method"] = req.get("method")
                            return result
                        await asyncio.sleep(0.05)
                    raise TimeoutError(f"Timed out waiting for {mode} matching {url_pattern}")

                tasks = [asyncio.create_task(wait_from_event()),
                         asyncio.create_task(wait_from_capture())]
                try:
                    pending = set(tasks)
                    errors = []
                    deadline = asyncio.get_running_loop().time() + timeout / 1000
                    while pending:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        done, pending = await asyncio.wait(
                            pending, timeout=remaining,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            break
                        for task in done:
                            try:
                                return task.result()
                            except Exception as e:
                                errors.append(str(e))
                    if errors:
                        return error_response("; ".join(errors))
                    return error_response(f"Timed out waiting for {mode} matching {url_pattern}")
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    # Await cancelled tasks so the event loop can clean them up.
                    # Without this, Python emits "Task was destroyed but it is
                    # pending!" warnings in asyncio debug mode.
                    await asyncio.gather(*tasks, return_exceptions=True)
                    if not was_capturing:
                        browser_manager._capturing = False
                        browser_manager._capture_pattern = old_pattern
                        browser_manager._capture_body = old_capture_body
                        browser_manager._capture_page_id = old_capture_page_id
        else:
            return error_response("Provide either selector or url_pattern")
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def get_page_info() -> dict:
    """Get current page URL, title, and viewport size."""
    try:
        page = await browser_manager.get_active_page()
        viewport = page.viewport_size or {}
        actual = await page.evaluate("""() => ({
            inner_width: window.innerWidth,
            inner_height: window.innerHeight,
            outer_width: window.outerWidth,
            outer_height: window.outerHeight,
            device_pixel_ratio: window.devicePixelRatio,
            visual_viewport_width: window.visualViewport ? window.visualViewport.width : null,
            visual_viewport_height: window.visualViewport ? window.visualViewport.height : null,
            screen_width: screen.width,
            screen_height: screen.height,
            screen_avail_width: screen.availWidth,
            screen_avail_height: screen.availHeight,
        })""")
        return {
            "url": page.url, "title": await page.title(),
            "viewport_width": viewport.get("width"),
            "viewport_height": viewport.get("height"),
            "window_inner_width": actual.get("inner_width"),
            "window_inner_height": actual.get("inner_height"),
            "window_outer_width": actual.get("outer_width"),
            "window_outer_height": actual.get("outer_height"),
            "device_pixel_ratio": actual.get("device_pixel_ratio"),
            "visual_viewport_width": actual.get("visual_viewport_width"),
            "visual_viewport_height": actual.get("visual_viewport_height"),
            "screen_width": actual.get("screen_width"),
            "screen_height": actual.get("screen_height"),
            "screen_avail_width": actual.get("screen_avail_width"),
            "screen_avail_height": actual.get("screen_avail_height"),
        }
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def set_window_size(width: int, height: int) -> dict:
    """Resize the browser at runtime.

    Behaviour depends on how the browser was launched:
      - Fixed-viewport mode (launched with no_viewport=False): resizes the emulated
        viewport. This works reliably.
      - Default no_viewport mode: Firefox blocks scripted resizing of the main
        window (window.resizeTo only affects script-opened popups), so a runtime
        programmatic resize is NOT possible. To change the size, either drag the
        window edge manually, or relaunch with launch_browser(window_width=...,
        window_height=...). This tool reports that limitation honestly instead of
        silently no-opping.

    Args:
        width: Target width in px.
        height: Target height in px.
    """
    try:
        if width <= 0 or height <= 0:
            return error_response("width and height must be positive integers")
        page = await browser_manager.get_active_page()
        no_viewport = browser_manager._context_kwargs.get("no_viewport", False)

        if not no_viewport:
            # Fixed emulated viewport: set_viewport_size is the supported path.
            await page.set_viewport_size({"width": width, "height": height})
            actual = await page.evaluate("""() => ({
                iw: window.innerWidth, ih: window.innerHeight,
                ow: window.outerWidth, oh: window.outerHeight,
                vw: window.visualViewport ? window.visualViewport.width : null,
                vh: window.visualViewport ? window.visualViewport.height : null})""")
            viewport = page.viewport_size or {}
            js_viewport_matches = (
                (actual.get("iw") == width and actual.get("ih") == height)
                or (actual.get("vw") == width and actual.get("vh") == height)
            )
            if not js_viewport_matches:
                return {
                    "status": "viewport_metadata_updated",
                    "method": "viewport",
                    "requested": {"width": width, "height": height},
                    "page_viewport_width": viewport.get("width"),
                    "page_viewport_height": viewport.get("height"),
                    "window_inner_width": actual.get("iw"),
                    "window_inner_height": actual.get("ih"),
                    "window_outer_width": actual.get("ow"),
                    "window_outer_height": actual.get("oh"),
                    "visual_viewport_width": actual.get("vw"),
                    "visual_viewport_height": actual.get("vh"),
                    "reason": "Playwright accepted set_viewport_size(), but page JS "
                              "still sees the real window size.",
                    "hint": "Relaunch with launch_browser(window_width=%d, "
                            "window_height=%d) to change site-visible dimensions."
                            % (width, height),
                }
            return {
                "status": "resized",
                "method": "viewport",
                "requested": {"width": width, "height": height},
                "page_viewport_width": viewport.get("width"),
                "page_viewport_height": viewport.get("height"),
                "window_inner_width": actual.get("iw"),
                "window_inner_height": actual.get("ih"),
                "window_outer_width": actual.get("ow"),
                "window_outer_height": actual.get("oh"),
                "visual_viewport_width": actual.get("vw"),
                "visual_viewport_height": actual.get("vh"),
            }

        # no_viewport mode: best-effort resizeTo, then verify whether it took effect.
        before = await page.evaluate("() => [window.outerWidth, window.outerHeight]")
        await page.evaluate("([w, h]) => { try { window.resizeTo(w, h); } catch (e) {} }",
                            [width, height])
        await page.wait_for_timeout(150)
        after = await page.evaluate("() => [window.outerWidth, window.outerHeight]")
        if after != before:
            return {
                "status": "resized",
                "method": "window.resizeTo",
                "requested": {"width": width, "height": height},
                "window_outer_width": after[0],
                "window_outer_height": after[1],
            }
        return {
            "status": "not_supported",
            "reason": "Firefox blocks scripted resizing of the main window in "
                      "no_viewport mode.",
            "current_outer_width": after[0],
            "current_outer_height": after[1],
            "hint": "Drag the window edge manually, or relaunch with "
                    "launch_browser(window_width=%d, window_height=%d). The window "
                    "is freely drag-resizable now that the black-border viewport "
                    "lock is removed." % (width, height),
        }
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def reset_browser_state(
    clear_persistent_hooks: bool = True,
    clear_network_capture: bool = True,
    clear_active_routes: bool = True,
    clear_cookies: bool = False,
    clear_storage: bool = False,
) -> dict:
    """Reset MCP-side browser residual state without closing the browser.

    Args:
        clear_persistent_hooks: Remove all persistent init scripts.
        clear_network_capture: Clear network request buffer and stop captures.
        clear_active_routes: Clear instrumentation routes.
        clear_cookies: ALSO clear browser cookies (destructive; default False).
        clear_storage: ALSO clear localStorage/sessionStorage (default False).
    """
    from typing import Any
    result: dict[str, Any] = {"status": "reset"}
    try:
        if clear_persistent_hooks:
            try:
                from .hooking import remove_hooks
                r = await remove_hooks(keep_persistent=False)
                result["hooks_removed"] = r
            except Exception as e:
                result["hooks_remove_error"] = str(e)
        if clear_network_capture:
            count = await browser_manager.clear_network_capture(stop=True)
            browser_manager._capture_page_id = None
            result["network_requests_cleared"] = count
        if clear_active_routes:
            try:
                from .instrumentation import _active_routes, _stop
                count = len(_active_routes)
                await _stop(None)
                result["instrumentation_routes_cleared"] = count
            except Exception as e:
                result["instrumentation_clear_error"] = str(e)
            try:
                route_result = await browser_manager.clear_route_handlers()
                result["network_routes_cleared"] = route_result["removed"]
                result["network_route_errors"] = route_result["errors"]
            except Exception as e:
                result["network_routes_clear_error"] = str(e)
        if clear_cookies:
            try:
                ctx = browser_manager.contexts.get("default")
                if ctx:
                    await ctx.clear_cookies()
                    result["cookies_cleared"] = True
            except Exception as e:
                result["cookies_clear_error"] = str(e)
        if clear_storage:
            try:
                page = await browser_manager.get_active_page()
                await page.evaluate(
                    "() => { try { localStorage.clear(); } catch(e) {} "
                    "try { sessionStorage.clear(); } catch(e) {} }"
                )
                result["storage_cleared"] = True
            except Exception as e:
                result["storage_clear_error"] = str(e)
        return result
    except Exception as e:
        return error_response(e)
