from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_WS_FRAMES = 500

@dataclass
class WebSocketFrame:
    direction: str  # "sent" or "received"
    time: float
    opcode: int
    mask: bool
    payloadData: str

@dataclass
class WebSocketConnection:
    wsid: str
    url: str
    initiator: dict[str, Any] | None = None
    frames: deque[WebSocketFrame] = field(default_factory=lambda: deque(maxlen=MAX_WS_FRAMES))
    handshake_request_headers: dict[str, Any] | None = None
    handshake_response_headers: dict[str, Any] | None = None
    handshake_status: int | None = None
    handshake_status_text: str | None = None
    frame_errors: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    closed: bool = False
