from __future__ import annotations

import json

from ..network_capture import NetworkCaptureService


async def get_runtime_events(
    network_capture: NetworkCaptureService,
    event_type: str = "console",
    clear: bool = False,
    limit: int = 50,
) -> str:
    await network_capture.ensure_monitoring()
    collector = network_capture.collector
    if not collector:
        return "Runtime not connected."

    if event_type == "console":
        data = list(collector.runtime_console)
        if clear:
            collector.runtime_console.clear()
    elif event_type == "exception":
        data = list(collector.runtime_exceptions)
        if clear:
            collector.runtime_exceptions.clear()
    elif event_type == "context":
        data = list(collector.execution_contexts.values())
    else:
        return "Invalid event_type. Use console, exception, or context."

    data = data[-limit:] if limit > 0 else data
    return json.dumps({"type": event_type, "count": len(data), "events": data}, ensure_ascii=False, indent=2)[:20000]


def _hot_functions(profile: dict, limit: int = 30) -> list[dict]:
    profile_data = profile.get("profile") or profile
    nodes = profile_data.get("nodes") or []
    samples = profile_data.get("samples") or []
    sample_count: dict[int, int] = {}
    for node_id in samples:
        try:
            key = int(node_id)
        except (TypeError, ValueError):
            continue
        sample_count[key] = sample_count.get(key, 0) + 1
    total = max(1, len(samples))
    rows = []
    for node in nodes:
        call_frame = node.get("callFrame") or {}
        node_id = node.get("id")
        self_samples = sample_count.get(int(node_id), 0) if node_id is not None else 0
        hit_count = int(node.get("hitCount") or 0)
        if self_samples <= 0 and hit_count <= 0:
            continue
        rows.append({
            "functionName": call_frame.get("functionName") or "(anonymous)",
            "url": call_frame.get("url") or "",
            "scriptId": call_frame.get("scriptId"),
            "lineNumber": call_frame.get("lineNumber"),
            "columnNumber": call_frame.get("columnNumber"),
            "hitCount": hit_count,
            "selfSamples": self_samples,
            "samplePercent": round(self_samples * 100 / total, 2),
            "nodeId": node_id,
        })
    rows.sort(key=lambda item: (item.get("selfSamples") or 0, item.get("hitCount") or 0), reverse=True)
    return rows[:limit]


async def start_cpu_profile(network_capture: NetworkCaptureService, sampling_interval: int | None = None) -> str:
    await network_capture.ensure_monitoring()
    collector = network_capture.collector
    if not collector:
        return "Profiler not connected."
    try:
        await collector.start_cpu_profile(sampling_interval=sampling_interval)
        return "CPU profiling started. Trigger the miniapp action, then call stop_cpu_profile()."
    except Exception as e:
        return f"Error starting CPU profile: {e}"


async def stop_cpu_profile(
    network_capture: NetworkCaptureService,
    limit: int = 30,
    include_profile: bool = False,
) -> str:
    await network_capture.ensure_monitoring()
    collector = network_capture.collector
    if not collector:
        return "Profiler not connected."
    try:
        profile = await collector.stop_cpu_profile()
        result = {
            "status": "stopped",
            "hot_functions": _hot_functions(profile, limit=limit),
        }
        if include_profile:
            result["profile"] = profile.get("profile") or profile
        return json.dumps(result, ensure_ascii=False, indent=2)[:30000]
    except Exception as e:
        return f"Error stopping CPU profile: {e}"


async def precise_coverage(
    network_capture: NetworkCaptureService,
    action: str = "start",
    reset: bool = False,
    limit: int = 100,
) -> str:
    await network_capture.ensure_monitoring()
    collector = network_capture.collector
    if not collector:
        return "Profiler not connected."
    try:
        if action == "start":
            await collector.start_precise_coverage()
            return "Precise coverage started. Trigger actions, then call precise_coverage(action='take' or 'stop')."
        if action == "take":
            data = await collector.take_precise_coverage(reset=reset)
        elif action == "stop":
            data = await collector.stop_precise_coverage()
        else:
            return "Invalid action. Use start, take, or stop."
        rows = []
        for item in data.get("result", [])[:limit]:
            rows.append({
                "scriptId": item.get("scriptId"),
                "url": item.get("url"),
                "functions": len(item.get("functions") or []),
            })
        return json.dumps({"action": action, "returned": len(rows), "scripts": rows}, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error handling precise coverage: {e}"
