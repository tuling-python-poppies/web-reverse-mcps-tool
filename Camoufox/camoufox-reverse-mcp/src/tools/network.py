# 模块说明: 网络抓包、请求详情、请求发起栈和拦截控制工具。
from __future__ import annotations

import time
from typing import Literal

from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}


def _redact_headers(headers: dict | None) -> dict | None:
    if headers is None:
        return None
    return {
        key: ("<redacted>" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


@mcp.tool()
async def network_capture(
    action: Literal["start", "stop", "clear", "status"],
    url_pattern: str = "**/*",
    capture_body: bool = False,
) -> dict:
    """Unified network capture control (v0.9.0).

    Replaces start_network_capture / stop_network_capture.

    Args:
        action:
          "start"  — begin capturing network events
          "stop"   — stop capturing (buffer retained)
          "clear"  — clear the capture buffer
          "status" — return current capture state
        url_pattern: Glob pattern for "start" (default "**/*" captures all).
        capture_body: For "start" only; capture response bodies (more memory).

    Returns:
        dict with action result + current status snapshot.
    """
    if action == "start":
        page = await browser_manager.get_active_page()
        browser_manager._capturing = True
        browser_manager._capture_pattern = url_pattern
        browser_manager._capture_body = capture_body
        browser_manager._capture_page_id = id(page)
        return {"status": "capturing", "pattern": url_pattern,
                "capture_body": capture_body,
                "page": browser_manager.active_page_name}
    elif action == "stop":
        browser_manager._capturing = False
        browser_manager._capture_page_id = None
        return {"status": "stopped",
                "total_requests": len(browser_manager._network_requests)}
    elif action == "clear":
        count = await browser_manager.clear_network_capture(stop=False)
        return {"status": "cleared", "cleared_count": count}
    elif action == "status":
        return {
            "active": browser_manager._capturing,
            "pattern": browser_manager._capture_pattern,
            "capture_body": browser_manager._capture_body,
            "buffer_size": len(browser_manager._network_requests),
            "page": browser_manager.active_page_name if browser_manager._capture_page_id else None,
        }
    else:
        return error_response(f"unknown action: {action}. Use start/stop/clear/status")


@mcp.tool()
async def list_network_requests(
    url_filter: str | None = None,
    url_contains_domain: str | None = None,
    method: str | None = None,
    resource_type: str | None = None,
    status_code: int | None = None,
) -> list[dict]:
    """List captured network requests with optional filters.

    Args:
        url_filter: Substring filter for request URLs.
        url_contains_domain: Convenience domain filter (e.g. 'nmpa.gov.cn').
        method: HTTP method filter (e.g. "GET", "POST").
        resource_type: Resource type filter (e.g. "xhr", "fetch", "script", "document").
        status_code: HTTP status code filter.

    Returns:
        List of request summaries with id, url, method, status, type, ms, size.
    """
    try:
        reqs = list(browser_manager._network_requests)
        if url_filter:
            reqs = [r for r in reqs if url_filter in r["url"]]
        if url_contains_domain:
            reqs = [r for r in reqs if url_contains_domain in r.get("url", "")]
        if method:
            reqs = [r for r in reqs if r["method"].upper() == method.upper()]
        if resource_type:
            reqs = [r for r in reqs if r.get("resource_type", "").lower() == resource_type.lower()]
        if status_code is not None:
            reqs = [r for r in reqs if r.get("status") == status_code]

        summaries = []
        for r in reqs:
            body_size = len(r["response_body"]) if r.get("response_body") else 0
            summaries.append({
                "id": r["id"], "url": r["url"][:200], "method": r["method"],
                "status": r.get("status"), "type": r.get("resource_type"),
                "ms": r.get("duration"), "size": body_size,
                "has_body": body_size > 0,
            })
        return summaries
    except Exception as e:
        return [error_response(e)]


@mcp.tool()
async def get_network_request(
    request_id: int,
    include_body: bool = False,
    include_headers: bool = True,
    include_sensitive_headers: bool = False,
    max_body_size: int = 5000,
) -> dict:
    """Get full details of a specific captured network request.

    Args:
        request_id: The ID of the request (from list_network_requests).
        include_body: Include response body (default False).
        include_headers: Include request/response headers (default True).
        include_sensitive_headers: Return auth/cookie headers without redaction.
        max_body_size: Max chars of body when include_body=True. Pass -1 for unlimited.

    Returns:
        dict with request and response details.
    """
    try:
        for r in browser_manager._network_requests:
            if r["id"] == request_id:
                result = dict(r)
                result.pop("_request_key", None)
                if not include_body:
                    body = result.pop("response_body", None)
                    result["response_body_available"] = body is not None
                    if body:
                        result["response_body_size"] = len(body)
                else:
                    body = result.get("response_body")
                    if body is not None and max_body_size >= 0 and len(body) > max_body_size:
                        result["response_body"] = body[:max_body_size]
                        result["response_body_truncated"] = True
                        result["response_body_original_size"] = len(body)
                        result["response_body_size_returned"] = max_body_size
                    elif body is not None:
                        result["response_body_truncated"] = False
                        result["response_body_original_size"] = len(body)
                        result["response_body_size_returned"] = len(body)
                if not include_headers:
                    result.pop("request_headers", None)
                    result.pop("response_headers", None)
                elif not include_sensitive_headers:
                    result["request_headers"] = _redact_headers(result.get("request_headers"))
                    result["response_headers"] = _redact_headers(result.get("response_headers"))
                return result
        return error_response(f"Request ID {request_id} not found")
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def get_request_initiator(request_id: int) -> dict:
    """Get the JS call stack that initiated a network request.

    Golden path: see encrypted param -> get_request_initiator -> find signing function.
    Requires inject_hook_preset("xhr"/"fetch") BEFORE navigating.

    KNOWN LIMITATIONS (v0.8.1+):
      1. For requests modified by an interceptor registered BEFORE MCP's
         hooks (e.g. SDKs loaded via sync <script>), the initiator will be
         the interceptor's call, not the original business code.
         Workaround: use reload_with_hooks().
      2. For fetch on Firefox, Playwright-native initiator is often null.
         Requires inject_hook_preset('fetch', persistent=True).

    Args:
        request_id: The ID of the request.

    Returns:
        dict with url, initiator_stack, source, diagnostics.
    """
    try:
        target_entry = None
        for r in browser_manager._network_requests:
            if r["id"] == request_id:
                target_entry = r
                break
        if target_entry is None:
            return error_response(f"Request ID {request_id} not found")

        page = await browser_manager.get_active_page()
        req_url = target_entry["url"]
        target_type = target_entry.get("resource_type")

        result = await page.evaluate("""({reqUrl, targetType}) => {
            // Priority-based URL matching to prevent cross-type mismatches.
            // Level 0: exact match
            // Level 1: same host + pathname + search (ignores hash)
            // Level 2: same host + pathname (ignores query string)
            // Level 3: substring match only when the shorter string is >= 30 chars
            //          AND constitutes a significant fraction of the longer one.
            function urlMatchLevel(reqUrl, logUrl) {
                if (!logUrl) return -1;
                if (reqUrl === logUrl) return 0;
                try {
                    const u1 = new URL(reqUrl, location.origin);
                    const u2 = new URL(logUrl, location.origin);
                    if (u1.host !== u2.host) return -1;
                    if (u1.pathname === u2.pathname) {
                        // Same host + pathname: accept even if search differs
                        if (u1.search === u2.search) return 1;
                        return 2;
                    }
                } catch(e) {}
                // Substring match only for meaningfully long URLs to prevent false positives
                const shorter = reqUrl.length < logUrl.length ? reqUrl : logUrl;
                const longer  = reqUrl.length < logUrl.length ? logUrl  : reqUrl;
                if (shorter.length >= 30 && longer.includes(shorter)) return 3;
                return -1;
            }

            function buildEntry(log, type, logUrl, matchLevel) {
                return {
                    url: logUrl, stack: log.stack || null, type: type,
                    method: log.method, headers: log.headers,
                    body: log.body ? String(log.body).substring(0, 2000) : null,
                    timestamp: log.timestamp || log.ts || null,
                    match_level: matchLevel
                };
            }

            function searchLogs(logs, type) {
                if (!logs || !logs.length) return null;
                let bestLevel = 99, bestEntry = null;
                for (let i = logs.length - 1; i >= 0; i--) {
                    const log = logs[i];
                    const logUrl = log.url || '';
                    const lvl = urlMatchLevel(reqUrl, logUrl);
                    if (lvl >= 0 && lvl < bestLevel) {
                        bestLevel = lvl;
                        bestEntry = buildEntry(log, type, logUrl, lvl);
                        if (lvl === 0) break; // exact match, stop early
                    }
                }
                return bestEntry;
            }

            function typePenalty(entry) {
                if (!targetType) return 0;
                const normalizedTargetType = String(targetType).toLowerCase();
                const normalizedEntryType = String(entry.type || '').toLowerCase();
                if (normalizedEntryType === normalizedTargetType) return 0;
                if (normalizedTargetType === 'fetch' && normalizedEntryType === 'fetch_hook') return 0;
                return 1;
            }

            function pickBest(candidates) {
                candidates = candidates.filter(Boolean);
                if (!candidates.length) return null;
                candidates.sort((a, b) => {
                    const levelDiff = (a.match_level ?? 99) - (b.match_level ?? 99);
                    if (levelDiff) return levelDiff;
                    const typeDiff = typePenalty(a) - typePenalty(b);
                    if (typeDiff) return typeDiff;
                    return (b.timestamp || 0) - (a.timestamp || 0);
                });
                return candidates[0];
            }

            const xhrResult = searchLogs(window.__mcp_xhr_log, 'xhr');
            const fetchResult = searchLogs(window.__mcp_fetch_log, 'fetch');

            // Fallback: dedicated fetch initiator log
            const fetchInitLog = window.__mcp_fetch_initiator_log || [];
            let bestFetchLevel = 99, bestFetchEntry = null;
            for (let i = fetchInitLog.length - 1; i >= 0; i--) {
                const entry = fetchInitLog[i];
                const logUrl = entry.url || '';
                const lvl = urlMatchLevel(reqUrl, logUrl);
                if (lvl >= 0 && lvl < bestFetchLevel) {
                    bestFetchLevel = lvl;
                    bestFetchEntry = { url: logUrl, stack: entry.stack || null,
                                       type: 'fetch_hook', method: entry.method,
                                       timestamp: entry.ts, match_level: lvl };
                    if (lvl === 0) break;
                }
            }
            const best = pickBest([xhrResult, fetchResult, bestFetchEntry]);
            if (best) return best;
            return {
                url: reqUrl, stack: null, type: 'unknown',
                diagnostics: {
                    xhr_hook_active: !!window.__mcp_xhr_hooked,
                    fetch_hook_active: !!window.__mcp_fetch_hooked,
                    hint: !window.__mcp_xhr_hooked && !window.__mcp_fetch_hooked
                        ? 'No hooks detected. Call inject_hook_preset("xhr"/"fetch") BEFORE navigating.'
                        : 'Hooks active but no matching URL found in logs.'
                }
            };
        }""", {"reqUrl": req_url, "targetType": target_type})

        source = result.get("type", "unknown")
        return {
            "url": result.get("url"),
            "initiator_stack": result.get("stack"),
            "initiator_type": source,
            "source": source,
            "method": result.get("method"),
            "request_headers": result.get("headers"),
            "request_body": result.get("body"),
            "match_level": result.get("match_level"),
            "diagnostics": result.get("diagnostics"),
            "diagnostic": (
                {
                    "likely_causes": [
                        "hook registered after SDK (try reload_with_hooks)",
                        "request made inside a sync-loaded SDK interceptor",
                        "fetch_hook.js not injected",
                    ],
                    "recommended_action": "Use reload_with_hooks() or inject hooks before navigate.",
                }
                if source in ("unknown", None) else None
            ),
        }
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def intercept_request(
    url_pattern: str,
    action: Literal["log", "block", "modify", "mock", "stop"] = "log",
    modify_headers: dict | None = None,
    modify_body: str | None = None,
    mock_response: dict | None = None,
) -> dict:
    """Intercept network requests matching a pattern.

    Args:
        url_pattern: URL glob pattern (e.g. "**/api/login*").
        action: "log", "block", "modify", "mock", or "stop" (unroute).
        modify_headers: Headers to add/override (action="modify").
        modify_body: Request body replacement (action="modify").
        mock_response: Dict with "status", "headers", "body" (action="mock").
    """
    try:
        page = await browser_manager.get_active_page()
        route_key = (id(page), url_pattern)

        if action == "stop":
            if url_pattern:
                previous = browser_manager._route_handlers.get(route_key)
                if previous:
                    await page.unroute(url_pattern, previous["handler"])
                    browser_manager._route_handlers.pop(route_key, None)
                return {
                    "status": "stopped",
                    "pattern": url_pattern,
                    "removed": bool(previous),
                }
            else:
                page_id = id(page)
                keys = [key for key in browser_manager._route_handlers if key[0] == page_id]
                removed = 0
                for key in keys:
                    previous = browser_manager._route_handlers[key]
                    await page.unroute(previous["pattern"], previous["handler"])
                    browser_manager._route_handlers.pop(key, None)
                    removed += 1
                return {"status": "stopped_all", "removed": removed}

        async def handler(route):
            if action == "log":
                browser_manager._console_logs.append({
                    "level": "info",
                    "text": f"[INTERCEPT:log] {route.request.method} {route.request.url}",
                    "timestamp": time.time() * 1000, "location": None,
                })
                await route.continue_()
            elif action == "block":
                await route.abort()
            elif action == "modify":
                overrides = {}
                if modify_headers:
                    overrides["headers"] = {**dict(route.request.headers), **modify_headers}
                if modify_body is not None:
                    overrides["post_data"] = modify_body
                await route.continue_(**overrides)
            elif action == "mock":
                resp = mock_response or {}
                await route.fulfill(
                    status=resp.get("status", 200),
                    headers=resp.get("headers", {"content-type": "application/json"}),
                    body=resp.get("body", "{}"),
                )

        previous = browser_manager._route_handlers.get(route_key)
        if previous:
            await page.unroute(url_pattern, previous["handler"])
            browser_manager._route_handlers.pop(route_key, None)
        try:
            await page.route(url_pattern, handler)
        except Exception:
            if previous:
                try:
                    await page.route(url_pattern, previous["handler"])
                    browser_manager._route_handlers[route_key] = previous
                except Exception:
                    pass
            raise
        browser_manager._route_handlers[route_key] = {
            "owner": page,
            "pattern": url_pattern,
            "handler": handler,
        }
        return {"status": "intercepting", "pattern": url_pattern, "action": action}
    except Exception as e:
        return error_response(e)
