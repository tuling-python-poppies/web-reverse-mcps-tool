from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .collectors.single_target_network import (
    DEFAULT_ENDPOINT,
    DEFAULT_RESOURCE_TYPES,
    SingleTargetNetworkCollector,
)
from .endpoint import resolve_endpoint

CONNECT_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


class NetworkCaptureService:
    def __init__(self) -> None:
        self.collector: SingleTargetNetworkCollector | None = None
        self.monitor: dict[str, Any] | None = None

    def _is_connection_alive(self) -> bool:
        """Check if the underlying WebSocket connection is still open."""
        if self.collector is None:
            return False
        client = self.collector.client
        if client is None or client.ws is None:
            return False
        # websockets library: ws.state or ws.open
        try:
            return client.ws.state.name == "OPEN"
        except AttributeError:
            # Fallback for older websockets versions
            try:
                return client.ws.open
            except AttributeError:
                return True  # Can't determine, assume alive

    async def start_collector(self, collector: SingleTargetNetworkCollector) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                collector.start(),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await collector.stop()
            raise RuntimeError(
                "Timed out connecting to WMPFDebugger at "
                f"{collector.ws_endpoint} after {CONNECT_TIMEOUT_SECONDS:g}s. "
                "Make sure WMPFDebugger is running and reopen the target miniapp."
            ) from exc
        except Exception as exc:
            with contextlib.suppress(Exception):
                await collector.stop()
            raise RuntimeError(
                f"Failed to connect to WMPFDebugger at {collector.ws_endpoint}: {exc}"
            ) from exc

    async def ensure_monitoring(self, endpoint: str | None = None) -> dict[str, Any]:
        raw_endpoint = endpoint or (self.monitor.get("resolvedEndpoint") if self.monitor else None)
        raw_endpoint = raw_endpoint or DEFAULT_ENDPOINT
        resolved_endpoint = resolve_endpoint(raw_endpoint)

        if self.collector is not None and self.collector.ws_endpoint != resolved_endpoint:
            await self.collector.stop()
            self.collector = None
            self.monitor = None

        # Health check: if collector exists but connection is dead, tear it down
        if self.collector is not None and not self._is_connection_alive():
            logger.info("WebSocket connection is dead, reconnecting...")
            with contextlib.suppress(Exception):
                await self.collector.stop()
            self.collector = None
            self.monitor = None

        if self.collector is None:
            collector = SingleTargetNetworkCollector(
                endpoint=raw_endpoint,
                resource_types=set(DEFAULT_RESOURCE_TYPES),
            )
            monitor = await self.start_collector(collector)
            self.collector = collector
            self.monitor = monitor
        return self.monitor or {}

    def list_requests(
        self,
        reqid: str | None = None,
        page_size: int = 20,
        page_idx: int = 0,
        resource_types: list[str] | None = None,
        url_filter: str | None = None,
        include_preserved_requests: bool = False,
    ) -> dict[str, Any]:
        if self.collector is None:
            return {
                "mode": "list",
                "totalCount": 0,
                "pageSize": page_size,
                "pageIdx": page_idx,
                "requests": [],
            }
        return self.collector.list_requests(
            reqid=reqid,
            page_size=page_size,
            page_idx=page_idx,
            resource_types=resource_types,
            url_filter=url_filter,
        )

    def clear_requests(self) -> None:
        if self.collector is not None:
            self.collector.clear()

    async def get_response_body(
        self, request_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        if self.collector is None:
            raise RuntimeError("Network collector not started")
        return await self.collector.get_response_body(request_id)

    async def get_request_post_data(self, request_id: str) -> dict[str, Any]:
        if self.collector is None:
            raise RuntimeError("Network collector not started")
        return await self.collector.get_request_post_data(request_id)
