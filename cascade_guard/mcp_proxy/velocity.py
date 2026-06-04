"""Per-Agent Velocity Controller — noisy neighbor isolation.

Maintains independent rolling-window invocation counters per agent.
Throttles individual agents without affecting global flow state.

Uses the same deque-based rolling window pattern as FlowMonitor,
but scoped to individual agents rather than the system as a whole.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class VelocityCheck:
    """Result of checking an agent's invocation velocity.

    Attributes:
        allowed: Whether the invocation is permitted.
        current_rate: Agent's current invocations/second in the window.
        threshold: The agent's configured max velocity.
        reason: Error reason if blocked (empty if allowed).
    """

    allowed: bool
    current_rate: float
    threshold: float
    reason: str = ""


class AgentVelocityController:
    """Per-agent velocity throttling controller.

    Maintains a rolling-window invocation counter per agent_id.
    When an agent exceeds its configured threshold, only that agent
    is throttled — other agents continue unaffected.

    This operates independently of the CascadeEngine's global flow state.

    Thread-safety note: This class is NOT thread-safe. In async contexts,
    callers should ensure sequential access (which asyncio provides naturally).
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        default_threshold: Optional[float] = None,
        global_max_velocity: float = 50.0,
    ):
        """Initialize the per-agent velocity controller.

        Args:
            window_seconds: Rolling time window in seconds (matches global window).
            default_threshold: Default per-agent max velocity.
                               If None, defaults to 2x global_max_velocity.
            global_max_velocity: The system-wide max_velocity for computing default.
        """
        self._window_seconds = window_seconds
        self._global_max_velocity = global_max_velocity
        self._default_threshold = (
            default_threshold
            if default_threshold is not None
            else 2.0 * global_max_velocity
        )

        # Per-agent rolling windows: agent_id → deque of timestamps
        self._agent_windows: Dict[str, deque] = {}

        # Per-agent threshold overrides: agent_id → max invocations/second
        self._agent_thresholds: Dict[str, float] = {}

        # Track which agents are currently in throttled state
        self._throttled_agents: set = set()

    @property
    def window_seconds(self) -> float:
        """Rolling window duration in seconds."""
        return self._window_seconds

    @property
    def default_threshold(self) -> float:
        """Default per-agent velocity threshold."""
        return self._default_threshold

    def set_threshold(self, agent_id: str, max_velocity: float) -> None:
        """Set a per-agent velocity threshold override.

        Args:
            agent_id: The agent to configure.
            max_velocity: Maximum invocations per second for this agent.

        Raises:
            ValueError: If max_velocity is not positive.
        """
        if max_velocity <= 0:
            raise ValueError(
                f"max_velocity must be positive, got {max_velocity}"
            )
        self._agent_thresholds[agent_id] = max_velocity

    def get_threshold(self, agent_id: str) -> float:
        """Get the effective velocity threshold for an agent.

        Returns the agent-specific override if set, otherwise the default.

        Args:
            agent_id: The agent to query.

        Returns:
            The effective max invocations/second for this agent.
        """
        return self._agent_thresholds.get(agent_id, self._default_threshold)

    def remove_threshold(self, agent_id: str) -> None:
        """Remove a per-agent threshold override, reverting to default.

        Args:
            agent_id: The agent whose override to remove.
        """
        self._agent_thresholds.pop(agent_id, None)

    def record_invocation(self, agent_id: str) -> None:
        """Record a tool invocation for an agent.

        Appends current timestamp to the agent's rolling window.

        Args:
            agent_id: The agent making the invocation.
        """
        now = time.time()

        if agent_id not in self._agent_windows:
            self._agent_windows[agent_id] = deque()

        self._agent_windows[agent_id].append(now)
        self._prune_agent_window(agent_id)

    def check_velocity(self, agent_id: str) -> VelocityCheck:
        """Check if an agent's current velocity exceeds its threshold.

        Does NOT record an invocation — call record_invocation() separately
        after a successful check to avoid counting blocked attempts.

        Args:
            agent_id: The agent to check.

        Returns:
            VelocityCheck with allowed/blocked status and current rate.
        """
        current_rate = self.get_agent_rate(agent_id)
        threshold = self.get_threshold(agent_id)

        if current_rate > threshold:
            # Agent is over threshold — throttle
            if agent_id not in self._throttled_agents:
                self._throttled_agents.add(agent_id)

            return VelocityCheck(
                allowed=False,
                current_rate=current_rate,
                threshold=threshold,
                reason="agent_velocity_exceeded",
            )

        # Agent is within bounds
        if agent_id in self._throttled_agents:
            self._throttled_agents.discard(agent_id)

        return VelocityCheck(
            allowed=True,
            current_rate=current_rate,
            threshold=threshold,
            reason="",
        )

    def get_agent_rate(self, agent_id: str) -> float:
        """Get the current invocation rate for an agent.

        Computed as invocations / elapsed_time within the rolling window.

        Args:
            agent_id: The agent to query.

        Returns:
            Current invocations per second (0.0 if no invocations).
        """
        if agent_id not in self._agent_windows:
            return 0.0

        self._prune_agent_window(agent_id)

        # After pruning, window may have been removed if empty
        if agent_id not in self._agent_windows:
            return 0.0

        window = self._agent_windows[agent_id]

        if not window:
            return 0.0

        # Same velocity computation as FlowMonitor
        elapsed = time.time() - window[0]
        if elapsed < 0.001:
            return float(len(window))

        return len(window) / elapsed

    def is_throttled(self, agent_id: str) -> bool:
        """Check if an agent is currently in throttled state.

        Args:
            agent_id: The agent to query.

        Returns:
            True if the agent was throttled on its last check.
        """
        return agent_id in self._throttled_agents

    def get_throttled_agents(self) -> set:
        """Get the set of currently throttled agent IDs.

        Returns:
            Set of agent_id strings that are currently throttled.
        """
        return set(self._throttled_agents)

    def get_all_rates(self) -> Dict[str, float]:
        """Get current rates for all known agents.

        Returns:
            Dict mapping agent_id → current invocations/second.
        """
        rates = {}
        for agent_id in list(self._agent_windows.keys()):
            rates[agent_id] = self.get_agent_rate(agent_id)
        return rates

    def reset_agent(self, agent_id: str) -> None:
        """Reset an agent's velocity window and throttle state.

        Args:
            agent_id: The agent to reset.
        """
        self._agent_windows.pop(agent_id, None)
        self._throttled_agents.discard(agent_id)

    def reset(self) -> None:
        """Reset all agent windows and throttle states."""
        self._agent_windows.clear()
        self._throttled_agents.clear()

    def _prune_agent_window(self, agent_id: str) -> None:
        """Remove timestamps outside the rolling window for an agent.

        Args:
            agent_id: The agent whose window to prune.
        """
        if agent_id not in self._agent_windows:
            return

        cutoff = time.time() - self._window_seconds
        window = self._agent_windows[agent_id]

        while window and window[0] < cutoff:
            window.popleft()

        # Clean up empty windows to prevent memory leaks
        if not window:
            del self._agent_windows[agent_id]
