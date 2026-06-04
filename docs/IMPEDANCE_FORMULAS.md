# Impedance Formulas: The Mathematics of κ_effective

This document specifies the exact mathematical formulas used by CascadeGuard to compute system health. These are not approximations — they are the literal code paths executed on every delegation check.

---

## The Master Equation

CascadeGuard computes a continuous health coefficient called **Effective Impedance** (κ_effective):

```
κ_effective = κ_duat × (1 - Z)
```

Where:
- **κ_duat** = Base metabolic constant (default: 1.0). Represents the system's maximum theoretical throughput.
- **Z** = Weighted impedance score ∈ [0.0, 1.0]. Increases as the system degrades.

When Z = 0, the system is perfectly healthy (κ = κ_duat). When Z = 1, the system is fully saturated (κ = 0).

---

## Impedance Decomposition (Z)

Z is a weighted sum of five orthogonal degradation dimensions:

```
Z = w_v · V + w_d · D + w_f · F + w_c · C + w_t · T
```

**Default weights:**

| Weight | Symbol | Value | Dimension |
|--------|--------|-------|-----------|
| w_v | Velocity | 0.35 | Temporal cascade pressure |
| w_d | Depth | 0.15 | Structural chain growth |
| w_f | Fanout | 0.15 | Explosive spawn rate |
| w_c | Concentration | 0.15 | Asymmetric traffic density |
| w_t | Token pressure | 0.20 | Budget consumption rate |

Weights sum to 1.0. They are configurable at engine initialization.

---

## Dimension 1: Velocity Ratio (V)

**What it measures:** How fast delegations are occurring relative to the system's rated capacity.

```
V = min(1.0, delegations_in_window / max_velocity)
```

- `delegations_in_window`: Count of delegation events in the rolling time window (default: 60 seconds)
- `max_velocity`: Maximum rated delegations per second (configurable, default: 50)

**Interpretation:**
- V = 0.0 → System is idle
- V = 0.5 → System at 50% temporal capacity
- V = 1.0 → System at or above rated velocity (temporal saturation)

**Failure mode caught:** Runaway recursive loops that generate delegations faster than the system can process them.

---

## Dimension 2: Depth Ratio (D)

**What it measures:** How deep the longest delegation chain has grown relative to the structural limit.

```
D = min(1.0, max_observed_depth / depth_limit)
```

- `max_observed_depth`: Deepest chain in the current delegation graph
- `depth_limit`: Maximum allowed depth (configurable, default: 10)

**Interpretation:**
- D = 0.0 → All agents are at root level
- D = 0.5 → Deepest chain is half the structural limit
- D = 1.0 → A chain has reached maximum depth (next delegation would be blocked)

**Failure mode caught:** Unbounded chain growth (A→B→C→D→...→Z) that exceeds audit/accountability limits.

---

## Dimension 3: Fanout Ratio (F)

**What it measures:** The maximum number of children any single agent has spawned, relative to the fanout limit.

```
F = min(1.0, max_children_count / fanout_limit)
```

- `max_children_count`: Maximum children across all agents: `max(children_count[agent] for agent in agents)`
- `fanout_limit`: Maximum allowed children per agent (configurable, default: 20)

**Interpretation:**
- F = 0.0 → No agent has spawned any children
- F = 0.5 → Most prolific agent is at 50% of spawn capacity
- F = 1.0 → An agent has reached its spawn limit (next spawn would be blocked)

**Failure mode caught:** Explosive fanout (one agent spawning hundreds of sub-agents simultaneously).

---

## Dimension 4: Concentration (C) — Gini Coefficient

**What it measures:** How unevenly delegation traffic is distributed across agents. High concentration means one agent is handling disproportionate load.

```
C = Gini coefficient of delegation counts per agent
```

The Gini coefficient is computed as:

```
Given n agents with delegation counts x₁, x₂, ..., xₙ (sorted ascending):

C = (2 · Σᵢ (i · xᵢ)) / (n · Σᵢ xᵢ) - (n + 1) / n
```

**Interpretation:**
- C = 0.0 → Perfect equality (all agents handle equal traffic)
- C = 0.5 → Moderate inequality (some agents are busier than others)
- C = 1.0 → Perfect inequality (one agent handles ALL traffic)

**Failure mode caught:** Asymmetric cascades where a single bottleneck agent becomes a single point of failure.

---

## Dimension 5: Token Pressure (T)

**What it measures:** How much of the global token budget has been consumed.

```
T = min(1.0, total_tokens_consumed / global_token_budget)
```

- `total_tokens_consumed`: Sum of all tokens consumed across all agents
- `global_token_budget`: System-wide token cap (derived from dollar_budget / cost_per_1k_tokens × 1000)

If no global budget is configured, T = 0.0 (no pressure).

**Interpretation:**
- T = 0.0 → No tokens consumed (or no budget configured)
- T = 0.5 → Half of monthly budget consumed
- T = 1.0 → Budget exhausted (system halts all delegation)

**Failure mode caught:** Runaway API spend from rogue agents or misconfigured loops.

---

## Flow State Classification

The computed κ_effective maps to discrete flow states:

```
κ ≥ 0.8  →  NOMINAL       (Green)   — Full-speed operation
κ ≥ 0.5  →  ELEVATED      (Yellow)  — Increased monitoring
κ ≥ 0.3  →  THROTTLED     (Orange)  — Rate-limited, critical path only
κ < 0.3  →  PRESERVATION  (Red)     — Circuit breaker tripped, system halt
```

The preservation threshold (default 0.3) is the point where the system enters full protection mode — no new delegations are permitted until impedance decreases.

---

## Worked Example

**Scenario:** 5 agents, 30 delegations in the last minute, deepest chain is 6 levels, one agent has spawned 12 children, traffic is moderately concentrated, 60% of budget consumed.

```
V = min(1.0, 30/50) = 0.60
D = min(1.0, 6/10)  = 0.60
F = min(1.0, 12/20) = 0.60
C = 0.45  (computed Gini)
T = 0.60

Z = 0.35(0.60) + 0.15(0.60) + 0.15(0.60) + 0.15(0.45) + 0.20(0.60)
  = 0.210 + 0.090 + 0.090 + 0.0675 + 0.120
  = 0.5775

κ_effective = 1.0 × (1 - 0.5775) = 0.4225

Flow state: THROTTLED (0.3 ≤ 0.4225 < 0.5)
```

**Response:** System enters throttled mode. Non-critical delegations are deferred. Rate limits are enforced. The system self-heals as velocity decreases and impedance drops back below the threshold.

---

## Properties

1. **Monotonic degradation:** Z can only increase as any dimension worsens. κ can only decrease.
2. **Independent recovery:** Each dimension recovers independently. Velocity drops when delegations slow. Depth drops when chains complete. Token pressure only recovers at budget reset.
3. **Bounded output:** κ ∈ [0.0, κ_duat]. Z ∈ [0.0, 1.0]. No unbounded values.
4. **Deterministic:** Same inputs always produce same outputs. No randomness, no ML inference, no non-deterministic behavior.
5. **Composable:** Multiple CascadeGuard instances can monitor nested subsystems. Each computes its own κ independently.

---

## Configuration Reference

```python
CascadeEngine(
    max_velocity=50.0,                              # V denominator
    depth_limit=10,                                 # D denominator
    fanout_limit=20,                                # F denominator
    preservation_threshold=0.3,                     # κ halt threshold
    dollar_budget=500.0,                            # T denominator (converted to tokens)
    cost_per_1k_tokens=0.03,                        # Dollar-to-token conversion
    weights=(0.35, 0.15, 0.15, 0.15, 0.20),        # (w_v, w_d, w_f, w_c, w_t)
)
```
