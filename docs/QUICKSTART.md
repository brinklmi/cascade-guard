# Technical Quick-Start Guide

Integrate CascadeGuard as an inline safety layer in your agent middleware in under 5 minutes.

---

## Installation

```bash
pip install -e .
```

No external dependencies beyond `pydantic`. No LLM required. No API keys.

---

## Core Integration Pattern

CascadeGuard sits between your agent orchestrator and the execution layer. Every delegation request passes through CascadeGuard before reaching the target:

```python
from cascade_guard import CascadeEngine
from cascade_guard.models import DelegationAction

# Initialize once at application startup
engine = CascadeEngine(
    max_velocity=50.0,            # Max delegations per second
    depth_limit=10,               # Max chain depth
    fanout_limit=20,              # Max children per agent
    preservation_threshold=0.3,   # κ below this = full stop
    dollar_budget=500.0,          # Monthly spend cap ($)
    cost_per_1k_tokens=0.03,      # Your model's pricing
)
```

---

## Middleware Integration (Request Pipeline)

Drop CascadeGuard into your middleware router. This pattern works with any framework (LangGraph, CrewAI, AutoGen, custom):

```python
def handle_delegation_request(source_id: str, target_id: str, task: dict) -> dict:
    """Middleware handler — called before every agent-to-agent delegation."""
    
    # 1. Ask CascadeGuard: is this delegation safe?
    verdict = engine.attempt_delegation(
        source_id, 
        target_id, 
        DelegationAction.DELEGATE,
        tokens_used=task.get("estimated_tokens", 0),
    )
    
    # 2. If blocked, return safe fallback (zero cost)
    if not verdict.allowed:
        return {
            "status": "blocked",
            "reason": verdict.reason,
            "cycle_detected": verdict.cycle_detected,
            "kappa_effective": engine.get_status().kappa_effective,
        }
    
    # 3. If allowed, forward to execution
    result = execute_task(target_id, task)
    
    # 4. Record actual token consumption
    engine.record_tokens(target_id, result["tokens_used"])
    
    return {"status": "completed", "result": result}
```

**Latency overhead:** <250μs per check. Zero network calls. Zero LLM inference.

---

## Agent Registration

Register agents at spawn time. CascadeGuard tracks the full delegation tree:

```python
# Root agent (no parent)
engine.register_agent("orchestrator", model_id="gpt-4o")

# Child agents (with budget limits)
engine.register_agent(
    "research_agent", 
    model_id="gpt-4o", 
    parent_id="orchestrator",
    token_budget=100000,  # This agent's individual cap
)

engine.register_agent(
    "code_writer", 
    model_id="claude-3.5", 
    parent_id="orchestrator",
    token_budget=500000,
)
```

---

## Real-Time Health Monitoring

Query system health at any time without impacting performance:

```python
status = engine.get_status()

print(f"κ_effective: {status.kappa_effective:.3f}")
print(f"Flow state:  {status.flow_state.value}")
print(f"Tokens used: {status.total_tokens_consumed:,.0f}")
print(f"Budget util: {status.token_budget_utilization:.1%}")
print(f"Cycles caught: {status.cycles_detected}")
print(f"Blocked:     {status.delegations_blocked}")
```

---

## Flow State Responses

Use κ_effective to drive graduated responses in your application:

```python
status = engine.get_status()
kappa = status.kappa_effective

if kappa >= 0.8:
    # NOMINAL — proceed at full speed
    pass
elif kappa >= 0.5:
    # ELEVATED — reduce parallelism
    max_concurrent_agents = max(1, max_concurrent_agents // 2)
elif kappa >= 0.3:
    # THROTTLED — critical path only
    if not task.get("critical"):
        return defer_task(task)
else:
    # PRESERVATION — circuit breaker tripped
    return emergency_fallback(task)
```

---

## Framework-Specific Examples

### LangGraph

```python
from langgraph.graph import StateGraph
from cascade_guard import CascadeEngine

engine = CascadeEngine(max_velocity=50.0, depth_limit=10, fanout_limit=20)

def guarded_node(state):
    """Wrap any LangGraph node with CascadeGuard."""
    verdict = engine.attempt_delegation(
        state["current_agent"], 
        state["next_agent"],
        DelegationAction.DELEGATE,
    )
    if not verdict.allowed:
        return {"blocked": True, "reason": verdict.reason}
    return run_agent(state)
```

### FastAPI Middleware

```python
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
engine = CascadeEngine(dollar_budget=1000.0, cost_per_1k_tokens=0.03)

@app.middleware("http")
async def cascade_guard_middleware(request: Request, call_next):
    source = request.headers.get("X-Agent-Source")
    target = request.headers.get("X-Agent-Target")
    
    if source and target:
        verdict = engine.attempt_delegation(source, target, DelegationAction.DELEGATE)
        if not verdict.allowed:
            raise HTTPException(429, detail=verdict.reason)
    
    return await call_next(request)
```

---

## What CascadeGuard Does NOT Do

- Does not read or analyze agent prompts or responses
- Does not add tokens to context windows
- Does not require network connectivity
- Does not depend on any LLM or ML model
- Does not introduce non-deterministic behavior

It operates exclusively on **structural metadata** (who delegates to whom, how deep, how fast) — making it immune to prompt injection, context poisoning, and semantic bypass attacks.

---

## Next Steps

- [USE_CASES.md](USE_CASES.md) — Real-world applications (wind turbines, drones, vehicles)
- [THEORY.md](../THEORY.md) — Mathematical foundations and design philosophy
- [IMPEDANCE_FORMULAS.md](IMPEDANCE_FORMULAS.md) — Exact formulas for κ_effective computation
- `streamlit run demo/app.py` — Interactive visual dashboard
