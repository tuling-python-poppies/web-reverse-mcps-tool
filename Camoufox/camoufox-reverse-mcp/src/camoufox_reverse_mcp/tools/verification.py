# 模块说明: 离线验证签名函数,对比样本输出与预期结果差异。
"""Stateless sample verification with browser and optional Node.js runtimes."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response


@mcp.tool()
async def verify_signer_offline(
    signer_code: str,
    samples: list[dict],
    compare_params: list[str] | None = None,
    runtime: str = "browser",
    timeout_ms: int = 10000,
) -> dict:
    """Verify a signer against explicit expected values without sending requests.

    Args:
        signer_code: JS expression evaluating to a function receiving sample.input
            and returning an object. Async functions are supported. Only run code
            you intend to execute locally; Node vm is not a security boundary.
        samples: Non-empty list (up to 1000) of {id?, input: object, expected: object}.
            Each expected object must contain at least one comparison key.
        compare_params: Optional non-empty list of expected keys. Missing keys
            are invalid input; missing computed keys fail even if expected is null.
        runtime: "browser" preserves the current-page default. "node" runs an
            independent process without launching a browser, supports require of
            crypto/node:crypto, and needs Node.js on PATH. No runtime fallback.
        timeout_ms: Node process deadline (1..120000); also the maximum wait for
            the browser evaluation. A browser timeout does not undo/stop effects.

    Returns:
        total_samples, passed, failed, pass_rate, first_divergence and details.
        Invalid input returns error before any signer code is executed.
    """
    try:
        _validate_samples(samples, compare_params)
        if runtime not in ("browser", "node"):
            raise ValueError("runtime must be 'browser' or 'node'")
        if not 1 <= timeout_ms <= 120000:
            raise ValueError("timeout_ms must be between 1 and 120000")
        if not isinstance(signer_code, str) or not signer_code.strip():
            raise ValueError("signer_code must be a non-empty function expression")
        inputs = [s.get("input", {}) for s in samples]
        if runtime == "node":
            outcomes = await _run_node(signer_code, inputs, timeout_ms)
        else:
            page = await browser_manager.get_active_page()
            # One lexical scope per invocation: parallel calls cannot overwrite
            # a shared window.__mcp_signer_fn or leak a stale signer into the page.
            expression = """async (inputs) => {
                const signer = (
SIGNER
);
                if (typeof signer !== 'function') throw new Error('signer_code must evaluate to a function');
                const outcomes = [];
                for (const input of inputs) {
                    try { outcomes.push({computed: await signer(input)}); }
                    catch (error) { outcomes.push({error: String(error)}); }
                }
                return outcomes;
            }""".replace("SIGNER", signer_code)
            outcomes = await asyncio.wait_for(page.evaluate(expression, inputs), timeout_ms / 1000)
        details = []
        first_divergence = None
        for index, (sample, outcome) in enumerate(zip(samples, outcomes)):
            detail = {"sample_id": sample.get("id", f"sample_{index}"), "passed": False}
            if outcome.get("error"):
                detail["error"] = outcome["error"]
            elif not isinstance(outcome.get("computed"), dict):
                detail["error"] = "signer must return an object of computed parameters"
            else:
                diffs = _compare_params(sample["expected"], outcome["computed"], compare_params)
                detail["passed"] = not diffs
                if diffs:
                    detail["diffs"] = diffs
            if not detail["passed"] and first_divergence is None:
                first_divergence = {**detail, "input": sample.get("input", {})}
            details.append(detail)
        if len(details) != len(samples):
            raise ValueError("runtime returned an incomplete sample result")
        passed = sum(d["passed"] for d in details)
        return {"total_samples": len(samples), "passed": passed,
                "failed": len(samples) - passed, "pass_rate": round(passed / len(samples), 3),
                "first_divergence": first_divergence, "details": details, "runtime": runtime}
    except asyncio.TimeoutError:
        return {"error": "signer execution timed out; no automatic replay was attempted", "runtime": runtime}
    except Exception as exc:
        return {"error": str(exc)}


def _validate_samples(samples, focus) -> None:
    if not isinstance(samples, list) or not 1 <= len(samples) <= 1000:
        raise ValueError("samples must be a non-empty list with at most 1000 samples")
    if focus is not None and (not isinstance(focus, list) or not focus or
                              any(not isinstance(k, str) or not k for k in focus)):
        raise ValueError("compare_params must be a non-empty list of non-empty keys")
    for i, sample in enumerate(samples):
        if not isinstance(sample, dict) or not isinstance(sample.get("input", {}), dict):
            raise ValueError(f"sample {i}: sample and input must be objects")
        expected = sample.get("expected")
        if not isinstance(expected, dict) or not expected or any(not isinstance(k, str) or not k for k in expected):
            raise ValueError(f"sample {i}: expected must be a non-empty object with non-empty keys")
        if focus is not None and any(k not in expected for k in focus):
            raise ValueError(f"sample {i}: compare_params contains a key missing from expected")


def _json_equal(expected, actual):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(_json_equal(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(_json_equal(a, b) for a, b in zip(expected, actual))
    return expected == actual


def _compare_params(expected: dict, computed: Any, focus: list[str] | None) -> list[dict]:
    diffs = []
    for key in focus if focus is not None else expected:
        exp = expected[key]
        act = computed.get(key)
        # bool and number are distinct in JavaScript, even though True == 1 in Python.
        if key in computed and _json_equal(exp, act):
            continue
        diff = {"param": key, "expected": exp, "actual": act}
        if key not in computed:
            diff["actual_missing"] = True
        if isinstance(exp, str) and isinstance(act, str):
            first = next((i for i, (a, b) in enumerate(zip(exp, act)) if a != b), min(len(exp), len(act)))
            diff.update(first_diff_char=first, expected_length=len(exp), actual_length=len(act))
        diffs.append(diff)
    return diffs


async def _run_node(code: str, inputs: list, timeout_ms: int) -> list:
    node = shutil.which("node")
    if node is None:
        raise ValueError("runtime='node' requires Node.js on PATH; browser was not launched")
    payload = json.dumps({"code": code, "inputs": inputs, "timeout_ms": timeout_ms}).encode()
    if len(payload) > 2_000_000:
        raise ValueError("Node verification input exceeds 2 MB")
    runner = Path(__file__).parents[1] / "hooks" / "offline_signer_runner.js"
    proc = await asyncio.create_subprocess_exec(node, str(runner), stdin=asyncio.subprocess.PIPE,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async def exchange():
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
            async def bounded_read(stream):
                chunks = []
                size = 0
                while chunk := await stream.read(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ValueError("Node verification output exceeds 2 MB")
                    chunks.append(chunk)
                return b"".join(chunks)
            stdout, stderr = await asyncio.gather(bounded_read(proc.stdout), bounded_read(proc.stderr))
            await proc.wait()
            return stdout, stderr
        stdout, stderr = await asyncio.wait_for(exchange(), timeout_ms / 1000)
        if proc.returncode:
            raise ValueError("Node signer failed: " + stderr.decode(errors="replace")[:2000])
        result = json.loads(stdout)
        if "error" in result:
            raise ValueError(result["error"])
        return result["outcomes"]
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
