"""Observability & Metrics — CloudWatch EMF + Prometheus exposition format.

Emits: invocations/sec, blocked/sec, safety check latency, κ_effective,
active agents, flow state transitions, per-agent throttle events.
Exposes /health, /ready, and /metrics endpoints.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


@dataclass
class FlowStateChangeEvent:
    """Records a flow state transition."""

    timestamp: float
    previous_state: str
    new_state: str
    trigger: str  # What caused the transition
    kappa_effective: float = 0.0


@dataclass
class AgentThrottleEvent:
    """Records an agent throttle state change."""

    timestamp: float
    agent_id: str
    throttled: bool  # True = entered throttled, False = recovered
    current_rate: float
    threshold: float


@dataclass
class HealthState:
    """Current health state for /health endpoint."""

    flow_state: str = "nominal"
    kappa_effective: float = 1.0
    active_agents: int = 0
    total_invocations: int = 0
    total_blocked: int = 0
    uptime_seconds: float = 0.0
    is_draining: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for HTTP response."""
        return {
            "flow_state": self.flow_state,
            "kappa_effective": self.kappa_effective,
            "active_agents": self.active_agents,
            "total_invocations": self.total_invocations,
            "total_blocked": self.total_blocked,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "is_draining": self.is_draining,
        }


class MetricsEmitter:
    """Metrics collection and emission for the MCP proxy.

    Collects operational metrics and emits them in formats compatible
    with CloudWatch Embedded Metric Format and Prometheus exposition format.

    Also manages /health, /ready, and /metrics endpoint data.
    """

    def __init__(self):
        """Initialize metrics emitter."""
        self._start_time = time.time()
        self._total_invocations: int = 0
        self._total_blocked: int = 0
        self._latency_window: Deque[float] = deque(maxlen=1000)  # Last 1000 latencies (μs)
        self._flow_state: str = "nominal"
        self._kappa_effective: float = 1.0
        self._active_agents: int = 0
        self._is_draining: bool = False

        # Event logs (bounded)
        self._flow_state_events: Deque[FlowStateChangeEvent] = deque(maxlen=100)
        self._throttle_events: Deque[AgentThrottleEvent] = deque(maxlen=100)

        # Per-second counters (rolling 60s window for rate calculation)
        self._invocation_timestamps: Deque[float] = deque(maxlen=10000)
        self._blocked_timestamps: Deque[float] = deque(maxlen=10000)

    @property
    def total_invocations(self) -> int:
        """Total tool invocations processed."""
        return self._total_invocations

    @property
    def total_blocked(self) -> int:
        """Total tool invocations blocked."""
        return self._total_blocked

    @property
    def uptime_seconds(self) -> float:
        """Seconds since metrics emitter started."""
        return time.time() - self._start_time

    def emit_invocation(self, tool_name: str, verdict: str) -> None:
        """Record a tool invocation.

        Args:
            tool_name: The invoked tool name.
            verdict: "allowed" or "blocked".
        """
        now = time.time()
        self._total_invocations += 1
        self._invocation_timestamps.append(now)

        if verdict == "blocked":
            self._total_blocked += 1
            self._blocked_timestamps.append(now)

    def emit_latency(self, latency_us: int) -> None:
        """Record safety check latency.

        Args:
            latency_us: Latency in microseconds.
        """
        self._latency_window.append(float(latency_us))

    def emit_flow_state_change(
        self,
        previous_state: str,
        new_state: str,
        trigger: str,
        kappa_effective: float = 0.0,
    ) -> None:
        """Record a flow state transition.

        Args:
            previous_state: Flow state before transition.
            new_state: Flow state after transition.
            trigger: What caused the transition.
            kappa_effective: Current κ_effective at transition time.
        """
        self._flow_state = new_state
        self._kappa_effective = kappa_effective

        event = FlowStateChangeEvent(
            timestamp=time.time(),
            previous_state=previous_state,
            new_state=new_state,
            trigger=trigger,
            kappa_effective=kappa_effective,
        )
        self._flow_state_events.append(event)

    def emit_agent_throttle(
        self,
        agent_id: str,
        throttled: bool,
        current_rate: float,
        threshold: float,
    ) -> None:
        """Record an agent throttle state change.

        Args:
            agent_id: The agent affected.
            throttled: True if entering throttled, False if recovering.
            current_rate: Agent's current invocation rate.
            threshold: Agent's velocity threshold.
        """
        event = AgentThrottleEvent(
            timestamp=time.time(),
            agent_id=agent_id,
            throttled=throttled,
            current_rate=current_rate,
            threshold=threshold,
        )
        self._throttle_events.append(event)

    def update_active_agents(self, count: int) -> None:
        """Update active agent count.

        Args:
            count: Current number of active agents.
        """
        self._active_agents = count

    def update_kappa(self, kappa: float) -> None:
        """Update κ_effective value.

        Args:
            kappa: Current κ_effective.
        """
        self._kappa_effective = kappa

    def set_draining(self, draining: bool) -> None:
        """Set drain state for /ready endpoint.

        Args:
            draining: True if proxy is in drain mode.
        """
        self._is_draining = draining

    def invocations_per_second(self) -> float:
        """Calculate current invocations per second (60s window).

        Returns:
            Invocations per second.
        """
        return self._rate_from_timestamps(self._invocation_timestamps)

    def blocked_per_second(self) -> float:
        """Calculate current blocked invocations per second (60s window).

        Returns:
            Blocked invocations per second.
        """
        return self._rate_from_timestamps(self._blocked_timestamps)

    def avg_latency_us(self) -> float:
        """Average safety check latency in microseconds.

        Returns:
            Average latency (0 if no data).
        """
        if not self._latency_window:
            return 0.0
        return sum(self._latency_window) / len(self._latency_window)

    def health_check(self) -> HealthState:
        """Get current health state for /health endpoint.

        Returns:
            HealthState with current metrics.
        """
        return HealthState(
            flow_state=self._flow_state,
            kappa_effective=self._kappa_effective,
            active_agents=self._active_agents,
            total_invocations=self._total_invocations,
            total_blocked=self._total_blocked,
            uptime_seconds=self.uptime_seconds,
            is_draining=self._is_draining,
        )

    def ready_check(self) -> bool:
        """Check if proxy is ready to serve (for /ready endpoint).

        Returns:
            True if ready (not draining), False if draining.
        """
        return not self._is_draining

    def to_prometheus(self) -> str:
        """Export metrics in Prometheus exposition format.

        Returns:
            Multi-line string in Prometheus text format.
        """
        lines = []
        lines.append("# HELP cascadeguard_invocations_total Total tool invocations")
        lines.append("# TYPE cascadeguard_invocations_total counter")
        lines.append(f"cascadeguard_invocations_total {self._total_invocations}")
        lines.append("")
        lines.append("# HELP cascadeguard_blocked_total Total blocked invocations")
        lines.append("# TYPE cascadeguard_blocked_total counter")
        lines.append(f"cascadeguard_blocked_total {self._total_blocked}")
        lines.append("")
        lines.append("# HELP cascadeguard_invocations_per_second Current invocation rate")
        lines.append("# TYPE cascadeguard_invocations_per_second gauge")
        lines.append(f"cascadeguard_invocations_per_second {self.invocations_per_second():.2f}")
        lines.append("")
        lines.append("# HELP cascadeguard_blocked_per_second Current blocked rate")
        lines.append("# TYPE cascadeguard_blocked_per_second gauge")
        lines.append(f"cascadeguard_blocked_per_second {self.blocked_per_second():.2f}")
        lines.append("")
        lines.append("# HELP cascadeguard_safety_check_latency_us Average safety check latency")
        lines.append("# TYPE cascadeguard_safety_check_latency_us gauge")
        lines.append(f"cascadeguard_safety_check_latency_us {self.avg_latency_us():.1f}")
        lines.append("")
        lines.append("# HELP cascadeguard_kappa_effective Current impedance value")
        lines.append("# TYPE cascadeguard_kappa_effective gauge")
        lines.append(f"cascadeguard_kappa_effective {self._kappa_effective:.4f}")
        lines.append("")
        lines.append("# HELP cascadeguard_active_agents Number of active agents")
        lines.append("# TYPE cascadeguard_active_agents gauge")
        lines.append(f"cascadeguard_active_agents {self._active_agents}")
        lines.append("")
        return "\n".join(lines)

    def to_cloudwatch_emf(self) -> str:
        """Export metrics in CloudWatch Embedded Metric Format.

        Returns:
            JSON string in EMF format.
        """
        emf = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "CascadeGuard/MCPProxy",
                        "Dimensions": [["FlowState"]],
                        "Metrics": [
                            {"Name": "InvocationsTotal", "Unit": "Count"},
                            {"Name": "BlockedTotal", "Unit": "Count"},
                            {"Name": "InvocationsPerSecond", "Unit": "Count/Second"},
                            {"Name": "BlockedPerSecond", "Unit": "Count/Second"},
                            {"Name": "SafetyCheckLatencyUs", "Unit": "Microseconds"},
                            {"Name": "KappaEffective", "Unit": "None"},
                            {"Name": "ActiveAgents", "Unit": "Count"},
                        ],
                    }
                ],
            },
            "FlowState": self._flow_state,
            "InvocationsTotal": self._total_invocations,
            "BlockedTotal": self._total_blocked,
            "InvocationsPerSecond": round(self.invocations_per_second(), 2),
            "BlockedPerSecond": round(self.blocked_per_second(), 2),
            "SafetyCheckLatencyUs": round(self.avg_latency_us(), 1),
            "KappaEffective": round(self._kappa_effective, 4),
            "ActiveAgents": self._active_agents,
        }
        return json.dumps(emf)

    def get_flow_state_events(self, n: int = 10) -> List[Dict[str, Any]]:
        """Get recent flow state change events.

        Args:
            n: Maximum events to return.

        Returns:
            List of event dicts (newest last).
        """
        events = list(self._flow_state_events)[-n:]
        return [
            {
                "timestamp": e.timestamp,
                "previous_state": e.previous_state,
                "new_state": e.new_state,
                "trigger": e.trigger,
                "kappa_effective": e.kappa_effective,
            }
            for e in events
        ]

    def get_throttle_events(self, n: int = 10) -> List[Dict[str, Any]]:
        """Get recent agent throttle events.

        Args:
            n: Maximum events to return.

        Returns:
            List of event dicts (newest last).
        """
        events = list(self._throttle_events)[-n:]
        return [
            {
                "timestamp": e.timestamp,
                "agent_id": e.agent_id,
                "throttled": e.throttled,
                "current_rate": e.current_rate,
                "threshold": e.threshold,
            }
            for e in events
        ]

    def _rate_from_timestamps(self, timestamps: Deque[float], window: float = 60.0) -> float:
        """Calculate rate from a deque of timestamps over a rolling window.

        Args:
            timestamps: Deque of event timestamps.
            window: Window size in seconds.

        Returns:
            Events per second in the window.
        """
        now = time.time()
        cutoff = now - window

        # Count events in window
        count = sum(1 for t in timestamps if t >= cutoff)

        if count == 0:
            return 0.0

        return count / window
