from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from cdp_use.client import CDPClient

from ..endpoint import resolve_endpoint
from ..models import TargetState
from ..cdp_runtime import CDPRuntime
from ..event_bus import EventBus
from ..state_store import StateStore

DEFAULT_ENDPOINT = "devtools://devtools/bundled/inspector.html?ws=127.0.0.1:62000"
DEFAULT_RESOURCE_TYPES = {"XHR", "Fetch"}
ALL_RESOURCE_TYPES = {
    "Document",
    "Stylesheet",
    "Image",
    "Media",
    "Font",
    "Script",
    "TextTrack",
    "XHR",
    "Fetch",
    "Prefetch",
    "EventSource",
    "WebSocket",
    "Manifest",
    "SignedExchange",
    "Ping",
    "CSPViolationReport",
    "Preflight",
    "Other",
}


@dataclass(slots=True)
class RequestRecord:
    request_id: str
    session_id: str
    url: str | None = None
    method: str | None = None
    resource_type: str | None = None
    status: int | None = None
    mime_type: str | None = None
    encoded_data_length: float | None = None
    initiator: dict[str, Any] | None = None
    request_headers: dict[str, Any] | None = None
    response_headers: dict[str, Any] | None = None
    request_extra_info: dict[str, Any] | None = None
    response_extra_info: dict[str, Any] | None = None
    post_data: str | None = None
    loading_finished: bool = False
    loading_failed: bool = False
    failure: dict[str, Any] | None = None


@dataclass(slots=True)
class ScriptRecord:
    script_id: str
    url: str
    start_line: int = 0
    start_column: int = 0
    end_line: int = 0
    end_column: int = 0
    execution_context_id: int = 0
    hash: str = ""
    source_map_url: str | None = None
    length: int = 0


@dataclass(slots=True)
class PausedState:
    is_paused: bool = False
    reason: str | None = None
    call_frames: list[dict[str, Any]] = field(default_factory=list)
    hit_breakpoints: list[str] = field(default_factory=list)
    data: Any = None


@dataclass(slots=True)
class BreakpointInfo:
    breakpoint_id: str
    url: str
    line_number: int
    column_number: int = 0
    condition: str | None = None
    is_regex: bool = False
    locations: list[dict[str, Any]] = field(default_factory=list)


class SingleTargetNetworkCollector:
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        target_id: str | None = None,
        resource_types: set[str] | None = None,
    ) -> None:
        self.raw_endpoint = endpoint
        self.ws_endpoint = resolve_endpoint(endpoint)
        self.target_id = target_id
        self.resource_types = resource_types or set(DEFAULT_RESOURCE_TYPES)
        self.state_store = StateStore()
        self.runtime = CDPRuntime(self.state_store, EventBus())
        self.client: CDPClient | None = None
        self.selected_target: TargetState | None = None
        self.session_id: str | None = None
        self.records: dict[str, RequestRecord] = {}
        self.request_order: deque[str] = deque(maxlen=2000)
        self.scripts: dict[str, ScriptRecord] = {}
        self.paused_state = PausedState()
        self._paused_event: asyncio.Event = asyncio.Event()
        self.xhr_breakpoints: set[str] = set()
        self.event_listener_breakpoints: set[tuple[str, str]] = set()
        self.breakpoints: dict[str, BreakpointInfo] = {}
        self.runtime_console: deque[dict[str, Any]] = deque(maxlen=1000)
        self.runtime_exceptions: deque[dict[str, Any]] = deque(maxlen=500)
        self.execution_contexts: dict[int, dict[str, Any]] = {}
        self.profiler_active = False
        self.precise_coverage_active = False

        from .websocket_models import WebSocketConnection, WebSocketFrame
        self.websockets: dict[str, WebSocketConnection] = {}

    async def start(self) -> dict[str, Any]:
        self.client = CDPClient(self.ws_endpoint)
        await self.client.start()
        targets = await self.client.send.Target.getTargets()
        target_infos = targets.get("targetInfos", [])
        for target_info in target_infos:
            self.state_store.upsert_target(
                TargetState(
                    target_id=target_info["targetId"],
                    target_type=target_info["type"],
                    title=target_info.get("title", ""),
                    url=target_info.get("url", ""),
                )
            )
        targets_model = self.state_store.list_targets()
        if self.target_id:
            target = next((t for t in targets_model if t.target_id == self.target_id), None)
            if target is None:
                raise RuntimeError(f"Target not found: {self.target_id}")
        else:
            target = self.runtime.select_default_app_target(targets_model)
        self.selected_target = target

        attached = await self.client.send.Target.attachToTarget(
            params={"targetId": target.target_id, "flatten": True}
        )
        session_id = attached.get("sessionId")
        if not session_id:
            raise RuntimeError("attachToTarget returned no sessionId")
        self.session_id = session_id

        self.client.register.Network.requestWillBeSent(self.on_request)
        self.client.register.Network.requestWillBeSentExtraInfo(self.on_request_extra_info)
        self.client.register.Network.responseReceived(self.on_response)
        self.client.register.Network.responseReceivedExtraInfo(self.on_response_extra_info)
        self.client.register.Network.loadingFinished(self.on_finished)
        self.client.register.Network.loadingFailed(self.on_loading_failed)
        self.client.register.Debugger.scriptParsed(self.on_script_parsed)
        self.client.register.Debugger.paused(self.on_paused)
        self.client.register.Debugger.resumed(self.on_resumed)
        self.client.register.Runtime.consoleAPICalled(self.on_console_api_called)
        self.client.register.Runtime.exceptionThrown(self.on_exception_thrown)
        self.client.register.Runtime.executionContextCreated(self.on_execution_context_created)
        self.client.register.Runtime.executionContextDestroyed(self.on_execution_context_destroyed)
        self.client.register.Runtime.executionContextsCleared(self.on_execution_contexts_cleared)
        
        self.client.register.Network.webSocketCreated(self.on_ws_created)
        self.client.register.Network.webSocketWillSendHandshakeRequest(self.on_ws_handshake_request)
        self.client.register.Network.webSocketHandshakeResponseReceived(self.on_ws_handshake_response)
        self.client.register.Network.webSocketFrameSent(self.on_ws_sent)
        self.client.register.Network.webSocketFrameReceived(self.on_ws_received)
        self.client.register.Network.webSocketFrameError(self.on_ws_error)
        self.client.register.Network.webSocketClosed(self.on_ws_closed)
        
        await self.client.send.Network.enable(session_id=session_id)
        await self.client.send.Runtime.enable(session_id=session_id)
        await self.client.send.Debugger.enable(session_id=session_id)
        
        # Async call stack depth for better stack traces
        try:
            await self.client.send.Debugger.setAsyncCallStackDepth(
                params={"maxDepth": 32}, session_id=session_id
            )
        except Exception:
            pass

        return {
            "resolvedEndpoint": self.ws_endpoint,
            "targetId": target.target_id,
            "title": target.title,
            "url": target.url,
            "sessionId": session_id,
        }

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.stop()
            self.client = None

    def on_ws_created(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event["requestId"]
        from .websocket_models import WebSocketConnection
        self.websockets[request_id] = WebSocketConnection(
            wsid=request_id,
            url=event.get("url", ""),
            initiator=event.get("initiator")
        )

    def on_ws_handshake_request(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event.get("requestId")
        if not request_id:
            return
        ws = self.websockets.get(request_id)
        if not ws:
            from .websocket_models import WebSocketConnection
            ws = WebSocketConnection(wsid=request_id, url="")
            self.websockets[request_id] = ws
        req = event.get("request") or {}
        ws.handshake_request_headers = req.get("headers")

    def on_ws_handshake_response(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event.get("requestId")
        if not request_id:
            return
        ws = self.websockets.get(request_id)
        if not ws:
            from .websocket_models import WebSocketConnection
            ws = WebSocketConnection(wsid=request_id, url="")
            self.websockets[request_id] = ws
        resp = event.get("response") or {}
        ws.handshake_response_headers = resp.get("headers")
        ws.handshake_status = resp.get("status")
        ws.handshake_status_text = resp.get("statusText")

    def on_ws_sent(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event["requestId"]
        ws = self.websockets.get(request_id)
        if ws:
            from .websocket_models import WebSocketFrame
            frame_data = event.get("response", {})
            import time
            ws.frames.append(WebSocketFrame(
                direction="sent",
                time=event.get("timestamp", time.time()),
                opcode=frame_data.get("opcode", 0),
                mask=frame_data.get("mask", False),
                payloadData=frame_data.get("payloadData", "")
            ))

    def on_ws_received(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event["requestId"]
        ws = self.websockets.get(request_id)
        if ws:
            from .websocket_models import WebSocketFrame
            frame_data = event.get("response", {})
            import time
            ws.frames.append(WebSocketFrame(
                direction="received",
                time=event.get("timestamp", time.time()),
                opcode=frame_data.get("opcode", 0),
                mask=frame_data.get("mask", False),
                payloadData=frame_data.get("payloadData", "")
            ))

    def on_ws_closed(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event["requestId"]
        ws = self.websockets.get(request_id)
        if ws:
            ws.closed = True

    def on_ws_error(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id: return
        request_id = event.get("requestId")
        ws = self.websockets.get(request_id) if request_id else None
        if ws:
            ws.frame_errors.append(event.get("errorMessage") or str(event))

    def on_request(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        resource_type = event.get("type")
        request_id = event["requestId"]
        request = event.get("request", {})
        record = self.records.get(request_id)

        # For new requests, filter by resource_type.
        # For existing requests (redirects with same requestId), always update —
        # the original request was already accepted so we keep tracking it.
        if record is None:
            if resource_type not in self.resource_types:
                return
            record = RequestRecord(request_id=request_id, session_id=self.session_id)
            # If deque is full, append will auto-evict the oldest entry from the left.
            # We must clean up the corresponding record dict entry for the evicted id.
            if len(self.request_order) == self.request_order.maxlen:
                evicted_id = self.request_order[0]  # this will be auto-popped by append
                self.records.pop(evicted_id, None)
            self.request_order.append(request_id)
            self.records[request_id] = record

        record.url = request.get("url")
        record.method = request.get("method")
        record.resource_type = resource_type
        record.initiator = event.get("initiator")
        record.request_headers = request.get("headers")
        record.post_data = request.get("postData")

    def on_request_extra_info(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        request_id = event.get("requestId")
        if not request_id:
            return
        record = self.records.get(request_id)
        if record:
            record.request_extra_info = event

    def on_response(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        request_id = event["requestId"]
        record = self.records.get(request_id)
        if record is None:
            return
        response = event.get("response", {})
        record.status = response.get("status")
        record.mime_type = response.get("mimeType")
        record.response_headers = response.get("headers")

    def on_response_extra_info(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        request_id = event.get("requestId")
        if not request_id:
            return
        record = self.records.get(request_id)
        if record:
            record.response_extra_info = event

    def on_finished(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        request_id = event["requestId"]
        record = self.records.get(request_id)
        if record is None:
            return
        record.encoded_data_length = event.get("encodedDataLength")
        record.loading_finished = True

    def on_loading_failed(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        request_id = event.get("requestId")
        if not request_id:
            return
        record = self.records.get(request_id)
        if record is None:
            record = RequestRecord(request_id=request_id, session_id=self.session_id)
            if len(self.request_order) == self.request_order.maxlen:
                self.records.pop(self.request_order[0], None)
            self.request_order.append(request_id)
            self.records[request_id] = record
        record.loading_failed = True
        record.failure = {
            "errorText": event.get("errorText"),
            "canceled": event.get("canceled"),
            "blockedReason": event.get("blockedReason"),
            "corsErrorStatus": event.get("corsErrorStatus"),
            "timestamp": event.get("timestamp"),
        }

    def on_console_api_called(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        self.runtime_console.append(event)

    def on_exception_thrown(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        self.runtime_exceptions.append(event)

    def on_execution_context_created(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        ctx = event.get("context") or {}
        ctx_id = ctx.get("id")
        if ctx_id is not None:
            self.execution_contexts[int(ctx_id)] = ctx

    def on_execution_context_destroyed(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        ctx_id = event.get("executionContextId")
        if ctx_id is not None:
            self.execution_contexts.pop(int(ctx_id), None)

    def on_execution_contexts_cleared(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        self.execution_contexts.clear()

    def on_script_parsed(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        script_id = event["scriptId"]
        self.scripts[script_id] = ScriptRecord(
            script_id=script_id,
            url=event.get("url", ""),
            start_line=event.get("startLine", 0),
            start_column=event.get("startColumn", 0),
            end_line=event.get("endLine", 0),
            end_column=event.get("endColumn", 0),
            execution_context_id=event.get("executionContextId", 0),
            hash=event.get("hash", ""),
            source_map_url=event.get("sourceMapURL"),
            length=event.get("length", 0),
        )

    def on_paused(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        
        call_frames = []
        for frame in event.get("callFrames", []):
            call_frames.append({
                "callFrameId": frame.get("callFrameId"),
                "functionName": frame.get("functionName") or "<anonymous>",
                "location": frame.get("location", {}),
                "url": frame.get("url", ""),
                "scopeChain": frame.get("scopeChain", []),
                "this": frame.get("this", {})
            })
            
        self.paused_state = PausedState(
            is_paused=True,
            reason=event.get("reason"),
            call_frames=call_frames,
            hit_breakpoints=event.get("hitBreakpoints", []),
            data=event.get("data")
        )
        self._paused_event.set()

    def on_resumed(self, event: dict[str, Any], session_id: str | None) -> None:
        if session_id != self.session_id:
            return
        self.paused_state = PausedState()
        self._paused_event.clear()

    def list_scripts(self, url_filter: str | None = None) -> list[ScriptRecord]:
        results = []
        for script in self.scripts.values():
            if url_filter and url_filter.lower() not in script.url.lower():
                continue
            results.append(script)
        return results

    def clear(self) -> None:
        self.records.clear()
        self.request_order.clear()
        self.scripts.clear()

    def list_requests(
        self,
        reqid: str | None = None,
        page_size: int = 20,
        page_idx: int = 0,
        resource_types: list[str] | None = None,
        url_filter: str | None = None,
    ) -> dict[str, Any]:
        allowed_types = set(resource_types) if resource_types else self.resource_types
        requests = []
        for request_id in reversed(self.request_order):
            request = self.records.get(request_id)
            if not request:
                continue
            if request.resource_type and request.resource_type not in allowed_types:
                continue
            if url_filter and (not request.url or url_filter not in request.url):
                continue
            requests.append(request)

        if reqid is not None:
            request = next((item for item in requests if item.request_id == reqid), None)
            if request is None:
                raise ValueError(f"Request not found: {reqid}")
            return {"mode": "detail", "request": asdict(request)}

        start = page_idx * page_size
        end = start + page_size
        paged = requests[start:end]
        return {
            "mode": "list",
            "totalCount": len(requests),
            "pageSize": page_size,
            "pageIdx": page_idx,
            "requests": [asdict(request) for request in paged],
        }

    async def get_response_body(self, request_id: str) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Collector not started")
        record = self.records.get(request_id)
        if record is not None and not record.loading_finished:
            raise RuntimeError(
                f"Request {request_id} has not finished loading yet. "
                "Wait for the response to complete or re-trigger the action."
            )
        try:
            result = await self.client.send.Network.getResponseBody(
                params={"requestId": request_id},
                session_id=self.session_id,
            )
        except RuntimeError as e:
            error_msg = str(e)
            if "No resource with given identifier" in error_msg or "No data found" in error_msg:
                raise RuntimeError(
                    f"Response body for request {request_id} is no longer available. "
                    "The browser may have garbage-collected it. Re-trigger the request to capture it fresh."
                ) from e
            raise
        return {
            "requestId": request_id,
            "sessionId": self.session_id,
            "body": result.get("body", ""),
            "base64Encoded": result.get("base64Encoded", False),
            "mimeType": record.mime_type if record else None,
        }

    async def get_request_post_data(self, request_id: str) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Collector not started")
        result = await self.client.send.Network.getRequestPostData(
            params={"requestId": request_id},
            session_id=self.session_id,
        )
        return {"requestId": request_id, "postData": result.get("postData", "")}

    async def get_script_source(self, script_id: str) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        result = await self.client.send.Debugger.getScriptSource(
            params={"scriptId": script_id},
            session_id=self.session_id,
        )
        return result

    async def search_in_scripts(
        self, query: str, case_sensitive: bool = False, is_regex: bool = False
    ) -> list[dict[str, Any]]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
            
        all_matches = []
        for script_id, script_record in list(self.scripts.items()):
            try:
                result = await self.client.send.Debugger.searchInContent(
                    params={
                        "scriptId": script_id,
                        "query": query,
                        "caseSensitive": case_sensitive,
                        "isRegex": is_regex,
                    },
                    session_id=self.session_id,
                )
                matches = result.get("result", [])
                for match in matches:
                    all_matches.append({
                        "scriptId": script_id,
                        "url": script_record.url,
                        "lineNumber": match.get("lineNumber", 0),
                        "lineContent": match.get("lineContent", ""),
                    })
            except Exception:
                # Some scripts might fail to be searched (e.g., GC'd)
                continue
        return all_matches

    async def evaluate_script(
        self,
        expression: str,
        return_by_value: bool = True,
        await_promise: bool = True,
        context_id: int | None = None,
        frame_index: int = 0,
    ) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Runtime not connected")
            
        # If paused, evaluate on the call frame
        if self.paused_state.is_paused and self.paused_state.call_frames:
            if 0 <= frame_index < len(self.paused_state.call_frames):
                call_frame_id = self.paused_state.call_frames[frame_index].get("callFrameId")
                if call_frame_id:
                    params = {
                        "callFrameId": call_frame_id,
                        "expression": expression,
                        "returnByValue": return_by_value,
                        "includeCommandLineAPI": True,
                    }
                    return await self.client.send.Debugger.evaluateOnCallFrame(
                        params=params,
                        session_id=self.session_id,
                    )

        # Otherwise evaluate globally
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": await_promise,
            "includeCommandLineAPI": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
            
        return await self.client.send.Runtime.evaluate(
            params=params,
            session_id=self.session_id,
        )

    async def set_xhr_breakpoint(self, url: str) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        await self.client.send.DOMDebugger.setXHRBreakpoint(
            params={"url": url}, session_id=self.session_id
        )
        self.xhr_breakpoints.add(url)

    async def remove_xhr_breakpoint(self, url: str) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        await self.client.send.DOMDebugger.removeXHRBreakpoint(
            params={"url": url}, session_id=self.session_id
        )
        self.xhr_breakpoints.discard(url)

    async def set_event_listener_breakpoint(self, event_name: str, target_name: str | None = None) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        params: dict[str, Any] = {"eventName": event_name}
        if target_name:
            params["targetName"] = target_name
        await self.client.send.DOMDebugger.setEventListenerBreakpoint(
            params=params, session_id=self.session_id
        )
        self.event_listener_breakpoints.add((event_name, target_name or "*"))

    async def remove_event_listener_breakpoint(self, event_name: str, target_name: str | None = None) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        params: dict[str, Any] = {"eventName": event_name}
        if target_name:
            params["targetName"] = target_name
        await self.client.send.DOMDebugger.removeEventListenerBreakpoint(
            params=params, session_id=self.session_id
        )
        self.event_listener_breakpoints.discard((event_name, target_name or "*"))

    async def start_cpu_profile(self, sampling_interval: int | None = None) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Profiler not connected")
        await self.client.send.Profiler.enable(session_id=self.session_id)
        if sampling_interval:
            await self.client.send.Profiler.setSamplingInterval(
                params={"interval": sampling_interval}, session_id=self.session_id
            )
        await self.client.send.Profiler.start(session_id=self.session_id)
        self.profiler_active = True

    async def stop_cpu_profile(self) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Profiler not connected")
        result = await self.client.send.Profiler.stop(session_id=self.session_id)
        self.profiler_active = False
        return result

    async def start_precise_coverage(self, call_count: bool = True, detailed: bool = True) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Profiler not connected")
        await self.client.send.Profiler.enable(session_id=self.session_id)
        await self.client.send.Profiler.startPreciseCoverage(
            params={"callCount": call_count, "detailed": detailed}, session_id=self.session_id
        )
        self.precise_coverage_active = True

    async def take_precise_coverage(self, reset: bool = False) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Profiler not connected")
        result = await self.client.send.Profiler.takePreciseCoverage(session_id=self.session_id)
        if reset:
            await self.client.send.Profiler.startPreciseCoverage(
                params={"callCount": True, "detailed": True}, session_id=self.session_id
            )
        return result

    async def stop_precise_coverage(self) -> dict[str, Any]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Profiler not connected")
        result = await self.client.send.Profiler.stopPreciseCoverage(session_id=self.session_id)
        self.precise_coverage_active = False
        return result

    async def resume(self) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        if not self.paused_state.is_paused:
            raise RuntimeError("Execution is not paused")
        await self.client.send.Debugger.resume(session_id=self.session_id)

    async def pause(self) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        await self.client.send.Debugger.pause(session_id=self.session_id)
        
    async def get_scope_variables(self, object_id: str) -> list[dict[str, Any]]:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        result = await self.client.send.Runtime.getProperties(
            params={"objectId": object_id, "ownProperties": True, "generatePreview": True},
            session_id=self.session_id
        )
        variables = []
        for prop in result.get("result", []):
            name = prop.get("name", "")
            if name.startswith("__") or name == "this":
                continue
            val = prop.get("value")
            if not val:
                continue
            variables.append({
                "name": name,
                "type": val.get("type"),
                "value": val.get("value") if "value" in val else (val.get("description") or f"[{val.get('type')}]"),
                "description": val.get("description")
            })
        return variables

    async def set_breakpoint(self, url: str, line_number: int, column_number: int = 0, condition: str | None = None) -> BreakpointInfo:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
            
        import re
        escaped_url = re.escape(url)
        params: dict[str, Any] = {
            "urlRegex": escaped_url,
            "lineNumber": line_number,
            "columnNumber": column_number
        }
        if condition:
            params["condition"] = condition
            
        result = await self.client.send.Debugger.setBreakpointByUrl(
            params=params, session_id=self.session_id
        )
        
        bp = BreakpointInfo(
            breakpoint_id=result.get("breakpointId", ""),
            url=url,
            line_number=line_number,
            column_number=column_number,
            condition=condition,
            locations=result.get("locations", [])
        )
        self.breakpoints[bp.breakpoint_id] = bp
        return bp

    async def remove_breakpoint(self, breakpoint_id: str) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        
        await self.client.send.Debugger.removeBreakpoint(
            params={"breakpointId": breakpoint_id},
            session_id=self.session_id
        )
        self.breakpoints.pop(breakpoint_id, None)

    async def clear_all_breakpoints(self) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        
        # Clear code breakpoints
        for bp_id in list(self.breakpoints.keys()):
            try:
                await self.remove_breakpoint(bp_id)
            except Exception:
                pass
                
        # Clear XHR breakpoints
        for url in list(self.xhr_breakpoints):
            try:
                await self.remove_xhr_breakpoint(url)
            except Exception:
                pass
        # Clear event listener breakpoints
        for event_name, target_name in list(self.event_listener_breakpoints):
            try:
                await self.remove_event_listener_breakpoint(
                    event_name, None if target_name == "*" else target_name
                )
            except Exception:
                pass

    async def step_over(self) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        if not self.paused_state.is_paused:
            raise RuntimeError("Execution is not paused")
        self._paused_event.clear()
        await self.client.send.Debugger.stepOver(session_id=self.session_id)

    async def step_into(self) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        if not self.paused_state.is_paused:
            raise RuntimeError("Execution is not paused")
        self._paused_event.clear()
        await self.client.send.Debugger.stepInto(session_id=self.session_id)

    async def step_out(self) -> None:
        if self.client is None or self.session_id is None:
            raise RuntimeError("Debugger not connected")
        if not self.paused_state.is_paused:
            raise RuntimeError("Execution is not paused")
        self._paused_event.clear()
        await self.client.send.Debugger.stepOut(session_id=self.session_id)

    async def wait_for_pause(self, timeout: float = 5.0) -> bool:
        """Wait for the debugger to pause again after a step/resume. Returns True if paused."""
        try:
            await asyncio.wait_for(self._paused_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
