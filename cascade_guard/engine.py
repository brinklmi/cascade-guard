"""CascadeGuard Engine — Orchestrates Cascade Detection and Prevention.

Combines Union-Find cycle detection with distribution-based impedance
to provide a complete cascade prevention system for multi-agent environments.

Key operations:
1. register_agent: Add a new agent to the delegation graph
2. attempt_delegation: Check if a delegation is safe (no cycle, within flow limits)
3. get_status: Current system health and impedance metrics
4. get_chain: Trace the delegation chain for any agent

All operations are O(α(N)) for cycle detection — effectively constant time.
"""

from __future__ import annotations

from typing import Optional

from .flow_monitor import FlowMonitor
from .models import (
    AgentNode,
    CascadeStatus,
    DelegationAction,
    DelegationAttempt,
    DelegationVerdict,
    FlowState,
    ModelCostRegistry,
)
from .union_find import UnionFind


class CascadeEngine:
    """The core CascadeGuard engine.

    Manages agent delegation graphs with cycle detection and flow control.

    Parameters
    ----------
    max_velocity : float
        Maximum delegations/second before full impedance.
    depth_limit : int
        Maximum delegation chain depth allowed.
    fanout_limit : int
        Maximum children per agent allowed.
    preservation_threshold : float
        κ_effective below this blocks all delegations.
    window_seconds : float
        Rolling window for velocity computation.
    """

    def __init__(
        self,
        max_velocity: float = 50.0,
        depth_limit: int = 10,
        fanout_limit: int = 20,
        preservation_threshold: float = 0.3,
        window_seconds: float = 60.0,
        token_budget: Optional[float] = None,
        dollar_budget: Optional[float] = None,
        cost_per_1k_tokens: float = 0.03,
        model_cost_registry: Optional[ModelCostRegistry] = None,
    ):
        # V2: Model-aware cost registry
        # If user provides a custom cost_per_1k_tokens without a registry,
        # use it as the fallback (backward-compatible flat-rate behavior).
        # The registry only applies model-specific rates when explicitly provided.
        if model_cost_registry is not None:
            self._model_costs = model_cost_registry
        else:
            # No registry provided — flat-rate mode (V1 backward compat)
            self._model_costs = ModelCostRegistry(
                overrides={},
                fallback_cost=cost_per_1k_tokens,
            )
            # Clear default model costs so fallback is always used
            self._model_costs._costs = {}

        # If dollar_budget is set, convert to token_budget
        if dollar_budget is not None and token_budget is None:
            token_budget = (dollar_budget / cost_per_1k_tokens) * 1000

        self._uf = UnionFind()
        self._flow = FlowMonitor(
            max_velocity=max_velocity,
            depth_limit=depth_limit,
            fanout_limit=fanout_limit,
            window_seconds=window_seconds,
            preservation_threshold=preservation_threshold,
            token_budget=token_budget,
            cost_per_1k_tokens=cost_per_1k_tokens,
        )

        # Agent registry
        self._agents: dict[str, AgentNode] = {}

        # Counters
        self._total_delegations: int = 0
        self._cycles_detected: int = 0
        self._delegations_blocked: int = 0

        # Limits
        self._depth_limit = depth_limit
        self._fanout_limit = fanout_limit

        # Token budget config
        self._token_budget = token_budget
        self._dollar_budget = dollar_budget
        self._cost_per_1k_tokens = cost_per_1k_tokens

    @property
    def num_agents(self) -> int:
        """Total registered agents."""
        return len(self._agents)

    @property
    def num_roots(self) -> int:
        """Number of independent delegation trees."""
        return self._uf.num_components

    def register_agent(
        self,
        agent_id: str,
        model_id: str = "unknown",
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        token_budget: Optional[float] = None,
    ) -> DelegationVerdict:
        """Register a new agent in the delegation graph.

        If parent_id is provided, this is treated as a delegation (spawn).
        The engine checks for cycles and flow limits before allowing.

        Parameters
        ----------
        agent_id : str
            Unique identifier for the new agent.
        model_id : str
            Model identifier (e.g., 'gpt-4', 'claude-3').
        parent_id : str, optional
            Parent agent ID. If None, this is a root agent.
        metadata : dict, optional
            Additional agent metadata.
        token_budget : float, optional
            Per-agent token budget. None = unlimited.

        Returns
        -------
        DelegationVerdict
            Whether the registration was allowed and why.
        """
        if agent_id in self._agents:
            return DelegationVerdict(
                allowed=False,
                source_id=parent_id or "",
                target_id=agent_id,
                reason=f"Agent '{agent_id}' already registered",
                impedance=self._flow.compute_impedance().impedance,
                flow_state=self._flow.flow_state,
            )

        # If no parent, this is a root agent — always allowed
        if parent_id is None:
            self._uf.make_set(agent_id)
            agent = AgentNode(
                id=agent_id,
                model_id=model_id,
                parent_id=None,
                depth=0,
                metadata=metadata or {},
                token_budget=token_budget,
                cost_per_1k_tokens=self._model_costs.get_cost(model_id),
            )
            self._agents[agent_id] = agent
            return DelegationVerdict(
                allowed=True,
                source_id="",
                target_id=agent_id,
                reason="Root agent registered",
                impedance=0.0,
                flow_state=self._flow.flow_state,
                depth=0,
            )

        # Parent must exist
        if parent_id not in self._agents:
            return DelegationVerdict(
                allowed=False,
                source_id=parent_id,
                target_id=agent_id,
                reason=f"Parent agent '{parent_id}' not found",
                impedance=self._flow.compute_impedance().impedance,
                flow_state=self._flow.flow_state,
            )

        # Delegate via attempt_delegation
        attempt = DelegationAttempt(
            source_id=parent_id,
            target_id=agent_id,
            action=DelegationAction.SPAWN,
            model_id=model_id,
            metadata=metadata or {},
        )
        result = self._execute_delegation(attempt)

        # Apply per-agent token budget if registration was allowed
        if result.allowed and token_budget is not None:
            self._agents[agent_id].token_budget = token_budget

        return result

    def attempt_delegation(
        self,
        source_id: str,
        target_id: str,
        action: DelegationAction = DelegationAction.DELEGATE,
        model_id: str = "unknown",
        metadata: Optional[dict] = None,
        tokens_used: float = 0.0,
    ) -> DelegationVerdict:
        """Attempt a delegation between two existing agents.

        Checks:
        1. Cycle detection (Union-Find)
        2. Flow impedance (velocity, depth, fanout, concentration, token pressure)
        3. Depth limit
        4. Fanout limit
        5. Per-agent token budget

        Parameters
        ----------
        source_id : str
            Agent performing the delegation.
        target_id : str
            Agent receiving the delegation.
        action : DelegationAction
            Type of delegation.
        model_id : str
            Model identifier for the delegation.
        metadata : dict, optional
            Additional metadata.
        tokens_used : float
            Tokens consumed by this delegation (default 0.0).

        Returns
        -------
        DelegationVerdict
            Whether the delegation was allowed and why.
        """
        attempt = DelegationAttempt(
            source_id=source_id,
            target_id=target_id,
            action=action,
            model_id=model_id,
            metadata=metadata or {},
        )
        return self._execute_delegation(attempt, tokens_used=tokens_used)

    def get_chain(self, agent_id: str) -> list[str]:
        """Trace the delegation chain from agent back to root.

        Parameters
        ----------
        agent_id : str
            Agent to trace.

        Returns
        -------
        list[str]
            Chain from root to agent (inclusive).
        """
        if agent_id not in self._agents:
            return []

        chain: list[str] = []
        current = agent_id
        visited: set[str] = set()

        while current is not None and current not in visited:
            chain.append(current)
            visited.add(current)
            agent = self._agents.get(current)
            if agent is None:
                break
            current = agent.parent_id

        chain.reverse()
        return chain

    def get_subtree(self, agent_id: str) -> list[str]:
        """Get all agents in the subtree rooted at agent_id.

        Parameters
        ----------
        agent_id : str
            Root of subtree.

        Returns
        -------
        list[str]
            All agents in the subtree (BFS order).
        """
        if agent_id not in self._agents:
            return []

        result: list[str] = []
        queue: list[str] = [agent_id]

        while queue:
            current = queue.pop(0)
            result.append(current)
            agent = self._agents[current]
            queue.extend(agent.children)

        return result

    def get_status(self) -> CascadeStatus:
        """Get current system status and impedance metrics.

        Returns
        -------
        CascadeStatus
            Full system health report.
        """
        impedance = self._flow.compute_impedance()

        max_depth = 0
        max_fanout = 0
        for agent in self._agents.values():
            if agent.depth > max_depth:
                max_depth = agent.depth
            children_count = len(agent.children)
            if children_count > max_fanout:
                max_fanout = children_count

        total_tokens = self._flow.total_tokens_consumed
        budget_util = 0.0
        if self._token_budget and self._token_budget > 0:
            budget_util = min(1.0, total_tokens / self._token_budget)

        return CascadeStatus(
            total_agents=len(self._agents),
            total_delegations=self._total_delegations,
            active_roots=self._uf.num_components,
            max_depth=max_depth,
            max_fan_out=max_fanout,
            cycles_detected=self._cycles_detected,
            delegations_blocked=self._delegations_blocked,
            flow_state=self._flow.flow_state,
            impedance=impedance,
            kappa_effective=self._flow.kappa_effective,
            total_tokens_consumed=total_tokens,
            total_token_budget=self._token_budget,
            token_budget_utilization=round(budget_util, 4),
        )

    def revoke_agent(self, agent_id: str) -> tuple[bool, str]:
        """Revoke an agent and all its descendants.

        Parameters
        ----------
        agent_id : str
            Agent to revoke.

        Returns
        -------
        tuple[bool, str]
            (success, reason)
        """
        if agent_id not in self._agents:
            return False, f"Agent '{agent_id}' not found"

        # Get full subtree to remove
        subtree = self.get_subtree(agent_id)

        # Remove from parent's children list
        agent = self._agents[agent_id]
        if agent.parent_id and agent.parent_id in self._agents:
            parent = self._agents[agent.parent_id]
            if agent_id in parent.children:
                parent.children.remove(agent_id)

        # Remove all agents in subtree
        for node_id in subtree:
            del self._agents[node_id]

        return True, f"Revoked agent '{agent_id}' and {len(subtree) - 1} descendants"

    def _execute_delegation(self, attempt: DelegationAttempt, tokens_used: float = 0.0) -> DelegationVerdict:
        """Execute a delegation attempt with all safety checks.

        Parameters
        ----------
        attempt : DelegationAttempt
            The delegation to evaluate.
        tokens_used : float
            Tokens consumed by this delegation.

        Returns
        -------
        DelegationVerdict
            Result of the evaluation.
        """
        source_id = attempt.source_id
        target_id = attempt.target_id

        # Source must exist
        if source_id not in self._agents:
            self._delegations_blocked += 1
            return DelegationVerdict(
                allowed=False,
                source_id=source_id,
                target_id=target_id,
                reason=f"Source agent '{source_id}' not found",
                impedance=self._flow.compute_impedance().impedance,
                flow_state=self._flow.flow_state,
            )

        source = self._agents[source_id]

        # Check 0: Per-agent token budget
        if tokens_used > 0 and source.token_budget is not None:
            if source.tokens_consumed + tokens_used > source.token_budget:
                self._delegations_blocked += 1
                return DelegationVerdict(
                    allowed=False,
                    source_id=source_id,
                    target_id=target_id,
                    reason=f"Agent token budget exceeded: {source.tokens_consumed + tokens_used:.0f} > {source.token_budget:.0f}",
                    impedance=self._flow.compute_impedance().impedance,
                    flow_state=self._flow.flow_state,
                    tokens_consumed=source.tokens_consumed,
                    token_budget_remaining=source.token_budget_remaining,
                )

        # Check 1: Flow impedance (metabolic fuse)
        if not self._flow.can_delegate():
            self._delegations_blocked += 1
            impedance = self._flow.compute_impedance()
            # Determine if it's token budget or impedance that caused the block
            if self._flow.token_budget is not None and self._flow._total_tokens_consumed >= self._flow.token_budget:
                reason = f"SYSTEM TOKEN BUDGET EXHAUSTED: {self._flow._total_tokens_consumed:,.0f} / {self._flow.token_budget:,.0f} tokens consumed"
            else:
                reason = f"PRESERVATION mode: κ_effective={self._flow.kappa_effective:.3f} < {self._flow.preservation_threshold}"
            return DelegationVerdict(
                allowed=False,
                source_id=source_id,
                target_id=target_id,
                reason=reason,
                impedance=impedance.impedance,
                flow_state=FlowState.PRESERVATION,
                cycle_detected=False,
                velocity=impedance.velocity,
                tokens_consumed=source.tokens_consumed,
                token_budget_remaining=source.token_budget_remaining,
            )

        # Check 2: Depth limit
        new_depth = source.depth + 1
        if new_depth > self._depth_limit:
            self._delegations_blocked += 1
            return DelegationVerdict(
                allowed=False,
                source_id=source_id,
                target_id=target_id,
                reason=f"Depth limit exceeded: {new_depth} > {self._depth_limit}",
                impedance=self._flow.compute_impedance().impedance,
                flow_state=self._flow.flow_state,
                depth=new_depth,
            )

        # Check 3: Fanout limit
        current_fanout = len(source.children)
        if current_fanout >= self._fanout_limit:
            self._delegations_blocked += 1
            return DelegationVerdict(
                allowed=False,
                source_id=source_id,
                target_id=target_id,
                reason=f"Fanout limit exceeded: {current_fanout} >= {self._fanout_limit}",
                impedance=self._flow.compute_impedance().impedance,
                flow_state=self._flow.flow_state,
                depth=new_depth,
            )

        # Check 4: Cycle detection (Union-Find)
        is_new_agent = target_id not in self._agents
        if is_new_agent:
            # New agent — add to Union-Find
            self._uf.make_set(target_id)
        else:
            # Existing agent — check for cycle
            if self._uf.would_create_cycle(source_id, target_id):
                self._cycles_detected += 1
                self._delegations_blocked += 1
                return DelegationVerdict(
                    allowed=False,
                    source_id=source_id,
                    target_id=target_id,
                    reason=f"Cycle detected: delegation {source_id} → {target_id} would create circular dependency",
                    impedance=self._flow.compute_impedance().impedance,
                    flow_state=self._flow.flow_state,
                    depth=new_depth,
                    cycle_detected=True,
                )

        # All checks passed — execute delegation
        merged = self._uf.union(source_id, target_id)

        # Create or update target agent
        if is_new_agent:
            agent = AgentNode(
                id=target_id,
                model_id=attempt.model_id,
                parent_id=source_id,
                depth=new_depth,
                metadata=attempt.metadata,
                cost_per_1k_tokens=self._model_costs.get_cost(attempt.model_id),
            )
            self._agents[target_id] = agent
        else:
            # Update existing agent's parent (re-delegation)
            self._agents[target_id].parent_id = source_id
            self._agents[target_id].depth = new_depth

        # Update source's children
        if target_id not in source.children:
            source.children.append(target_id)

        # Record token consumption on the source agent
        if tokens_used > 0:
            source.tokens_consumed += tokens_used

        # Record in flow monitor
        self._total_delegations += 1
        self._flow.record_delegation(
            source_id=source_id,
            target_id=target_id,
            depth=new_depth,
            fan_out=len(source.children),
            tokens_used=tokens_used,
        )

        impedance = self._flow.compute_impedance()

        return DelegationVerdict(
            allowed=True,
            source_id=source_id,
            target_id=target_id,
            reason="Delegation approved",
            impedance=impedance.impedance,
            flow_state=impedance.flow_state,
            depth=new_depth,
            cycle_detected=False,
            velocity=impedance.velocity,
            tokens_consumed=source.tokens_consumed,
            token_budget_remaining=source.token_budget_remaining,
            cost_estimate=self._flow.estimated_cost,
        )

    def reset(self) -> None:
        """Reset all engine state."""
        self._uf.reset()
        self._flow.reset()
        self._agents.clear()
        self._total_delegations = 0
        self._cycles_detected = 0
        self._delegations_blocked = 0

    def record_tokens(self, agent_id: str, tokens: float) -> DelegationVerdict:
        """Record token consumption for an agent (outside of delegation).

        Use this to track token usage during task execution. If the agent
        exceeds its per-agent budget, future delegations will be blocked.

        Parameters
        ----------
        agent_id : str
            Agent that consumed tokens.
        tokens : float
            Number of tokens consumed.

        Returns
        -------
        DelegationVerdict
            Status including whether the agent is over budget.
        """
        if agent_id not in self._agents:
            return DelegationVerdict(
                allowed=False,
                source_id=agent_id,
                reason=f"Agent '{agent_id}' not found",
                flow_state=self._flow.flow_state,
            )

        agent = self._agents[agent_id]
        agent.tokens_consumed += tokens
        self._flow.record_tokens(agent_id, tokens)

        # Check per-agent budget
        over_agent_budget = (
            agent.token_budget is not None
            and agent.tokens_consumed > agent.token_budget
        )

        # Check global system budget (monthly cap)
        over_global_budget = (
            self._token_budget is not None
            and self._flow.total_tokens_consumed >= self._token_budget
        )

        over_budget = over_agent_budget or over_global_budget

        if over_global_budget:
            reason = f"SYSTEM BUDGET EXHAUSTED: {self._flow.total_tokens_consumed:,.0f} / {self._token_budget:,.0f} tokens"
        elif over_agent_budget:
            reason = "Token budget exceeded"
        else:
            reason = "Tokens recorded"

        return DelegationVerdict(
            allowed=not over_budget,
            source_id=agent_id,
            reason=reason,
            flow_state=self._flow.flow_state,
            tokens_consumed=agent.tokens_consumed,
            token_budget_remaining=agent.token_budget_remaining,
            cost_estimate=self._flow.estimated_cost,
        )

    def get_agent_token_usage(self, agent_id: str) -> dict:
        """Get token usage details for a specific agent.

        Parameters
        ----------
        agent_id : str
            Agent to query.

        Returns
        -------
        dict
            Token usage details including consumed, budget, remaining, cost.
            V2: cost is model-aware (based on agent's model_id).
        """
        if agent_id not in self._agents:
            return {"error": f"Agent '{agent_id}' not found"}

        agent = self._agents[agent_id]
        return {
            "agent_id": agent_id,
            "model_id": agent.model_id,
            "cost_per_1k_tokens": agent.cost_per_1k_tokens,
            "tokens_consumed": agent.tokens_consumed,
            "token_budget": agent.token_budget,
            "token_budget_remaining": agent.token_budget_remaining,
            "budget_ratio": agent.token_budget_ratio,
            "estimated_cost": agent.estimated_cost,
            "over_budget": (
                agent.token_budget is not None
                and agent.tokens_consumed > agent.token_budget
            ),
        }

    # -----------------------------------------------------------------------
    # V2: Model-Aware Cost Intelligence
    # -----------------------------------------------------------------------

    def get_cost_breakdown(self) -> dict:
        """Get model-aware cost breakdown across all agents.

        Returns a per-agent and per-model summary showing how much each
        agent costs based on its model_id rate, not a flat system rate.

        Returns
        -------
        dict
            Cost breakdown with per-agent details, per-model totals,
            and system-wide total cost.
        """
        per_agent = []
        per_model: dict[str, dict] = {}

        for agent in self._agents.values():
            agent_cost = agent.estimated_cost
            per_agent.append({
                "agent_id": agent.id,
                "model_id": agent.model_id,
                "cost_per_1k_tokens": agent.cost_per_1k_tokens,
                "tokens_consumed": agent.tokens_consumed,
                "estimated_cost": round(agent_cost, 6),
            })

            # Aggregate by model
            if agent.model_id not in per_model:
                per_model[agent.model_id] = {
                    "model_id": agent.model_id,
                    "cost_per_1k_tokens": agent.cost_per_1k_tokens,
                    "agent_count": 0,
                    "total_tokens": 0.0,
                    "total_cost": 0.0,
                }
            per_model[agent.model_id]["agent_count"] += 1
            per_model[agent.model_id]["total_tokens"] += agent.tokens_consumed
            per_model[agent.model_id]["total_cost"] += agent_cost

        # Round model totals
        for m in per_model.values():
            m["total_cost"] = round(m["total_cost"], 6)

        total_cost = sum(a["estimated_cost"] for a in per_agent)

        return {
            "total_cost": round(total_cost, 6),
            "total_tokens": self._flow.total_tokens_consumed,
            "per_agent": sorted(per_agent, key=lambda x: x["estimated_cost"], reverse=True),
            "per_model": sorted(per_model.values(), key=lambda x: x["total_cost"], reverse=True),
        }

    def suggest_downgrades(self) -> list[dict]:
        """Suggest cost-saving model downgrades for active agents.

        For each agent using an expensive model, suggests cheaper alternatives
        and calculates potential savings based on tokens already consumed.

        This is the "surgical decision" feature:
        "This task would cost $2.40 on gpt-4o but $0.12 on llama-3 — want to downgrade?"

        Returns
        -------
        list[dict]
            Downgrade suggestions sorted by potential savings (highest first).
        """
        suggestions = []

        for agent in self._agents.values():
            if agent.tokens_consumed <= 0:
                continue

            alternatives = self._model_costs.suggest_cheaper_models(agent.model_id)
            if not alternatives:
                continue

            current_cost = agent.estimated_cost
            for alt in alternatives:
                alt_cost = agent.tokens_consumed * alt["cost_per_1k"] / 1000.0
                savings = current_cost - alt_cost

                if savings > 0:
                    suggestions.append({
                        "agent_id": agent.id,
                        "current_model": agent.model_id,
                        "current_cost": round(current_cost, 6),
                        "suggested_model": alt["model_id"],
                        "suggested_cost": round(alt_cost, 6),
                        "savings": round(savings, 6),
                        "savings_percent": alt["savings_percent"],
                        "tokens_consumed": agent.tokens_consumed,
                    })

        # Sort by savings descending — biggest wins first
        suggestions.sort(key=lambda x: x["savings"], reverse=True)
        return suggestions

    def get_model_cost_registry(self) -> ModelCostRegistry:
        """Access the model cost registry for custom configuration.

        Returns
        -------
        ModelCostRegistry
            The active cost registry instance.
        """
        return self._model_costs

    # -----------------------------------------------------------------------
    # SLCF Integration (Req 13)
    # -----------------------------------------------------------------------

    def load_from_slcf(self, data: bytes) -> None:
        """Initialize engine state from SLCF binary data.

        Performs footer-first validation, then:
        1. Rejects if Σδ > 2 for any page group
        2. Sets metabolic fuse = footer κ_effective
        3. Seeds Union-Find with β₁ cycle hints (if present)
        4. If β₁ absent, computes from agent graph on first traversal

        Parameters
        ----------
        data : bytes
            Complete SLCF binary data.

        Raises
        ------
        SLCFComplianceError
            If Σδ > 2 for any page group.
        SLCFIntegrityError
            If Maat hash validation fails.
        SLCFValidationError
            If κ_eff is out of range.
        SLCFStructureError
            If L3 page index is missing.
        """
        from cascade_guard.slcf.reader import SLCFReader

        reader = SLCFReader()
        slcf_file = reader.open(data)

        # Use footer κ_effective as initial metabolic fuse
        self._kappa_effective = slcf_file.kappa_effective

        # Seed Union-Find with β₁ cycle hints
        if slcf_file.beta_one > 0:
            self._uf._cycle_hints = slcf_file.beta_one
        else:
            # Will compute from agent graph on first traversal
            self._uf._cycle_hints = 0

    def track_glyph_compression(self, glyph_bytes: int) -> None:
        """Deduct glyph token equivalent from active token budget.

        Conversion: tokens = glyph_bytes / 4 (4 bytes per token).

        Parameters
        ----------
        glyph_bytes : int
            Size of compressed glyph output in bytes.
        """
        token_equivalent = glyph_bytes / 4.0
        # Deduct from system-wide token tracking via flow monitor
        self._flow.record_tokens("__glyph_compression__", token_equivalent)

    def store_decision_log(self, verdict: DelegationVerdict) -> dict:
        """Create an SLCF L5 governance decision log entry.

        Parameters
        ----------
        verdict : DelegationVerdict
            The delegation verdict to log.

        Returns
        -------
        dict
            Decision log entry with all required fields.
        """
        import time

        from cascade_guard.maat.validator import MaatValidator, ValidationContext

        # Compute Maat score for this decision
        validator = MaatValidator()
        ctx = ValidationContext(
            sigma_delta=0.0,
            kappa_effective=getattr(self, "_kappa_effective", 1.0),
            agent_count=self.num_agents,
            max_depth=self._status.max_depth if hasattr(self, "_status") else 0,
            resource_id=f"decision:{verdict.source_id}",
        )
        maat_result = validator.evaluate("decision_log", ctx)

        return {
            "timestamp": time.time(),
            "source_agent": verdict.source_id,
            "target_agent": verdict.target_id,
            "verdict": "allowed" if verdict.allowed else "blocked",
            "cycle_detected": verdict.cycle_detected,
            "flow_state": verdict.flow_state.value,
            "kappa_effective": getattr(self, "_kappa_effective", 1.0),
            "maat_validation_score": maat_result.overall_score,
        }
