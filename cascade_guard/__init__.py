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
    DEFAULT_MODEL_COSTS,
    DelegationAttempt,
    DelegationVerdict,
    FlowState,
    ImpedanceReport,
    ModelCostRegistry,
)
from .union_find import UnionFind
from .flow_monitor import FlowMonitor

__all__ = [
    "CascadeEngine",
    "UnionFind",
    "FlowMonitor",
    "AgentNode",
    "CascadeStatus",
    "DEFAULT_MODEL_COSTS",
    "DelegationAttempt",
    "DelegationVerdict",
    "FlowState",
    "ImpedanceReport",
    "ModelCostRegistry",
]

__version__ = "1.3.0"
