# CascadeGuard

**Cascade Detection and Prevention for Multi-Agent Delegation Chains**

When autonomous agents delegate tasks to sub-agents at runtime, circular dependencies and exponential spawning can cascade into system failure. CascadeGuard prevents this with O(α(N)) cycle detection and distribution-based flow control.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    CascadeEngine                         │
│                                                         │
│  ┌──────────────┐    ┌──────────────────────────────┐  │
│  │  Union-Find  │    │       FlowMonitor            │  │
│  │              │    │                              │  │
│  │ • Cycle det. │    │ • Velocity (delegations/s)   │  │
│  │ • O(α(N))    │    │ • Depth distribution         │  │
│  │ • Components │    │ • Fanout distribution        │  │
│  │              │    │ • Concentration (Gini)       │  │
│  └──────────────┘    │ • κ_effective computation    │  │
│                      └──────────────────────────────┘  │
│                                                         │
│  Checks (in order):                                     │
│  1. Flow impedance (metabolic fuse)                     │
│  2. Depth limit                                         │
│  3. Fanout limit                                        │
│  4. Cycle detection (Union-Find)                        │
└─────────────────────────────────────────────────────────┘
```

## Key Properties

| Property | Value |
|----------|-------|
| Cycle detection | O(α(N)) — effectively constant |
| No embeddings | Uses distribution stats, not vectors |
| No pairwise computation | Impedance from flow metadata |
| Memory | O(N) where N = number of agents |
| Dependencies | Only `pydantic` + `rich` (for CLI) |

## Installation

```bash
pip install -e .
```

## Usage

### CLI Demo

```bash
cascade-guard-demo
```

### Programmatic

```python
from cascade_guard import CascadeEngine, DelegationAction

engine = CascadeEngine(
    max_velocity=50.0,      # delegations/sec before full impedance
    depth_limit=10,         # max chain depth
    fanout_limit=20,        # max children per agent
    preservation_threshold=0.3,  # κ below this blocks all
)

# Register root agent
engine.register_agent("orchestrator", model_id="gpt-4o")

# Spawn sub-agent (checks cycle + flow)
result = engine.register_agent("planner", model_id="claude-3", parent_id="orchestrator")
assert result.allowed

# Attempt delegation between existing agents
result = engine.attempt_delegation("planner", "orchestrator", DelegationAction.DELEGATE)
assert not result.allowed  # Cycle detected!
assert result.cycle_detected

# Check system health
status = engine.get_status()
print(f"κ_effective: {status.kappa_effective}")
print(f"Flow state: {status.flow_state}")
```

## Impedance Formula

```
Z = w_v * velocity_ratio + w_d * depth_ratio + w_f * fanout_ratio + w_c * concentration

κ_effective = κ_duat * (1 - Z)

When κ_effective < 0.3 → PRESERVATION mode (block all delegations)
```

Components:
- **velocity_ratio**: current delegations/sec ÷ max allowed
- **depth_ratio**: deepest chain ÷ depth limit
- **fanout_ratio**: widest parent ÷ fanout limit
- **concentration**: Gini coefficient of delegation counts per source

## Flow States

| State | κ_effective | Behavior |
|-------|-------------|----------|
| NOMINAL | ≥ 0.8 | All delegations allowed |
| ELEVATED | 0.5 – 0.8 | Allowed, monitoring active |
| THROTTLED | 0.3 – 0.5 | Allowed with warnings |
| PRESERVATION | < 0.3 | All delegations blocked |

## Relationship to ACAP

CascadeGuard is the **front door** — it prevents cascading delegation failures before they reach the ACAP topological authorization engine. Together:

- **CascadeGuard**: Fast cycle detection + flow control (O(α(N)))
- **ACAP**: Deep authorization gap detection via simplicial homology (O(N²) worst case)

CascadeGuard handles the common case (cycle + overload) in constant time. ACAP handles the complex case (emergent super-permissions, authorization gaps) with full topological analysis.

## Tests

```bash
pytest tests/ -v
```

## License

Apache-2.0
