"""Tests for Metrics (Task 10) and Shutdown (Task 11).

Verifies:
- Metrics: Prometheus format, CloudWatch EMF, health/ready endpoints, rates
- Shutdown: drain lifecycle, state persistence, timeout handling
"""

import json
import tempfile
import time

import pytest

from cascade_guard.mcp_proxy.metrics import MetricsEmitter, HealthState
from cascade_guard.mcp_proxy.shutdown import (
    PersistedEngineState,
    ShutdownManager,
    ShutdownState,
)


class TestMetricsEmission:
    """MetricsEmitter basic operations."""

    def test_initial_state(self):
        """Fresh emitter has zero counts."""
        m = MetricsEmitter()
        assert m.total_invocations == 0
        assert m.total_blocked == 0
        assert m.avg_latency_us() == 0.0

    def test_emit_invocation_increments(self):
        """emit_invocation increments counters."""
        m = MetricsEmitter()
        m.emit_invocation("tool/a", "allowed")
        m.emit_invocation("tool/b", "blocked")
        m.emit_invocation("tool/c", "allowed")

        assert m.total_invocations == 3
        assert m.total_blocked == 1

    def test_emit_latency_tracked(self):
        """emit_latency updates average."""
        m = MetricsEmitter()
        m.emit_latency(100)
        m.emit_latency(200)
        m.emit_latency(300)

        assert m.avg_latency_us() == 200.0

    def test_flow_state_change_recorded(self):
        """Flow state changes are recorded."""
        m = MetricsEmitter()
        m.emit_flow_state_change("nominal", "elevated", "high_velocity", 0.7)

        events = m.get_flow_state_events()
        assert len(events) == 1
        assert events[0]["previous_state"] == "nominal"
        assert events[0]["new_state"] == "elevated"
        assert events[0]["trigger"] == "high_velocity"

    def test_agent_throttle_event(self):
        """Agent throttle events are recorded."""
        m = MetricsEmitter()
        m.emit_agent_throttle("noisy-agent", True, 120.0, 100.0)

        events = m.get_throttle_events()
        assert len(events) == 1
        assert events[0]["agent_id"] == "noisy-agent"
        assert events[0]["throttled"] is True


class TestMetricsFormats:
    """Prometheus and CloudWatch EMF output."""

    def test_prometheus_format(self):
        """to_prometheus returns valid Prometheus exposition format."""
        m = MetricsEmitter()
        m.emit_invocation("tool/a", "allowed")
        m.emit_invocation("tool/b", "blocked")
        m.emit_latency(50)

        output = m.to_prometheus()

        assert "cascadeguard_invocations_total 2" in output
        assert "cascadeguard_blocked_total 1" in output
        assert "cascadeguard_safety_check_latency_us 50.0" in output
        assert "# TYPE" in output
        assert "# HELP" in output

    def test_cloudwatch_emf_format(self):
        """to_cloudwatch_emf returns valid EMF JSON."""
        m = MetricsEmitter()
        m.emit_invocation("tool/a", "allowed")
        m.update_kappa(0.85)

        output = m.to_cloudwatch_emf()
        parsed = json.loads(output)

        assert "_aws" in parsed
        assert parsed["InvocationsTotal"] == 1
        assert parsed["KappaEffective"] == 0.85
        assert parsed["FlowState"] == "nominal"
        assert "CloudWatchMetrics" in parsed["_aws"]


class TestHealthAndReady:
    """Health and readiness endpoints."""

    def test_health_check(self):
        """health_check returns current state."""
        m = MetricsEmitter()
        m.emit_invocation("t", "allowed")
        m.update_active_agents(5)
        m.update_kappa(0.9)

        health = m.health_check()
        assert health.total_invocations == 1
        assert health.active_agents == 5
        assert health.kappa_effective == 0.9
        assert health.is_draining is False

    def test_ready_check_normal(self):
        """ready_check returns True when not draining."""
        m = MetricsEmitter()
        assert m.ready_check() is True

    def test_ready_check_draining(self):
        """ready_check returns False when draining."""
        m = MetricsEmitter()
        m.set_draining(True)
        assert m.ready_check() is False

    def test_health_state_to_dict(self):
        """HealthState.to_dict() has expected fields."""
        state = HealthState(
            flow_state="throttled",
            kappa_effective=0.4,
            active_agents=3,
        )
        d = state.to_dict()
        assert d["flow_state"] == "throttled"
        assert d["kappa_effective"] == 0.4
        assert d["active_agents"] == 3


class TestShutdownDrainLifecycle:
    """Shutdown drain state machine."""

    def test_initial_state_not_draining(self):
        """Initially not draining."""
        mgr = ShutdownManager(persist_state=False)
        assert mgr.is_draining is False

    def test_start_drain_sets_state(self):
        """start_drain enters drain mode."""
        mgr = ShutdownManager(persist_state=False)
        mgr.start_drain()

        assert mgr.is_draining is True
        assert mgr.state.drain_started_at > 0

    def test_drain_rejects_new_requests(self):
        """track_request_start returns False during drain."""
        mgr = ShutdownManager(persist_state=False)
        assert mgr.state.track_request_start() is True  # Before drain

        # Cannot use track_request_start on ShutdownState directly
        # Test via the manager
        mgr.start_drain()
        accepted = mgr.track_request_start()
        assert accepted is False

    def test_in_flight_tracking(self):
        """Tracks in-flight request count."""
        mgr = ShutdownManager(persist_state=False)
        mgr.track_request_start()
        mgr.track_request_start()
        assert mgr.state.in_flight_count == 2

        mgr.track_request_end()
        assert mgr.state.in_flight_count == 1

    def test_drain_completes_when_no_in_flight(self):
        """Drain completes when all in-flight requests finish."""
        completed = []
        mgr = ShutdownManager(
            persist_state=False,
            on_drain_complete=lambda: completed.append(True),
        )

        mgr.track_request_start()
        mgr.start_drain()

        # Still has in-flight
        assert mgr.state.drain_complete is False

        # Complete the request
        mgr.track_request_end()
        assert len(completed) == 1

    def test_drain_timeout(self):
        """Drain timeout abandons remaining requests."""
        mgr = ShutdownManager(drain_timeout=0.05, persist_state=False)
        mgr.track_request_start()
        mgr.track_request_start()
        mgr.start_drain()

        # Wait for timeout
        time.sleep(0.06)

        result = mgr.check_drain_timeout()
        assert result is True
        assert mgr.state.abandoned_count == 2
        assert mgr.state.in_flight_count == 0

    def test_start_drain_idempotent(self):
        """Starting drain twice doesn't reset state."""
        mgr = ShutdownManager(persist_state=False)
        mgr.start_drain()
        first_start = mgr.state.drain_started_at

        time.sleep(0.01)
        mgr.start_drain()
        assert mgr.state.drain_started_at == first_start


class TestStatePersistence:
    """Engine state persistence and restoration."""

    def test_persist_and_load(self):
        """State persists to JSON and loads back."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = f.name

        try:
            mgr = ShutdownManager(state_path=state_path, persist_state=True)

            state = PersistedEngineState(
                agents={"agent-a": {"tokens": 500}},
                total_tokens_consumed=5000.0,
                total_delegations=42,
                flow_state="elevated",
                kappa_effective=0.7,
            )

            success = mgr.persist_engine_state(state)
            assert success is True

            # Load it back
            loaded = mgr.load_persisted_state()
            assert loaded is not None
            assert loaded.total_tokens_consumed == 5000.0
            assert loaded.total_delegations == 42
            assert loaded.flow_state == "elevated"
            assert loaded.kappa_effective == 0.7
            assert loaded.agents == {"agent-a": {"tokens": 500}}
            assert loaded.persisted_at > 0
        finally:
            import os
            os.unlink(state_path)

    def test_load_nonexistent_returns_none(self):
        """Loading from nonexistent file returns None."""
        mgr = ShutdownManager(state_path="/nonexistent/path.json", persist_state=True)
        assert mgr.load_persisted_state() is None

    def test_persist_disabled(self):
        """persist_state=False skips persistence."""
        mgr = ShutdownManager(persist_state=False)
        state = PersistedEngineState()
        assert mgr.persist_engine_state(state) is False

    def test_persisted_state_roundtrip(self):
        """PersistedEngineState to_dict/from_dict roundtrip."""
        state = PersistedEngineState(
            agents={"a": {"model": "gpt-4"}},
            total_tokens_consumed=1234.5,
            total_delegations=10,
            cycles_detected=2,
            delegations_blocked=5,
            flow_state="throttled",
            kappa_effective=0.45,
            persisted_at=1717500000.0,
        )

        d = state.to_dict()
        restored = PersistedEngineState.from_dict(d)

        assert restored.total_tokens_consumed == 1234.5
        assert restored.total_delegations == 10
        assert restored.cycles_detected == 2
        assert restored.delegations_blocked == 5
        assert restored.flow_state == "throttled"
        assert restored.kappa_effective == 0.45

    def test_clear_persisted_state(self):
        """clear_persisted_state removes the file."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = f.name
            f.write(b'{"test": true}')

        try:
            mgr = ShutdownManager(state_path=state_path, persist_state=True)
            mgr.clear_persisted_state()

            import os
            assert not os.path.exists(state_path)
        except Exception:
            import os
            if os.path.exists(state_path):
                os.unlink(state_path)
            raise
