"""Domain models for CascadeGuard.

Focused on delegation flow control — no simplicial complexes needed.
Models track agent identity, delegation chains, and flow statistics.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Model Cost Registry — V2 model-aware pricing
# ---------------------------------------------------------------------------

# Default cost per 1K tokens by model_id.
# "tool-agent" burns zero LLM tokens (API cost is external).
DEFAULT_MODEL_COSTS: dict[str, float] = {
    # OpenAI
    "gpt-4o": 0.010,
    "gpt-4o-mini": 0.003,
    "gpt-4-turbo": 0.015,
    "gpt-4": 0.045,
    "gpt-3.5-turbo": 0.002,
    # Anthropic
    "claude-3.5-sonnet": 0.009,
    "claude-3.5": 0.009,
    "claude-3-opus": 0.045,
    "claude-3-sonnet": 0.009,
    "claude-3-haiku": 0.001,
    # Meta
    "llama-3": 0.001,
    "llama-3-70b": 0.003,
    # Tool agents (zero LLM cost — API cost is external)
    "tool-agent": 0.0,
    "tool": 0.0,
    # Fallback
    "unknown": 0.03,
}


class ModelCostRegistry:
    """Registry mapping model_id → cost_per_1k_tokens.

    Supports custom overrides and a configurable fallback rate.
    """

    def __init__(
        self,
        overrides: Optional[dict[str, float]] = None,
        fallback_cost: float = 0.03,
    ):
        self._costs: dict[str, float] = {**DEFAULT_MODEL_COSTS}
        if overrides:
            self._costs.update(overrides)
        self._fallback = fallback_cost

    def get_cost(self, model_id: str) -> float:
        """Resolve cost_per_1k_tokens for a model_id.

        Performs case-insensitive prefix matching as a fallback
        (e.g. "gpt-4o-2024-05-13" matches "gpt-4o").
        """
        # Exact match first
        if model_id in self._costs:
            return self._costs[model_id]

        # Case-insensitive exact match
        lower = model_id.lower()
        for key, cost in self._costs.items():
            if key.lower() == lower:
                return cost

        # Prefix match (longest prefix wins)
        best_key = ""
        best_cost = self._fallback
        for key, cost in self._costs.items():
            if lower.startswith(key.lower()) and len(key) > len(best_key):
                best_key = key
                best_cost = cost

        return best_cost

    def register(self, model_id: str, cost_per_1k: float) -> None:
        """Register or update a model's cost."""
        self._costs[model_id] = cost_per_1k

    def all_models(self) -> dict[str, float]:
        """Return all registered model costs."""
        return dict(self._costs)

    def suggest_cheaper_models(self, model_id: str, max_results: int = 3) -> list[dict[str, Any]]:
        """Suggest cheaper alternatives to a given model.

        Returns models sorted by cost (ascending) that are cheaper than the given model.
        """
        current_cost = self.get_cost(model_id)
        if current_cost <= 0:
            return []

        alternatives = []
        for mid, cost in self._costs.items():
            if cost < current_cost and mid != "unknown" and cost > 0:
                savings_pct = (1.0 - cost / current_cost) * 100
                alternatives.append({
                    "model_id": mid,
                    "cost_per_1k": cost,
                    "savings_percent": round(savings_pct, 1),
                })

        alternatives.sort(key=lambda x: x["cost_per_1k"])
        return alternatives[:max_results]


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
    Tracks delegation depth, token usage, and spawn metadata.
    """

    id: str
    model_id: str = "unknown"
    parent_id: Optional[str] = None
    depth: int = 0
    created_at: float = Field(default_factory=time.time)
    children: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    token_budget: Optional[float] = None  # Max tokens this agent may consume (None = unlimited)
    tokens_consumed: float = 0.0  # Cumulative tokens consumed by this agent
    cost_per_1k_tokens: float = 0.03  # V2: model-aware cost rate (resolved from registry at registration)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def estimated_cost(self) -> float:
        """Estimated dollar cost for this agent based on its model-specific rate."""
        return self.tokens_consumed * self.cost_per_1k_tokens / 1000.0

    @property
    def token_budget_remaining(self) -> Optional[float]:
        """Remaining token budget. None if unlimited."""
        if self.token_budget is None:
            return None
        return max(0.0, self.token_budget - self.tokens_consumed)

    @property
    def token_budget_ratio(self) -> float:
        """Fraction of budget consumed (0.0 = fresh, 1.0 = exhausted). 0.0 if unlimited."""
        if self.token_budget is None or self.token_budget <= 0:
            return 0.0
        return min(1.0, self.tokens_consumed / self.token_budget)


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
    tokens_consumed: float = 0.0  # tokens consumed by source agent so far
    token_budget_remaining: Optional[float] = None  # remaining budget for source (None = unlimited)
    cost_estimate: float = 0.0  # estimated cost in dollars for this delegation chain


class ImpedanceReport(BaseModel):
    """Distribution-based impedance metrics.

    Impedance is computed from delegation flow statistics:
    - velocity: delegations per second (rolling window)
    - depth_distribution: how deep delegation chains are getting
    - fan_out: average children per agent
    - concentration: how concentrated delegations are (Gini-like)
    - token_pressure: how close the system is to its token budget
    """

    impedance: float = 0.0  # 0.0 = free flow, 1.0 = fully blocked
    velocity: float = 0.0  # delegations/second
    mean_depth: float = 0.0  # average chain depth
    max_depth: int = 0  # deepest chain
    fan_out: float = 0.0  # average children per parent
    max_fan_out: int = 0  # widest single parent
    concentration: float = 0.0  # delegation concentration (0=uniform, 1=single source)
    token_pressure: float = 0.0  # token budget pressure (0=fresh, 1=exhausted)
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
    total_tokens_consumed: float = 0.0  # Total tokens consumed across all agents
    total_token_budget: Optional[float] = None  # System-wide token budget (None = unlimited)
    token_budget_utilization: float = 0.0  # 0.0 to 1.0
