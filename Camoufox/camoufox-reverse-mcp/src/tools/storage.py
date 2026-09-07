# 模块说明: cookies、storage、浏览器状态导出导入等存储相关工具。
from __future__ import annotations

import os
import re
import json
import tempfile
from typing import Literal

from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response
from ..workspace import resolve_workspace_path


@mcp.tool()
async def cookies(
    action: Literal["get", "set", "delete"],
    domain: str | None = None,
    cookies_list: list[dict] | None = None,
    name: str | None = None,
) -> dict | list:
    """Cookie management (v0.9.0 unified).

    Replaces get_cookies / set_cookies / delete_cookies.

    Args:
        action:
          "get"   — return cookies (optionally filtered by domain)
          "set"   — set cookies (requires cookies_list: [{name, value, domain, ...}])
          "delete" — delete cookies (filter by name and/or domain; no filter = clear all)
        domain: Domain filter for "get" and "delete" (e.g. ".example.com").
        cookies_list: List of cookie dicts for "set".
        name: Cookie name filter for "delete".

    Returns:
        For "get": list of cookie dicts.
        For "set"/"delete": dict with status and count.
    """
    try:
        page = await browser_manager.get_active_page()
        ctx = page.context

        if action == "get":
            all_cookies = await ctx.cookies()
            if domain:
                all_cookies = [c for c in all_cookies if domain in c.get("domain", "")]
            return all_cookies

        elif action == "set":
            if not cookies_list:
                return error_response("cookies_list is required for action='set'")
            await ctx.add_cookies(cookies_list)  # type: ignore[arg-type]
            return {"status": "set", "count": len(cookies_list)}

        elif action == "delete":
            all_cookies = await ctx.cookies()
            deleted = 0
            for c in all_cookies:
                should_delete = (
                    (not name or c["name"] == name)  # type: ignore[typeddict-item]
                    and (not domain or domain in c.get("domain", ""))
                )
                if should_delete:
                    deleted += 1

            filters = {}
            if name:
                filters["name"] = name
            if domain:
                filters["domain"] = re.compile(re.escape(domain))
            await ctx.clear_cookies(**filters)
            return {"status": "deleted", "count": deleted}

        else:
            return error_response(f"unknown action: {action}. Use get/set/delete")
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def get_storage(storage_type: Literal["local", "session"] = "local") -> dict:
    """Get the contents of localStorage or sessionStorage.

    Args:
        storage_type: "local" for localStorage, "session" for sessionStorage.

    Returns:
        dict with all key-value pairs in the storage.
    """
    try:
        page = await browser_manager.get_active_page()
        if storage_type == "local":
            data = await page.evaluate("""() => {
                const obj = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    obj[key] = localStorage.getItem(key);
                }
                return obj;
            }""")
        elif storage_type == "session":
            data = await page.evaluate("""() => {
                const obj = {};
                for (let i = 0; i < sessionStorage.length; i++) {
                    const key = sessionStorage.key(i);
                    obj[key] = sessionStorage.getItem(key);
                }
                return obj;
            }""")
        else:
            return error_response(f"Invalid storage_type: {storage_type}. Use 'local' or 'session'.")
        return {"storage_type": storage_type, "data": data, "count": len(data)}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def export_state(save_path: str) -> dict:
    """Export the complete browser state (cookies + storage) to a JSON file.

    Args:
        save_path: Local file path to save the state JSON.

    Returns:
        dict with status and the save path.
    """
    try:
        if not save_path or not save_path.strip():
            return error_response("save_path is required")
        target = resolve_workspace_path(save_path, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = resolve_workspace_path(str(target), for_write=True)
        page = await browser_manager.get_active_page()
        ctx = page.context
        state = await ctx.storage_state()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
            ) as output:
                temp_path = output.name
                json.dump(state, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return {"status": "exported", "path": str(target)}
    except Exception as e:
        return error_response(e)


@mcp.tool()
async def import_state(state_path: str) -> dict:
    """Import browser state from a JSON file by creating a new context.

    Args:
        state_path: Path to the state JSON file (exported by export_state).

    Returns:
        dict with status and the new context name.
    """
    try:
        if not state_path or not state_path.strip():
            return error_response("state_path is required")
        await browser_manager._ensure_browser()
        if browser_manager.browser is None:
            return error_response("No browser available after launch")
        source = resolve_workspace_path(state_path, for_write=False)
        if not source.is_file():
            return error_response(f"state file not found: {state_path}")
        ctx_name = f"imported_{len(browser_manager.contexts)}"
        await browser_manager.create_context(ctx_name, storage_state=str(source))
        return {"status": "imported", "context": ctx_name, "path": str(source)}
    except Exception as e:
        return error_response(e)
