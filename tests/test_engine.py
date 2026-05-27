"""Tests for CascadeGuard Engine."""

from cascade_guard.engine import CascadeEngine
from cascade_guard.models import DelegationAction, FlowState


class TestEngineRegistration:
    """Agent registration tests."""

    def test_register_root_agent(self):
        engine = CascadeEngine()
        r = engine.register_agent("root-1", model_id="gpt-4o")
        assert r.allowed is True
        assert r.target_id == "root-1"
        assert r.depth == 0
        assert engine.num_agents == 1

    def test_register_child_agent(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="gpt-4o")
        r = engine.register_agent("child", model_id="claude-3", parent_id="root")
        assert r.allowed is True
        assert r.depth == 1

    def test_register_duplicate_rejected(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="gpt-4o")
        r = engine.register_agent("root", model_id="gpt-4o")
        assert r.allowed is False
        assert "already registered" in r.reason

    def test_register_with_missing_parent_rejected(self):
        engine = CascadeEngine()
        r = engine.register_agent("child", model_id="gpt-4o", parent_id="nonexistent")
        assert r.allowed is False
        assert "not found" in r.reason


class TestCycleDetection:
    """Cycle detection in delegation chains."""

    def test_cycle_detected_back_to_root(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m1")
        engine.register_agent("B", model_id="m2", parent_id="A")
        engine.register_agent("C", model_id="m3", parent_id="B")

        # C tries to delegate back to A
        r = engine.attempt_delegation("C", "A", DelegationAction.DELEGATE)
        assert r.allowed is False
        assert r.cycle_detected is True

    def test_cycle_detected_to_parent(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m1")
        engine.register_agent("B", model_id="m2", parent_id="A")

        # B tries to delegate to A
        r = engine.attempt_delegation("B", "A", DelegationAction.DELEGATE)
        assert r.allowed is False
        assert r.cycle_detected is True

    def test_no_false_positive_across_trees(self):
        engine = CascadeEngine()
        engine.register_agent("root-1", model_id="m1")
        engine.register_agent("child-1", model_id="m2", parent_id="root-1")

        engine.register_agent("root-2", model_id="m3")
        engine.register_agent("child-2", model_id="m4", parent_id="root-2")

        # Cross-tree delegation should be allowed (merges trees)
        r = engine.attempt_delegation("child-1", "child-2", DelegationAction.DELEGATE)
        # This connects two separate trees — no cycle
        assert r.allowed is True
        assert r.cycle_detected is False


class TestDepthLimit:
    """Depth limit enforcement."""

    def test_depth_limit_blocks_deep_chains(self):
        engine = CascadeEngine(depth_limit=3)
        engine.register_agent("d0", model_id="m")
        engine.register_agent("d1", model_id="m", parent_id="d0")
        engine.register_agent("d2", model_id="m", parent_id="d1")
        engine.register_agent("d3", model_id="m", parent_id="d2")

        # d3 is at depth 3 (the limit). Trying to go deeper should fail.
        r = engine.register_agent("d4", model_id="m", parent_id="d3")
        assert r.allowed is False
        assert "Depth limit" in r.reason


class TestFanoutLimit:
    """Fanout limit enforcement."""

    def test_fanout_limit_blocks_wide_spawning(self):
        engine = CascadeEngine(fanout_limit=3)
        engine.register_agent("root", model_id="m")

        # Spawn up to limit
        engine.register_agent("c1", model_id="m", parent_id="root")
        engine.register_agent("c2", model_id="m", parent_id="root")
        engine.register_agent("c3", model_id="m", parent_id="root")

        # 4th child should be blocked
        r = engine.register_agent("c4", model_id="m", parent_id="root")
        assert r.allowed is False
        assert "Fanout limit" in r.reason


class TestChainTracing:
    """Chain and subtree operations."""

    def test_get_chain_returns_root_to_leaf(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m")
        engine.register_agent("B", model_id="m", parent_id="A")
        engine.register_agent("C", model_id="m", parent_id="B")

        chain = engine.get_chain("C")
        assert chain == ["A", "B", "C"]

    def test_get_chain_for_root(self):
        engine = CascadeEngine()
        engine.register_agent("root", model_id="m")
        assert engine.get_chain("root") == ["root"]

    def test_get_chain_for_missing_agent(self):
        engine = CascadeEngine()
        assert engine.get_chain("nonexistent") == []

    def test_get_subtree(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m")
        engine.register_agent("B", model_id="m", parent_id="A")
        engine.register_agent("C", model_id="m", parent_id="A")
        engine.register_agent("D", model_id="m", parent_id="B")

        subtree = engine.get_subtree("A")
        assert set(subtree) == {"A", "B", "C", "D"}


class TestRevocation:
    """Agent revocation."""

    def test_revoke_removes_subtree(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m")
        engine.register_agent("B", model_id="m", parent_id="A")
        engine.register_agent("C", model_id="m", parent_id="B")

        success, msg = engine.revoke_agent("B")
        assert success is True
        assert engine.num_agents == 1  # Only A remains
        assert "B" not in engine._agents
        assert "C" not in engine._agents

    def test_revoke_missing_agent_fails(self):
        engine = CascadeEngine()
        success, msg = engine.revoke_agent("nonexistent")
        assert success is False


class TestStatus:
    """System status reporting."""

    def test_status_reflects_state(self):
        engine = CascadeEngine()
        engine.register_agent("A", model_id="m")
        engine.register_agent("B", model_id="m", parent_id="A")
        engine.register_agent("C", model_id="m", parent_id="B")

        # Try a cycle (should be blocked)
        engine.attempt_delegation("C", "A", DelegationAction.DELEGATE)

        status = engine.get_status()
        assert status.total_agents == 3
        assert status.total_delegations == 2  # B and C registrations
        assert status.cycles_detected == 1
        assert status.delegations_blocked == 1
        assert status.max_depth == 2
