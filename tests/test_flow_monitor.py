"""Tests for FlowMonitor distribution-based impedance."""

import time

from cascade_guard.flow_monitor import FlowMonitor
from cascade_guard.models import FlowState


class TestFlowMonitorBasics:
    """Basic flow monitoring operations."""

    def test_initial_state_is_nominal(self):
        fm = FlowMonitor()
        assert fm.flow_state == FlowState.NOMINAL
        assert fm.velocity == 0.0
        assert fm.kappa_effective == 1.0
        assert fm.can_delegate() is True

    def test_impedance_starts_at_zero(self):
        fm = FlowMonitor()
        report = fm.compute_impedance()
        assert report.impedance == 0.0
        assert report.flow_state == FlowState.NOMINAL

    def test_recording_delegation_increases_velocity(self):
        fm = FlowMonitor(max_velocity=10.0)
        fm.record_delegation("src", "tgt", depth=1, fan_out=1)
        assert fm.velocity > 0.0

    def test_depth_increases_impedance(self):
        fm = FlowMonitor(depth_limit=5)
        # Record delegations at increasing depth
        for i in range(1, 6):
            fm.record_delegation(f"src-{i}", f"tgt-{i}", depth=i, fan_out=1)

        report = fm.compute_impedance()
        # Max depth = 5 = depth_limit → depth_ratio = 1.0
        assert report.max_depth == 5
        assert report.impedance > 0.0

    def test_fanout_increases_impedance(self):
        fm = FlowMonitor(fanout_limit=5)
        # One source with high fanout
        for i in range(5):
            fm.record_delegation("src", f"tgt-{i}", depth=1, fan_out=i + 1)

        report = fm.compute_impedance()
        assert report.max_fan_out == 5
        assert report.impedance > 0.0


class TestImpedanceThresholds:
    """Test flow state transitions based on impedance."""

    def test_preservation_mode_blocks_delegation(self):
        fm = FlowMonitor(
            max_velocity=2.0,
            depth_limit=2,
            fanout_limit=2,
            preservation_threshold=0.3,
            window_seconds=60.0,
        )

        # Saturate all dimensions
        for i in range(10):
            fm.record_delegation("src", f"tgt-{i}", depth=2, fan_out=10)

        # Should be in preservation or throttled
        assert fm.kappa_effective < 1.0
        # Impedance should be high
        report = fm.compute_impedance()
        assert report.impedance > 0.5

    def test_concentration_with_single_source(self):
        fm = FlowMonitor()
        # All delegations from one source
        for i in range(10):
            fm.record_delegation("single-src", f"tgt-{i}", depth=1, fan_out=i + 1)

        report = fm.compute_impedance()
        # Single source = no concentration variance (Gini = 0 for single element)
        # But with only one source, concentration should be 0
        assert report.concentration == 0.0

    def test_concentration_with_uneven_sources(self):
        fm = FlowMonitor()
        # One source does 10 delegations, another does 1
        for i in range(10):
            fm.record_delegation("heavy", f"tgt-h-{i}", depth=1, fan_out=i + 1)
        fm.record_delegation("light", "tgt-l-1", depth=1, fan_out=1)

        report = fm.compute_impedance()
        # Should have non-zero concentration
        assert report.concentration > 0.0


class TestFlowMonitorReset:
    """Test reset functionality."""

    def test_reset_clears_all_state(self):
        fm = FlowMonitor()
        for i in range(5):
            fm.record_delegation(f"src-{i}", f"tgt-{i}", depth=i, fan_out=1)

        fm.reset()
        assert fm.velocity == 0.0
        report = fm.compute_impedance()
        assert report.impedance == 0.0
        assert report.max_depth == 0
        assert report.max_fan_out == 0
