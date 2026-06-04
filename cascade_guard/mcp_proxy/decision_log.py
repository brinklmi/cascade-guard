"""Decision Log — append-only audit trail for SOC 2 / ISO 42001 compliance.

Records every tool invocation verdict with deterministic replay capability.
Outputs JSON Lines compatible with CloudWatch Logs.

Deterministic replay invariant:
  replay(log.entries, same_config) == original_verdicts
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional


@dataclass
class DecisionEntry:
    """A single audit log entry for a tool invocation verdict.

    Contains all data needed for deterministic replay and compliance reporting.
    """

    timestamp: float
    agent_id: str
    tool_name: str
    verdict: str  # "allowed" | "blocked"
    reason: str
    impedance: float = 0.0
    flow_state: str = "nominal"
    tokens_consumed: float = 0.0
    schema_version: str = "1.0"
    latency_us: int = 0  # Safety check latency in microseconds

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    def to_json_line(self) -> str:
        """Convert to JSON Lines format (one line, no trailing newline).

        Compatible with CloudWatch Logs structured format.
        """
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionEntry":
        """Reconstruct from dictionary.

        Args:
            data: Dictionary with entry fields.

        Returns:
            DecisionEntry instance.
        """
        return cls(
            timestamp=float(data.get("timestamp", 0.0)),
            agent_id=str(data.get("agent_id", "")),
            tool_name=str(data.get("tool_name", "")),
            verdict=str(data.get("verdict", "blocked")),
            reason=str(data.get("reason", "")),
            impedance=float(data.get("impedance", 0.0)),
            flow_state=str(data.get("flow_state", "nominal")),
            tokens_consumed=float(data.get("tokens_consumed", 0.0)),
            schema_version=str(data.get("schema_version", "1.0")),
            latency_us=int(data.get("latency_us", 0)),
        )


@dataclass
class ReplayInput:
    """Input tuple for deterministic replay.

    The minimal data needed to reproduce a verdict sequence.
    """

    agent_id: str
    tool_name: str
    timestamp: float


class DecisionLog:
    """Append-only audit trail with deterministic replay capability.

    Stores decisions in a ring buffer (bounded memory).
    Supports structured output in JSON Lines and CloudWatch format.
    Enables deterministic replay for compliance verification.

    Thread-safety: Not thread-safe. In async contexts, callers should
    ensure sequential access (natural with asyncio single-threaded event loop).
    """

    DEFAULT_MAX_ENTRIES = 10000
    DEFAULT_QUERY_LIMIT = 50

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        """Initialize the decision log.

        Args:
            max_entries: Maximum entries to retain in the ring buffer.
        """
        self._max_entries = max_entries
        self._entries: Deque[DecisionEntry] = deque(maxlen=max_entries)
        self._total_appended: int = 0  # Lifetime counter (not bounded)

    @property
    def size(self) -> int:
        """Number of entries currently in the log."""
        return len(self._entries)

    @property
    def total_appended(self) -> int:
        """Total entries ever appended (including evicted)."""
        return self._total_appended

    @property
    def max_entries(self) -> int:
        """Maximum entries retained."""
        return self._max_entries

    def append(self, entry: DecisionEntry) -> None:
        """Append a decision entry to the log.

        If the log is full, the oldest entry is evicted (ring buffer).

        Args:
            entry: The decision entry to append.
        """
        self._entries.append(entry)
        self._total_appended += 1

    def record(
        self,
        agent_id: str,
        tool_name: str,
        verdict: str,
        reason: str,
        impedance: float = 0.0,
        flow_state: str = "nominal",
        tokens_consumed: float = 0.0,
        schema_version: str = "1.0",
        latency_us: int = 0,
    ) -> DecisionEntry:
        """Create and append a decision entry (convenience method).

        Automatically sets timestamp to current time.

        Args:
            agent_id: The agent making the tool call.
            tool_name: Fully-qualified tool name.
            verdict: "allowed" or "blocked".
            reason: Reason for the verdict.
            impedance: Current κ_effective impedance.
            flow_state: Current flow state string.
            tokens_consumed: Tokens consumed by the agent so far.
            schema_version: Envelope schema version used.
            latency_us: Safety check latency in microseconds.

        Returns:
            The created DecisionEntry.
        """
        entry = DecisionEntry(
            timestamp=time.time(),
            agent_id=agent_id,
            tool_name=tool_name,
            verdict=verdict,
            reason=reason,
            impedance=impedance,
            flow_state=flow_state,
            tokens_consumed=tokens_consumed,
            schema_version=schema_version,
            latency_us=latency_us,
        )
        self.append(entry)
        return entry

    def query(self, n: int = DEFAULT_QUERY_LIMIT) -> List[DecisionEntry]:
        """Query the most recent N entries.

        Args:
            n: Maximum number of entries to return (default 50).

        Returns:
            List of most recent entries (newest last).
        """
        if n >= len(self._entries):
            return list(self._entries)
        return list(self._entries)[-n:]

    def query_by_agent(self, agent_id: str, n: int = DEFAULT_QUERY_LIMIT) -> List[DecisionEntry]:
        """Query entries for a specific agent.

        Args:
            agent_id: The agent to filter by.
            n: Maximum entries to return.

        Returns:
            List of entries for the agent (newest last).
        """
        filtered = [e for e in self._entries if e.agent_id == agent_id]
        return filtered[-n:]

    def query_blocked(self, n: int = DEFAULT_QUERY_LIMIT) -> List[DecisionEntry]:
        """Query only blocked entries.

        Args:
            n: Maximum entries to return.

        Returns:
            List of blocked entries (newest last).
        """
        filtered = [e for e in self._entries if e.verdict == "blocked"]
        return filtered[-n:]

    def export_json_lines(self, n: Optional[int] = None) -> str:
        """Export entries as JSON Lines (one JSON object per line).

        Compatible with CloudWatch Logs and standard log aggregation.

        Args:
            n: Number of entries to export (None = all).

        Returns:
            String with one JSON object per line.
        """
        entries = self.query(n) if n else list(self._entries)
        lines = [entry.to_json_line() for entry in entries]
        return "\n".join(lines)

    def get_replay_inputs(self) -> List[ReplayInput]:
        """Extract replay inputs from the current log.

        Returns the minimal data needed to reproduce the verdict sequence
        against the same engine configuration.

        Returns:
            List of ReplayInput tuples.
        """
        return [
            ReplayInput(
                agent_id=entry.agent_id,
                tool_name=entry.tool_name,
                timestamp=entry.timestamp,
            )
            for entry in self._entries
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Get summary statistics for the decision log.

        Returns:
            Dict with counts, rates, and distribution info.
        """
        total = len(self._entries)
        if total == 0:
            return {
                "total_entries": 0,
                "allowed_count": 0,
                "blocked_count": 0,
                "allowed_rate": 0.0,
                "blocked_rate": 0.0,
                "unique_agents": 0,
                "unique_tools": 0,
                "avg_latency_us": 0,
            }

        allowed = sum(1 for e in self._entries if e.verdict == "allowed")
        blocked = total - allowed
        unique_agents = len(set(e.agent_id for e in self._entries))
        unique_tools = len(set(e.tool_name for e in self._entries))
        avg_latency = sum(e.latency_us for e in self._entries) / total

        return {
            "total_entries": total,
            "allowed_count": allowed,
            "blocked_count": blocked,
            "allowed_rate": allowed / total,
            "blocked_rate": blocked / total,
            "unique_agents": unique_agents,
            "unique_tools": unique_tools,
            "avg_latency_us": round(avg_latency, 1),
        }

    def clear(self) -> None:
        """Clear all entries (for testing only)."""
        self._entries.clear()
