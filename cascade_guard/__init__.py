"""CascadeGuard — Cascade Detection and Prevention for Multi-Agent Delegation Chains.

Uses Union-Find for O(α(N)) cycle detection and distribution-based impedance
for flow control. Prevents cascading delegation failures before they propagate.

Architecture:
- UnionFind: Disjoint-set forest for cycle detection in delegation graphs
- FlowMonitor: Distribution-based impedance from delegation velocity stats
- CascadeEngine: Orchestrates detection + prevention + flow control
- Models: Pydantic domain models for agents, delegations, and flow state
"""

from .engine import CascadeEngine
from .models import (
    AgentNode,
    CascadeStatus,
    DelegationAttempt,
    DelegationVerdict,
    FlowState,
    ImpedanceReport,
)
from .union_find import UnionFind
from .flow_monitor import FlowMonitor

__all__ = [
    "CascadeEngine",
    "UnionFind",
    "FlowMonitor",
    "AgentNode",
    "CascadeStatus",
    "DelegationAttempt",
    "DelegationVerdict",
    "FlowState",
    "ImpedanceReport",
]

__version__ = "1.0.0"
