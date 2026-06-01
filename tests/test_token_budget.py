"""Tests for CascadeGuard Token Budget Feature.

Validates:
- Per-agent token budget enforcement
- System-wide token budget pressure on impedance
- Token recording and cost attribution
- Budget exhaustion triggers delegation block
- Backward compatibility (no budget = unlimited)
"""

from cascade_guard.engine import CascadeEngine
from cascade_guard.models import DelegationAction, FlowState


class TestPerAgentTokenBudget:
    """Per-agent token budget enforcement."""

    def test_agent_with_budget_allows_within_limit(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="gpt-4o", token_budget=10000)

        # Spawn a new child with tokens within budget
        r = engine.register_agent("child", model_id="gpt-4o", parent_id="root")
        assert r.allowed is True

        # Delegation to a new agent with tokens
        r = engine.attempt_delegation(
            "root", "new-agent", DelegationAction.SPAWN, tokens_used=5000
        )
        assert r.allowed is True
        assert r.tokens_consumed == 5000
        assert r.token_budget_remaining == 5000

    def test_agent_budget_exceeded_blocks_delegation(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="gpt-4o", token_budget=10000)

        # First delegation uses most of budget
        r1 = engine.attempt_delegation(
            "root", "child-1", DelegationAction.SPAWN, tokens_used=8000
        )
        assert r1.allowed is True

        # Second delegation would exceed budget
        r2 = engine.attempt_delegation(
            "root", "child-2", DelegationAction.SPAWN, tokens_used=5000
        )
        assert r2.allowed is False
        assert "budget exceeded" in r2.reason.lower()

    def test_agent_without_budget_unlimited(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="gpt-4o")  # No token_budget

        # Large token usage should still be allowed
        r = engine.attempt_delegation(
            "root", "child-1", DelegationAction.SPAWN, tokens_used=1000000
        )
        assert r.allowed is True
        assert r.token_budget_remaining is None  # Unlimited

    def test_zero_tokens_always_allowed(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="gpt-4o", token_budget=100)

        # Use up the budget
        engine.attempt_delegation(
            "root", "child-1", DelegationAction.SPAWN, tokens_used=100
        )

        # Zero-token delegation should still pass (budget check only fires when tokens_used > 0)
        r = engine.attempt_delegation(
            "root", "child-2", DelegationAction.SPAWN, tokens_used=0
        )
        assert r.allowed is True


class TestRecordTokens:
    """Token recording outside of delegation."""

    def test_record_tokens_updates_agent(self):
        engine = CascadeEngine()
        engine.register_agent("agent-1", model_id="gpt-4o", token_budget=50000)

        r = engine.record_tokens("agent-1", 12000)
        assert r.allowed is True
        assert r.tokens_consumed == 12000
        assert r.token_budget_remaining == 38000

    def test_record_tokens_over_budget_flags(self):
        engine = CascadeEngine()
        engine.register_agent("agent-1", model_id="gpt-4o", token_budget=10000)

        r = engine.record_tokens("agent-1", 15000)
        assert r.allowed is False
        assert r.tokens_consumed == 15000
        assert r.token_budget_remaining == 0.0

    def test_record_tokens_missing_agent(self):
        engine = CascadeEngine()
        r = engine.record_tokens("nonexistent", 5000)
        assert r.allowed is False
        assert "not found" in r.reason

    def test_record_tokens_cumulative(self):
        engine = CascadeEngine()
        engine.register_agent("agent-1", model_id="gpt-4o", token_budget=30000)

        engine.record_tokens("agent-1", 10000)
        engine.record_tokens("agent-1", 10000)
        r = engine.record_tokens("agent-1", 10000)

        assert r.allowed is True
        assert r.tokens_consumed == 30000
        assert r.token_budget_remaining == 0.0


class TestSystemTokenBudget:
    """System-wide token budget and impedance pressure."""

    def test_system_budget_increases_impedance(self):
        engine = CascadeEngine(token_budget=100000)
        engine.register_agent("root", model_id="gpt-4o")
        engine.register_agent("child", model_id="gpt-4o", parent_id="root")

        # Record significant token usage
        engine.record_tokens("root", 80000)

        status = engine.get_status()
        assert status.total_tokens_consumed == 80000
        assert status.total_token_budget == 100000
        assert status.token_budget_utilization == 0.8

    def test_no_system_budget_no_pressure(self):
        engine = CascadeEngine()  # No token_budget
        engine.register_agent("root", model_id="gpt-4o")
        engine.register_agent("child", model_id="gpt-4o", parent_id="root")

        engine.record_tokens("root", 1000000)

        status = engine.get_status()
        assert status.total_tokens_consumed == 1000000
        assert status.total_token_budget is None
        assert status.token_budget_utilization == 0.0

    def test_token_pressure_contributes_to_impedance(self):
        # With budget, high usage should increase impedance
        engine_budgeted = CascadeEngine(token_budget=10000)
        engine_budgeted.register_agent("root", model_id="gpt-4o")
        engine_budgeted.register_agent("child", model_id="gpt-4o", parent_id="root")
        engine_budgeted.record_tokens("root", 9000)  # 90% of budget

        # Without budget, same usage should have lower impedance
        engine_unlimited = CascadeEngine()
        engine_unlimited.register_agent("root", model_id="gpt-4o")
        engine_unlimited.register_agent("child", model_id="gpt-4o", parent_id="root")
        engine_unlimited.record_tokens("root", 9000)

        status_budgeted = engine_budgeted.get_status()
        status_unlimited = engine_unlimited.get_status()

        # Budgeted engine should have higher impedance (lower kappa)
        assert status_budgeted.kappa_effective < status_unlimited.kappa_effective


class TestCostAttribution:
    """Cost estimation and attribution."""

    def test_cost_estimate_in_verdict(self):
        engine = CascadeEngine(cost_per_1k_tokens=0.03)
        engine.register_agent("root", model_id="gpt-4o")

        r = engine.attempt_delegation(
            "root", "child-1", DelegationAction.SPAWN, tokens_used=10000
        )
        assert r.allowed is True
        # 10000 tokens * $0.03/1K = $0.30
        assert abs(r.cost_estimate - 0.30) < 0.01

    def test_agent_token_usage_report(self):
        engine = CascadeEngine(cost_per_1k_tokens=0.06)
        engine.register_agent("agent-1", model_id="gpt-4o", token_budget=50000)

        engine.record_tokens("agent-1", 25000)

        usage = engine.get_agent_token_usage("agent-1")
        assert usage["tokens_consumed"] == 25000
        assert usage["token_budget"] == 50000
        assert usage["token_budget_remaining"] == 25000
        assert usage["budget_ratio"] == 0.5
        # 25000 * 0.06 / 1000 = $1.50
        assert abs(usage["estimated_cost"] - 1.50) < 0.01
        assert usage["over_budget"] is False

    def test_agent_token_usage_missing_agent(self):
        engine = CascadeEngine()
        usage = engine.get_agent_token_usage("nonexistent")
        assert "error" in usage


class TestBackwardCompatibility:
    """Ensure existing behavior is unchanged when no token budget is set."""

    def test_default_engine_no_token_fields(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m")

        r = engine.attempt_delegation("A", "B", DelegationAction.SPAWN)
        assert r.allowed is True
        assert r.tokens_consumed == 0.0
        assert r.token_budget_remaining is None

    def test_status_defaults(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m")

        status = engine.get_status()
        assert status.total_tokens_consumed == 0.0
        assert status.total_token_budget is None
        assert status.token_budget_utilization == 0.0
