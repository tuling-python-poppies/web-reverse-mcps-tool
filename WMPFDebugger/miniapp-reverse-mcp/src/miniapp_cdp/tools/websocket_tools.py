from __future__ import annotations

from typing import Any
from ..network_capture import NetworkCaptureService

async def get_websocket_messages(
    network_capture: NetworkCaptureService,
    wsid: str | None = None,
    direction: str | None = None,
    show_content: bool = False,
    page_size: int = 10,
    page_idx: int = 0,
) -> str:
    """
    Get WebSocket connections or messages for a specific connection.
    If wsid is None, returns a list of active WebSocket connections.
    If wsid is provided, returns recent messages for that connection.
    """
    await network_capture.ensure_monitoring()
    collector = network_capture.collector
    
    if collector is None:
        return "Debugger not connected."
        
    websockets = getattr(collector, "websockets", {})
    
    # Mode 1: List connections
    if not wsid:
        if not websockets:
            return "No WebSocket connections found."
            
        md = ["## WebSocket Connections\n"]
        for wid, ws in websockets.items():
            status = "Closed" if ws.closed else "Active"
            md.append(f"- **ID**: `{wid}`")
            md.append(f"  - **URL**: `{ws.url}`")
            md.append(f"  - **Status**: {status}")
            if ws.handshake_status is not None:
                md.append(f"  - **Handshake**: {ws.handshake_status} {ws.handshake_status_text or ''}".rstrip())
            md.append(f"  - **Frames**: {len(ws.frames)}")
            if ws.frame_errors:
                md.append(f"  - **Frame Errors**: {len(ws.frame_errors)}")
            md.append("")
        md.append("Tip: Use `get_websocket_messages(wsid='...')` to view messages for a connection.")
        return "\n".join(md)
        
    # Mode 2: Show messages for a specific connection
    ws = websockets.get(wsid)
    if not ws:
        return f"WebSocket connection with ID '{wsid}' not found."
        
    frames = ws.frames
    md_header = [f"## Recent Messages for WebSocket `{wsid}`"]
    if ws.handshake_request_headers:
        md_header.append("\n### Handshake Request Headers")
        for k, v in ws.handshake_request_headers.items():
            md_header.append(f"- {k}: {v}")
    if ws.handshake_response_headers:
        md_header.append("\n### Handshake Response Headers")
        md_header.append(f"Status: {ws.handshake_status} {ws.handshake_status_text or ''}".rstrip())
        for k, v in ws.handshake_response_headers.items():
            md_header.append(f"- {k}: {v}")
    if ws.frame_errors:
        md_header.append("\n### Frame Errors")
        for err in list(ws.frame_errors)[-10:]:
            md_header.append(f"- {err}")
    if direction:
        frames = [f for f in frames if f.direction == direction]
        
    if not frames:
        return f"No messages found for WebSocket '{wsid}' (direction={direction or 'all'})."
        
    # Pagination
    total = len(frames)
    start_idx = total - 1 - (page_idx * page_size)
    end_idx = max(-1, start_idx - page_size)
    
    if start_idx < 0:
        return f"Page {page_idx} is out of bounds. Total messages: {total}."
        
    display_frames = []
    # Iterate backwards (newest first)
    for i in range(start_idx, end_idx, -1):
        display_frames.append((i, frames[i]))
        
    md = md_header
    md.append(f"Showing page {page_idx} ({len(display_frames)} messages, {total} total)\n")
    
    for idx, frame in display_frames:
        dir_icon = "⬆️ SENT" if frame.direction == "sent" else "⬇️ RECV"
        md.append(f"### Frame {idx} | {dir_icon} | Opcode: {frame.opcode}")
        
        payload = frame.payloadData
        if not payload:
            md.append("*(Empty payload)*\n")
            continue
            
        if show_content:
            # Simple heuristic for JSON or Text
            if payload.startswith("{") or payload.startswith("["):
                md.append("```json")
                # Avoid pretty-printing very large JSON to save space, but add a newline
                if len(payload) > 1000:
                    md.append(payload[:1000] + "\n... (truncated)")
                else:
                    md.append(payload)
                md.append("```")
            else:
                md.append("```text")
                if len(payload) > 1000:
                    md.append(payload[:1000] + "\n... (truncated)")
                else:
                    md.append(payload)
                md.append("```")
        else:
            md.append(f"*(Payload size: {len(payload)} chars, use `show_content=True` to view)*\n")
            
        md.append("")
        
    return "\n".join(md)
