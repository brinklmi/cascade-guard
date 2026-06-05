"""Migration Agent — translates ETRM events into governed MCP tool calls.

This is the bridge between legacy ETRM event systems (RightAngle BA Events,
Allegro notifications, etc.) and CascadeGuard's MCP Proxy.

The agent:
1. Listens for events from the source system (webhook, queue, CDC stream)
2. Transforms the event payload into the target system's schema
3. Wraps it as an MCP tools/call request with CascadeGuard envelope
4. Sends through CascadeGuard Proxy → target system

The agent itself is governed by CascadeGuard:
- Rate-limited (velocity throttling)
- Budget-capped (token/dollar enforcement)
- Cycle-prevented (cannot write back to source)
- Namespace-restricted (read source, write target only)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from enum import Enum

logger = logging.getLogger("cascadeguard.migration")


class EventType(str, Enum):
    """ETRM deal lifecycle event types."""

    DEAL_CREATED = "deal_created"
    DEAL_AMENDED = "deal_amended"
    DEAL_CONFIRMED = "deal_confirmed"
    DEAL_SCHEDULED = "deal_scheduled"
    DEAL_SETTLED = "deal_settled"
    DEAL_CANCELLED = "deal_cancelled"
    POSITION_UPDATED = "position_updated"
    NOMINATION_SUBMITTED = "nomination_submitted"


@dataclass
class ETRMEvent:
    """An event from the source ETRM system.

    Represents a business event (deal created, amended, etc.)
    captured from the legacy system's event mechanism.
    """

    event_type: EventType
    source_system: str  # "rightangle" | "allegro" | etc.
    deal_id: str
    timestamp: float = field(default_factory=time.time)
    payload: Dict[str, Any] = field(default_factory=dict)
    commodity: str = ""
    counterparty: str = ""
    portfolio: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "source_system": self.source_system,
            "deal_id": self.deal_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "commodity": self.commodity,
            "counterparty": self.counterparty,
            "portfolio": self.portfolio,
        }


@dataclass
class MigrationResult:
    """Result of migrating a single event to the target system."""

    success: bool
    source_event: ETRMEvent
    target_tool: str = ""
    error: str = ""
    latency_ms: float = 0.0
    governed: bool = True  # Passed through CascadeGuard

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "deal_id": self.source_event.deal_id,
            "event_type": self.source_event.event_type.value,
            "target_tool": self.target_tool,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "governed": self.governed,
        }


class SchemaTransformer:
    """Transforms source ETRM event payload to target system schema.

    Subclass this for each source→target pair:
    - RightAngleToEndurTransformer
    - AllegroToEndurTransformer
    - etc.
    """

    def transform(
        self, event: ETRMEvent, target_tool: str
    ) -> Dict[str, Any]:
        """Transform source event payload to target tool arguments.

        Args:
            event: The source ETRM event.
            target_tool: The target MCP tool name (e.g. "endur/create_deal").

        Returns:
            Dict of arguments for the target MCP tool call.
        """
        raise NotImplementedError("Subclass must implement transform()")

    def get_target_tool(self, event: ETRMEvent) -> str:
        """Determine which target tool to call for this event type.

        Args:
            event: The source ETRM event.

        Returns:
            Target tool name (e.g. "endur/create_deal").
        """
        raise NotImplementedError("Subclass must implement get_target_tool()")


class RightAngleToEndurTransformer(SchemaTransformer):
    """Transforms RightAngle deal events to Endur JVS API calls.

    Maps RightAngle's deal structure to Endur's ab_tran schema:
    - RA deal_header → Endur ins_type + tran_type
    - RA deal_detail → Endur param table entries
    - RA deal_transport → Endur nom_info
    - RA scheduling → Endur nom_schedule
    """

    # RightAngle event type → Endur target tool mapping
    TOOL_MAP = {
        EventType.DEAL_CREATED: "endur/create_deal",
        EventType.DEAL_AMENDED: "endur/amend_deal",
        EventType.DEAL_CONFIRMED: "endur/confirm_deal",
        EventType.DEAL_SCHEDULED: "endur/submit_schedule",
        EventType.DEAL_SETTLED: "endur/settle_deal",
        EventType.DEAL_CANCELLED: "endur/cancel_deal",
        EventType.POSITION_UPDATED: "endur/update_position",
        EventType.NOMINATION_SUBMITTED: "endur/submit_nomination",
    }

    def get_target_tool(self, event: ETRMEvent) -> str:
        return self.TOOL_MAP.get(event.event_type, "endur/generic_event")

    def transform(
        self, event: ETRMEvent, target_tool: str
    ) -> Dict[str, Any]:
        """Transform RightAngle payload to Endur arguments.

        This is the core schema mapping. In production, each deal type
        (physical gas, financial swap, transport, storage) has its own
        mapping logic. This base implementation handles the common fields.
        """
        payload = event.payload

        # Common field mapping: RightAngle → Endur
        endur_args = {
            # Deal identity
            "external_reference": f"RA-{event.deal_id}",
            "deal_tracking_num": payload.get("deal_num", event.deal_id),

            # Instrument classification
            "ins_type": self._map_ins_type(payload),
            "tran_type": self._map_tran_type(event.event_type),
            "tran_status": self._map_status(event.event_type),

            # Counterparty
            "external_bunit": payload.get("counterparty", event.counterparty),
            "internal_bunit": payload.get("internal_company", ""),
            "internal_portfolio": payload.get("book", event.portfolio),

            # Commodity
            "commodity": event.commodity,
            "pipeline": payload.get("pipeline", ""),
            "location": payload.get("delivery_point", ""),

            # Dates
            "start_date": payload.get("start_date", ""),
            "end_date": payload.get("end_date", ""),
            "trade_date": payload.get("trade_date", ""),

            # Volume & Price
            "quantity": payload.get("volume", 0.0),
            "quantity_unit": payload.get("volume_unit", ""),
            "price": payload.get("price", 0.0),
            "price_unit": payload.get("price_unit", ""),
            "currency": payload.get("currency", "USD"),

            # Source tracking (for reconciliation)
            "_migration_source": "rightangle",
            "_source_deal_id": event.deal_id,
            "_migration_timestamp": event.timestamp,
        }

        return endur_args

    def _map_ins_type(self, payload: Dict[str, Any]) -> str:
        """Map RightAngle deal type to Endur instrument type."""
        ra_type = payload.get("deal_type", "").lower()
        mapping = {
            "physical": "PHYS",
            "financial": "FIN",
            "swap": "SWAP",
            "option": "OPT",
            "transport": "TRANS",
            "storage": "STOR",
        }
        return mapping.get(ra_type, "PHYS")

    def _map_tran_type(self, event_type: EventType) -> str:
        """Map event type to Endur transaction type."""
        if event_type == EventType.DEAL_CREATED:
            return "NEW"
        elif event_type == EventType.DEAL_AMENDED:
            return "AMEND"
        elif event_type == EventType.DEAL_CANCELLED:
            return "CANCEL"
        return "NEW"

    def _map_status(self, event_type: EventType) -> str:
        """Map event type to Endur transaction status."""
        status_map = {
            EventType.DEAL_CREATED: "Pending",
            EventType.DEAL_CONFIRMED: "Validated",
            EventType.DEAL_SCHEDULED: "Scheduled",
            EventType.DEAL_SETTLED: "Settled",
            EventType.DEAL_CANCELLED: "Cancelled",
        }
        return status_map.get(event_type, "Pending")


class MigrationAgent:
    """The core migration agent — listens, transforms, routes through CascadeGuard.

    This is the "thin adapter" that bridges legacy ETRM events to the
    MCP Proxy. It's intentionally lightweight — all safety logic lives
    in CascadeGuard, not here.

    Usage:
        agent = MigrationAgent(
            agent_id="ra-to-endur-gas",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
            mcp_proxy_call=proxy.handle_request,
        )
        result = agent.process_event(event)
    """

    def __init__(
        self,
        agent_id: str,
        source_system: str,
        target_server: str,
        transformer: SchemaTransformer,
        mcp_proxy_call: Optional[Callable] = None,
        commodity_filter: Optional[List[str]] = None,
    ):
        """Initialize migration agent.

        Args:
            agent_id: Unique ID for this agent (used in CascadeGuard tracking).
            source_system: Source ETRM identifier ("rightangle", "allegro").
            target_server: Target server namespace in CascadeGuard registry.
            transformer: Schema transformer for source → target mapping.
            mcp_proxy_call: Callable to send MCP requests through CascadeGuard.
            commodity_filter: Optional list of commodities to process (None = all).
        """
        self._agent_id = agent_id
        self._source_system = source_system
        self._target_server = target_server
        self._transformer = transformer
        self._mcp_proxy_call = mcp_proxy_call
        self._commodity_filter = commodity_filter

        # Stats
        self._events_processed: int = 0
        self._events_succeeded: int = 0
        self._events_failed: int = 0
        self._events_filtered: int = 0

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "processed": self._events_processed,
            "succeeded": self._events_succeeded,
            "failed": self._events_failed,
            "filtered": self._events_filtered,
        }

    def process_event(self, event: ETRMEvent) -> MigrationResult:
        """Process a single ETRM event through the migration pipeline.

        Pipeline:
        1. Filter (commodity check)
        2. Transform (source schema → target schema)
        3. Build MCP request with CascadeGuard envelope
        4. Route through CascadeGuard Proxy
        5. Return result

        Args:
            event: The source ETRM event to migrate.

        Returns:
            MigrationResult with success/failure and metadata.
        """
        self._events_processed += 1
        start = time.perf_counter_ns()

        # 1. Commodity filter
        if self._commodity_filter and event.commodity not in self._commodity_filter:
            self._events_filtered += 1
            return MigrationResult(
                success=True,
                source_event=event,
                error="filtered_by_commodity",
                governed=False,
            )

        # 2. Determine target tool
        target_tool = self._transformer.get_target_tool(event)
        full_tool_name = f"{self._target_server}/{target_tool.split('/')[-1]}"

        # 3. Transform payload
        try:
            arguments = self._transformer.transform(event, target_tool)
        except Exception as e:
            self._events_failed += 1
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000
            return MigrationResult(
                success=False,
                source_event=event,
                target_tool=full_tool_name,
                error=f"transform_error: {str(e)}",
                latency_ms=latency_ms,
            )

        # 4. Build MCP request with CascadeGuard envelope
        mcp_request = {
            "method": "tools/call",
            "params": {
                "name": full_tool_name,
                "arguments": arguments,
                "_cascadeguard": {
                    "schema_version": "1.0",
                    "agent_id": self._agent_id,
                    "caller_id": f"migration-{self._source_system}",
                    "token_budget": 1000,
                    "execution_seconds": 30,
                },
            },
            "id": f"migration-{event.deal_id}-{int(event.timestamp)}",
        }

        # 5. Route through CascadeGuard (if proxy connected)
        if self._mcp_proxy_call is None:
            # Dry-run mode — no proxy connected, just validate transform
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000
            self._events_succeeded += 1
            return MigrationResult(
                success=True,
                source_event=event,
                target_tool=full_tool_name,
                latency_ms=latency_ms,
                governed=False,
            )

        try:
            response = self._mcp_proxy_call(mcp_request)
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000

            if response and response.get("error"):
                self._events_failed += 1
                return MigrationResult(
                    success=False,
                    source_event=event,
                    target_tool=full_tool_name,
                    error=response["error"].get("message", "unknown_error"),
                    latency_ms=latency_ms,
                    governed=True,
                )

            self._events_succeeded += 1
            return MigrationResult(
                success=True,
                source_event=event,
                target_tool=full_tool_name,
                latency_ms=latency_ms,
                governed=True,
            )

        except Exception as e:
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000
            self._events_failed += 1
            return MigrationResult(
                success=False,
                source_event=event,
                target_tool=full_tool_name,
                error=f"proxy_error: {str(e)}",
                latency_ms=latency_ms,
            )

    def process_batch(self, events: List[ETRMEvent]) -> List[MigrationResult]:
        """Process a batch of events sequentially.

        Args:
            events: List of ETRM events to migrate.

        Returns:
            List of MigrationResult for each event.
        """
        return [self.process_event(event) for event in events]
