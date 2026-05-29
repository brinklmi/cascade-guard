"""Domain models for CascadeGuard.

Focused on delegation flow control — no simplicial complexes needed.
Models track agent identity, delegation chains, and flow statistics.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class DelegationAction(str, Enum):
    """Actions that can be delegated between agents."""

    SPAWN = "spawn"  # Create a new sub-agent
    DELEGATE = "delegate"  # Pass a task to existing agent
    ESCALATE = "escalate"  # Request higher authority
    REVOKE = "revoke"  # Remove a delegation


class FlowState(str, Enum):
    """System flow states based on impedance."""

    NOMINAL = "nominal"  # Normal operation
    ELEVATED = "elevated"  # Increased delegation velocity
    THROTTLED = "throttled"  # Active throttling engaged
    PRESERVATION = "preservation"  # Emergency mode — rejecting delegations


class AgentNode(BaseModel):
    """An agent in the delegation graph.

    Maps to a node in the Union-Find forest.
    Tracks delegation depth and spawn metadata.
    """

    id: str
    model_id: str = "unknown"
    parent_id: Optional[str] = None
    depth: int = 0
    created_at: float = Field(default_factory=time.time)
    children: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


class DelegationAttempt(BaseModel):
    """A request to delegate from one agent to another."""

    source_id: str
    target_id: Optional[str] = None  # None = spawn new agent
    action: DelegationAction = DelegationAction.SPAWN
    model_id: str = "unknown"
    timestamp: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DelegationVerdict(BaseModel):
    """Result of evaluating a delegation attempt."""

    allowed: bool
    source_id: str
    target_id: Optional[str] = None
    reason: str = ""
    impedance: float = 0.0
    flow_state: FlowState = FlowState.NOMINAL
    depth: int = 0
    cycle_detected: bool = False
    velocity: float = 0.0  # delegations/second in current window


class ImpedanceReport(BaseModel):
    """Distribution-based impedance metrics.

    Impedance is computed from delegation flow statistics:
    - velocity: delegations per second (rolling window)
    - depth_distribution: how deep delegation chains are getting
    - fan_out: average children per agent
    - concentration: how concentrated delegations are (Gini-like)
    """

    impedance: float = 0.0  # 0.0 = free flow, 1.0 = fully blocked
    velocity: float = 0.0  # delegations/second
    mean_depth: float = 0.0  # average chain depth
    max_depth: int = 0  # deepest chain
    fan_out: float = 0.0  # average children per parent
    max_fan_out: int = 0  # widest single parent
    concentration: float = 0.0  # delegation concentration (0=uniform, 1=single source)
    flow_state: FlowState = FlowState.NOMINAL


class CascadeStatus(BaseModel):
    """Overall system status."""

    total_agents: int = 0
    total_delegations: int = 0
    active_roots: int = 0  # Number of independent delegation trees
    max_depth: int = 0
    max_fan_out: int = 0
    cycles_detected: int = 0
    delegations_blocked: int = 0
    flow_state: FlowState = FlowState.NOMINAL
    impedance: ImpedanceReport = Field(default_factory=ImpedanceReport)
    kappa_effective: float = 1.0
