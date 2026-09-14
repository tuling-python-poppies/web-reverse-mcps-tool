# 模块说明: 读取和渲染 hooks/ 下的 JS 模板文件。
from __future__ import annotations

import os


def _read_hook_template(filename: str) -> str:
    """Read a JS hook template file from the hooks/ directory."""
    hooks_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks")
    filepath = os.path.join(hooks_dir, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def get_font_fallback_script() -> str:
    """Load the CJK font fallback script for cross-OS fingerprinting."""
    return _read_hook_template("font_fallback.js")


def _render_template(template_name: str, **kwargs) -> str:
    """Render a JS template with placeholder substitution."""
    template = _read_hook_template(template_name)
    js = template
    for key, value in kwargs.items():
        placeholder = "{{" + key + "}}"
        if isinstance(value, bool):
            js = js.replace(placeholder, "true" if value else "false")
        else:
            js = js.replace(placeholder, str(value))
    return js


def render_trace_template(
    function_path: str,
    max_captures: int = 50,
    log_args: bool = True,
    log_return: bool = True,
    log_stack: bool = False,
) -> str:
    """Render the trace_template.js with the given parameters."""
    return _render_template(
        "trace_template.js",
        FUNCTION_PATH=function_path,
        MAX_CAPTURES=max_captures,
        LOG_ARGS=log_args,
        LOG_RETURN=log_return,
        LOG_STACK=log_stack,
    )


def render_persistent_trace_template(
    function_path: str,
    max_captures: int = 50,
    log_args: bool = True,
    log_return: bool = True,
    log_stack: bool = False,
) -> str:
    """Render the persistent trace template that emits data via console.log."""
    return _render_template(
        "trace_persistent_template.js",
        FUNCTION_PATH=function_path,
        MAX_CAPTURES=max_captures,
        LOG_ARGS=log_args,
        LOG_RETURN=log_return,
        LOG_STACK=log_stack,
    )
