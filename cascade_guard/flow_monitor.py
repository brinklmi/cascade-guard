"""Flow Monitor — Distribution-Based Impedance for Delegation Control.

Computes impedance from delegation flow statistics rather than vector embeddings.
No pairwise similarity computation needed — just rolling window stats.

Impedance formula:
    Z = w_v * velocity_ratio + w_d * depth_ratio + w_f * fanout_ratio + w_c * concentration

Where:
    velocity_ratio = current_velocity / max_velocity
    depth_ratio = max_depth / depth_limit
    fanout_ratio = max_fanout / fanout_limit
    concentration = gini(delegation_counts_per_source)

κ_effective = κ_duat * (1 - Z)

When κ_effective < preservation_threshold → PRESERVATION mode (block all).
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Optional

from .models import FlowState, ImpedanceReport


class FlowMonitor:
    """Monitors delegation flow and computes distribution-based impedance.

    Parameters
    ----------
    max_velocity : float
        Maximum delegations/second before full impedance (default 50.0).
    depth_limit : int
        Maximum delegation chain depth before full impedance (default 10).
    fanout_limit : int
        Maximum children per agent before full impedance (default 20).
    window_seconds : float
        Rolling window for velocity computation (default 60.0).
    preservation_threshold : float
        κ_effective below this triggers preservation mode (default 0.3).
    kappa_duat : float
        Base metabolic constant (default 1.0).
    weights : tuple[float, float, float, float]
        Weights for (velocity, depth, fanout, concentration). Default (0.4, 0.2, 0.2, 0.2).
    """

    def __init__(
        self,
        max_velocity: float = 50.0,
        depth_limit: int = 10,
        fanout_limit: int = 20,
        window_seconds: float = 60.0,
        preservation_threshold: float = 0.3,
        kappa_duat: float = 1.0,
        weights: tuple[float, float, float, float] = (0.4, 0.2, 0.2, 0.2),
    ):
        self.max_velocity = max_velocity
        self.depth_limit = depth_limit
        self.fanout_limit = fanout_limit
        self.window_seconds = window_seconds
        self.preservation_threshold = preservation_threshold
        self.kappa_duat = kappa_duat
        self.weights = weights

        # Rolling window of delegation timestamps
        self._timestamps: deque[float] = deque()

        # Per-source delegation counts (for concentration)
        self._source_counts: dict[str, int] = {}

        # Depth and fanout tracking
        self._max_depth: int = 0
        self._depths: dict[str, int] = {}
        self._children_count: dict[str, int] = {}

    @property
    def velocity(self) -> float:
        """Current delegation velocity (delegations/second) in the rolling window."""
        self._prune_window()
        if not self._timestamps:
            return 0.0
        elapsed = time.time() - self._timestamps[0]
        if elapsed < 0.001:
            return float(len(self._timestamps))
        return len(self._timestamps) / elapsed

    @property
    def kappa_effective(self) -> float:
        """Current κ_effective value."""
        impedance = self.compute_impedance().impedance
        return self.kappa_duat * (1.0 - impedance)

    @property
    def flow_state(self) -> FlowState:
        """Current flow state based on impedance."""
        kappa = self.kappa_effective
        if kappa >= 0.8:
            return FlowState.NOMINAL
        elif kappa >= 0.5:
            return FlowState.ELEVATED
        elif kappa >= self.preservation_threshold:
            return FlowState.THROTTLED
        else:
            return FlowState.PRESERVATION

    def can_delegate(self) -> bool:
        """Check if delegation is allowed under current flow state."""
        return self.kappa_effective >= self.preservation_threshold

    def record_delegation(
        self,
        source_id: str,
        target_id: str,
        depth: int,
        fan_out: int,
    ) -> None:
        """Record a delegation event for flow tracking.

        Parameters
        ----------
        source_id : str
            Agent performing the delegation.
        target_id : str
            Agent receiving the delegation.
        depth : int
            Depth of the target in the delegation chain.
        fan_out : int
            Number of children the source now has.
        """
        now = time.time()
        self._timestamps.append(now)

        # Update source counts
        self._source_counts[source_id] = self._source_counts.get(source_id, 0) + 1

        # Update depth tracking
        self._depths[target_id] = depth
        if depth > self._max_depth:
            self._max_depth = depth

        # Update fanout tracking
        self._children_count[source_id] = fan_out

    def compute_impedance(self) -> ImpedanceReport:
        """Compute current impedance from flow distribution statistics.

        Returns
        -------
        ImpedanceReport
            Full impedance breakdown with all component metrics.
        """
        self._prune_window()

        # Velocity component
        vel = self.velocity
        velocity_ratio = min(1.0, vel / self.max_velocity) if self.max_velocity > 0 else 0.0

        # Depth component
        depth_ratio = min(1.0, self._max_depth / self.depth_limit) if self.depth_limit > 0 else 0.0

        # Fanout component
        max_fanout = max(self._children_count.values()) if self._children_count else 0
        fanout_ratio = min(1.0, max_fanout / self.fanout_limit) if self.fanout_limit > 0 else 0.0

        # Concentration component (Gini coefficient of delegation counts)
        concentration = self._compute_concentration()

        # Weighted impedance
        w_v, w_d, w_f, w_c = self.weights
        impedance = (
            w_v * velocity_ratio
            + w_d * depth_ratio
            + w_f * fanout_ratio
            + w_c * concentration
        )
        impedance = min(1.0, max(0.0, impedance))

        # Mean depth
        mean_depth = 0.0
        if self._depths:
            mean_depth = sum(self._depths.values()) / len(self._depths)

        # Mean fanout
        fan_out_mean = 0.0
        if self._children_count:
            fan_out_mean = sum(self._children_count.values()) / len(self._children_count)

        # Determine flow state from impedance
        kappa = self.kappa_duat * (1.0 - impedance)
        if kappa >= 0.8:
            state = FlowState.NOMINAL
        elif kappa >= 0.5:
            state = FlowState.ELEVATED
        elif kappa >= self.preservation_threshold:
            state = FlowState.THROTTLED
        else:
            state = FlowState.PRESERVATION

        return ImpedanceReport(
            impedance=round(impedance, 4),
            velocity=round(vel, 2),
            mean_depth=round(mean_depth, 2),
            max_depth=self._max_depth,
            fan_out=round(fan_out_mean, 2),
            max_fan_out=max_fanout,
            concentration=round(concentration, 4),
            flow_state=state,
        )

    def _compute_concentration(self) -> float:
        """Compute Gini coefficient of delegation counts per source.

        0 = perfectly uniform (all sources delegate equally)
        1 = perfectly concentrated (one source does all delegations)
        """
        if not self._source_counts:
            return 0.0

        counts = sorted(self._source_counts.values())
        n = len(counts)
        if n <= 1:
            return 0.0

        total = sum(counts)
        if total == 0:
            return 0.0

        # Gini coefficient
        cumulative = 0.0
        weighted_sum = 0.0
        for i, count in enumerate(counts):
            cumulative += count
            weighted_sum += (2 * (i + 1) - n - 1) * count

        gini = weighted_sum / (n * total)
        return max(0.0, min(1.0, gini))

    def _prune_window(self) -> None:
        """Remove timestamps outside the rolling window."""
        cutoff = time.time() - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def reset(self) -> None:
        """Clear all flow state."""
        self._timestamps.clear()
        self._source_counts.clear()
        self._depths.clear()
        self._children_count.clear()
        self._max_depth = 0
