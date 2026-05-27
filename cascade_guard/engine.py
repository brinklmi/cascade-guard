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
    ):
        self._uf = UnionFind()
        self._flow = FlowMonitor(
            max_velocity=max_velocity,
            depth_limit=depth_limit,
            fanout_limit=fanout_limit,
            window_seconds=window_seconds,
            preservation_threshold=preservation_threshold,
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
        return self._execute_delegation(attempt)

    def attempt_delegation(
        self,
        source_id: str,
        target_id: str,
        action: DelegationAction = DelegationAction.DELEGATE,
        model_id: str = "unknown",
        metadata: Optional[dict] = None,
    ) -> DelegationVerdict:
        """Attempt a delegation between two existing agents.

        Checks:
        1. Cycle detection (Union-Find)
        2. Flow impedance (velocity, depth, fanout, concentration)
        3. Depth limit
        4. Fanout limit

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
        return self._execute_delegation(attempt)

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

    def _execute_delegation(self, attempt: DelegationAttempt) -> DelegationVerdict:
        """Execute a delegation attempt with all safety checks.

        Parameters
        ----------
        attempt : DelegationAttempt
            The delegation to evaluate.

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

        # Check 1: Flow impedance (metabolic fuse)
        if not self._flow.can_delegate():
            self._delegations_blocked += 1
            impedance = self._flow.compute_impedance()
            return DelegationVerdict(
                allowed=False,
                source_id=source_id,
                target_id=target_id,
                reason=f"PRESERVATION mode: κ_effective={self._flow.kappa_effective:.3f} < {self._flow.preservation_threshold}",
                impedance=impedance.impedance,
                flow_state=FlowState.PRESERVATION,
                cycle_detected=False,
                velocity=impedance.velocity,
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
            )
            self._agents[target_id] = agent
        else:
            # Update existing agent's parent (re-delegation)
            self._agents[target_id].parent_id = source_id
            self._agents[target_id].depth = new_depth

        # Update source's children
        if target_id not in source.children:
            source.children.append(target_id)

        # Record in flow monitor
        self._total_delegations += 1
        self._flow.record_delegation(
            source_id=source_id,
            target_id=target_id,
            depth=new_depth,
            fan_out=len(source.children),
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
        )

    def reset(self) -> None:
        """Reset all engine state."""
        self._uf.reset()
        self._flow.reset()
        self._agents.clear()
        self._total_delegations = 0
        self._cycles_detected = 0
        self._delegations_blocked = 0
