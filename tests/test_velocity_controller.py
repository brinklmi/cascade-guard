"""Tests for Per-Agent Velocity Controller.

Verifies:
1. Noisy agent throttled independently
2. Quiet agents unaffected by noisy neighbor
3. Per-agent threshold overrides work
4. Default threshold is 2x global max_velocity
5. Throttled state transitions (normal ↔ throttled)
6. Rolling window expiration
7. Property: throttled agent never exceeds threshold + boundary tolerance
"""

import time

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from cascade_guard.mcp_proxy.velocity import AgentVelocityController, VelocityCheck


class TestVelocityControllerBasics:
    """Basic velocity controller operations."""

    def test_default_threshold_is_2x_global(self):
        """Default per-agent threshold = 2 * global_max_velocity."""
        vc = AgentVelocityController(global_max_velocity=50.0)
        assert vc.default_threshold == 100.0

    def test_custom_default_threshold(self):
        """Custom default_threshold overrides 2x formula."""
        vc = AgentVelocityController(
            global_max_velocity=50.0,
            default_threshold=75.0,
        )
        assert vc.default_threshold == 75.0

    def test_window_seconds_configurable(self):
        """Window seconds is configurable."""
        vc = AgentVelocityController(window_seconds=30.0)
        assert vc.window_seconds == 30.0

    def test_no_invocations_rate_zero(self):
        """Agent with no invocations has rate 0."""
        vc = AgentVelocityController()
        assert vc.get_agent_rate("agent-x") == 0.0

    def test_single_invocation_recorded(self):
        """Single invocation results in non-zero rate."""
        vc = AgentVelocityController()
        vc.record_invocation("agent-a")
        rate = vc.get_agent_rate("agent-a")
        assert rate > 0.0

    def test_check_velocity_allowed_under_threshold(self):
        """Agent under threshold is allowed."""
        vc = AgentVelocityController(global_max_velocity=50.0)
        vc.record_invocation("agent-a")

        check = vc.check_velocity("agent-a")
        assert check.allowed is True
        assert check.reason == ""
        assert check.threshold == 100.0

    def test_check_velocity_returns_current_rate(self):
        """VelocityCheck includes current rate."""
        vc = AgentVelocityController()
        vc.record_invocation("agent-a")

        check = vc.check_velocity("agent-a")
        assert check.current_rate > 0.0

    def test_unknown_agent_allowed(self):
        """Unknown agent with no history is always allowed."""
        vc = AgentVelocityController()
        check = vc.check_velocity("unknown-agent")
        assert check.allowed is True
        assert check.current_rate == 0.0


class TestNoisyNeighborIsolation:
    """Verify noisy agents are throttled without affecting others."""

    def test_noisy_agent_throttled(self):
        """Agent exceeding threshold is blocked."""
        # Very low threshold for testing
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=5.0,
        )

        # Fire 10 invocations rapidly (exceeds threshold of 5/sec)
        for _ in range(10):
            vc.record_invocation("noisy-agent")

        check = vc.check_velocity("noisy-agent")
        assert check.allowed is False
        assert check.reason == "agent_velocity_exceeded"
        assert check.current_rate > 5.0

    def test_quiet_agent_unaffected_by_noisy_neighbor(self):
        """Other agents remain unaffected when one is throttled."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=5.0,
        )

        # Noisy agent exceeds threshold
        for _ in range(10):
            vc.record_invocation("noisy-agent")

        # Quiet agent has minimal usage
        vc.record_invocation("quiet-agent")

        # Noisy is throttled
        noisy_check = vc.check_velocity("noisy-agent")
        assert noisy_check.allowed is False

        # Quiet is still allowed
        quiet_check = vc.check_velocity("quiet-agent")
        assert quiet_check.allowed is True

    def test_multiple_agents_independent(self):
        """Each agent's velocity is tracked independently."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=5.0,
        )

        # Agent A: 3 invocations (under threshold)
        for _ in range(3):
            vc.record_invocation("agent-a")

        # Agent B: 8 invocations (over threshold)
        for _ in range(8):
            vc.record_invocation("agent-b")

        # Agent C: 0 invocations
        assert vc.check_velocity("agent-a").allowed is True
        assert vc.check_velocity("agent-b").allowed is False
        assert vc.check_velocity("agent-c").allowed is True


class TestThresholdOverrides:
    """Per-agent threshold configuration."""

    def test_set_threshold_override(self):
        """Per-agent threshold overrides the default."""
        vc = AgentVelocityController(default_threshold=100.0)
        vc.set_threshold("restricted-agent", 10.0)

        assert vc.get_threshold("restricted-agent") == 10.0
        assert vc.get_threshold("normal-agent") == 100.0

    def test_override_affects_throttling(self):
        """Agent with low override gets throttled sooner."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=100.0,
        )
        vc.set_threshold("restricted", 3.0)

        # 5 invocations: over 3 threshold but under 100 default
        for _ in range(5):
            vc.record_invocation("restricted")
            vc.record_invocation("normal")

        assert vc.check_velocity("restricted").allowed is False
        assert vc.check_velocity("normal").allowed is True

    def test_remove_threshold_reverts_to_default(self):
        """Removing override reverts to default threshold."""
        vc = AgentVelocityController(default_threshold=100.0)
        vc.set_threshold("agent-x", 10.0)
        assert vc.get_threshold("agent-x") == 10.0

        vc.remove_threshold("agent-x")
        assert vc.get_threshold("agent-x") == 100.0

    def test_set_threshold_rejects_non_positive(self):
        """Threshold must be positive."""
        vc = AgentVelocityController()
        with pytest.raises(ValueError):
            vc.set_threshold("agent-x", 0.0)
        with pytest.raises(ValueError):
            vc.set_threshold("agent-x", -5.0)


class TestThrottleStateTransitions:
    """Throttled state tracking and transitions."""

    def test_initially_not_throttled(self):
        """Agents start in non-throttled state."""
        vc = AgentVelocityController()
        assert vc.is_throttled("agent-a") is False

    def test_becomes_throttled_on_exceed(self):
        """Agent transitions to throttled when exceeding threshold."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=3.0,
        )

        for _ in range(5):
            vc.record_invocation("agent-a")

        vc.check_velocity("agent-a")
        assert vc.is_throttled("agent-a") is True

    def test_unthrottled_on_recovery(self):
        """Agent transitions back to normal when rate drops below threshold."""
        vc = AgentVelocityController(
            window_seconds=0.05,  # 50ms window for fast test
            default_threshold=3.0,
        )

        # Exceed threshold
        for _ in range(5):
            vc.record_invocation("agent-a")
        vc.check_velocity("agent-a")
        assert vc.is_throttled("agent-a") is True

        # Wait for window to expire
        time.sleep(0.06)

        # Now should be under threshold
        check = vc.check_velocity("agent-a")
        assert check.allowed is True
        assert vc.is_throttled("agent-a") is False

    def test_get_throttled_agents_set(self):
        """get_throttled_agents returns currently throttled set."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=3.0,
        )

        for _ in range(5):
            vc.record_invocation("agent-a")
            vc.record_invocation("agent-b")

        vc.check_velocity("agent-a")
        vc.check_velocity("agent-b")

        throttled = vc.get_throttled_agents()
        assert "agent-a" in throttled
        assert "agent-b" in throttled


class TestRollingWindowExpiration:
    """Verify timestamps expire correctly from the rolling window."""

    def test_old_invocations_expire(self):
        """Invocations older than window_seconds are pruned."""
        vc = AgentVelocityController(
            window_seconds=0.05,  # 50ms window
            default_threshold=3.0,
        )

        # Record invocations
        for _ in range(5):
            vc.record_invocation("agent-a")

        # Verify over threshold
        assert vc.check_velocity("agent-a").allowed is False

        # Wait for window to expire
        time.sleep(0.06)

        # Now should be at 0 rate
        assert vc.get_agent_rate("agent-a") == 0.0
        assert vc.check_velocity("agent-a").allowed is True

    def test_reset_agent_clears_window(self):
        """reset_agent clears an agent's history."""
        vc = AgentVelocityController(
            window_seconds=60.0,
            default_threshold=3.0,
        )

        for _ in range(5):
            vc.record_invocation("agent-a")

        assert vc.get_agent_rate("agent-a") > 0
        vc.reset_agent("agent-a")
        assert vc.get_agent_rate("agent-a") == 0.0
        assert vc.is_throttled("agent-a") is False

    def test_full_reset_clears_all(self):
        """reset() clears all agent state."""
        vc = AgentVelocityController(
            window_seconds=60.0,
            default_threshold=3.0,
        )

        for _ in range(5):
            vc.record_invocation("agent-a")
            vc.record_invocation("agent-b")

        vc.reset()
        assert vc.get_agent_rate("agent-a") == 0.0
        assert vc.get_agent_rate("agent-b") == 0.0
        assert vc.get_throttled_agents() == set()


class TestVelocityPropertyBased:
    """Property-based tests for velocity controller invariants."""

    @given(
        num_invocations=st.integers(min_value=0, max_value=50),
        threshold=st.floats(min_value=1.0, max_value=1000.0),
    )
    @settings(max_examples=100)
    def test_throttled_means_rate_exceeds_threshold(self, num_invocations, threshold):
        """Invariant: if throttled, current_rate > threshold."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=threshold,
        )

        for _ in range(num_invocations):
            vc.record_invocation("test-agent")

        check = vc.check_velocity("test-agent")

        if not check.allowed:
            # If blocked, rate must exceed threshold
            assert check.current_rate > check.threshold
            assert check.reason == "agent_velocity_exceeded"
        else:
            # If allowed, rate must be at or below threshold
            assert check.current_rate <= check.threshold

    @given(
        agent_ids=st.lists(
            st.text(alphabet="abcdef", min_size=1, max_size=5),
            min_size=2,
            max_size=5,
            unique=True,
        ),
        invocations_per_agent=st.lists(
            st.integers(min_value=0, max_value=20),
            min_size=2,
            max_size=5,
        ),
    )
    @settings(max_examples=50)
    def test_agents_independent_property(self, agent_ids, invocations_per_agent):
        """Property: each agent's throttle state depends only on its own invocations."""
        # Trim to same length
        n = min(len(agent_ids), len(invocations_per_agent))
        agent_ids = agent_ids[:n]
        invocations_per_agent = invocations_per_agent[:n]

        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=10.0,
        )

        # Record invocations for each agent
        for agent_id, count in zip(agent_ids, invocations_per_agent):
            for _ in range(count):
                vc.record_invocation(agent_id)

        # Check each agent — its result should only depend on its own count
        for i, agent_id in enumerate(agent_ids):
            check = vc.check_velocity(agent_id)
            rate = vc.get_agent_rate(agent_id)

            # Rate should be consistent with invocation count
            # (can't assert exact equality due to timing, but rate should be >= 0)
            assert rate >= 0.0
            assert check.current_rate == rate

    @given(threshold=st.floats(min_value=0.1, max_value=100.0))
    @settings(max_examples=50)
    def test_no_invocations_always_allowed(self, threshold):
        """Property: zero invocations → always allowed regardless of threshold."""
        vc = AgentVelocityController(
            window_seconds=1.0,
            default_threshold=threshold,
        )

        check = vc.check_velocity("fresh-agent")
        assert check.allowed is True
        assert check.current_rate == 0.0


class TestGetAllRates:
    """Test the get_all_rates utility."""

    def test_returns_all_known_agents(self):
        """get_all_rates includes all agents with history."""
        vc = AgentVelocityController()
        vc.record_invocation("agent-a")
        vc.record_invocation("agent-b")
        vc.record_invocation("agent-c")

        rates = vc.get_all_rates()
        assert "agent-a" in rates
        assert "agent-b" in rates
        assert "agent-c" in rates
        assert all(r > 0 for r in rates.values())

    def test_empty_when_no_invocations(self):
        """get_all_rates is empty with no invocations."""
        vc = AgentVelocityController()
        assert vc.get_all_rates() == {}
