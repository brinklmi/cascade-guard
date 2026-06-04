"""Tests for Decision Log & Audit Trail.

Verifies:
1. Append and query entries
2. Ring buffer eviction (bounded memory)
3. JSON Lines export (CloudWatch compatible)
4. Deterministic replay inputs
5. Filtering by agent and verdict
6. Statistics computation
7. Property: replay inputs match entries
"""

import json
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cascade_guard.mcp_proxy.decision_log import (
    DecisionEntry,
    DecisionLog,
    ReplayInput,
)


class TestDecisionLogBasics:
    """Basic append and query operations."""

    def test_empty_log(self):
        """New log has zero entries."""
        log = DecisionLog()
        assert log.size == 0
        assert log.total_appended == 0
        assert log.query() == []

    def test_append_single_entry(self):
        """Appending an entry increases size."""
        log = DecisionLog()
        entry = DecisionEntry(
            timestamp=time.time(),
            agent_id="agent-a",
            tool_name="datadog/list_monitors",
            verdict="allowed",
            reason="Delegation allowed",
            impedance=0.1,
            flow_state="nominal",
        )
        log.append(entry)

        assert log.size == 1
        assert log.total_appended == 1

    def test_record_convenience_method(self):
        """record() creates and appends entry with auto-timestamp."""
        log = DecisionLog()
        entry = log.record(
            agent_id="agent-b",
            tool_name="splunk/search",
            verdict="blocked",
            reason="cycle_detected",
            impedance=0.8,
            flow_state="throttled",
        )

        assert log.size == 1
        assert entry.agent_id == "agent-b"
        assert entry.verdict == "blocked"
        assert entry.timestamp > 0

    def test_query_returns_most_recent(self):
        """query(n) returns the most recent N entries."""
        log = DecisionLog()
        for i in range(10):
            log.record(
                agent_id=f"agent-{i}",
                tool_name="tool/test",
                verdict="allowed",
                reason="ok",
            )

        recent = log.query(3)
        assert len(recent) == 3
        assert recent[-1].agent_id == "agent-9"
        assert recent[0].agent_id == "agent-7"

    def test_query_default_50(self):
        """Default query returns up to 50 entries."""
        log = DecisionLog()
        for i in range(100):
            log.record(agent_id=f"a-{i}", tool_name="t", verdict="allowed", reason="ok")

        result = log.query()
        assert len(result) == 50

    def test_query_all_if_less_than_n(self):
        """Query returns all if fewer than N entries exist."""
        log = DecisionLog()
        log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok")
        log.record(agent_id="b", tool_name="t", verdict="allowed", reason="ok")

        result = log.query(100)
        assert len(result) == 2


class TestRingBufferEviction:
    """Bounded memory via ring buffer."""

    def test_eviction_at_max(self):
        """Oldest entries evicted when max_entries reached."""
        log = DecisionLog(max_entries=5)

        for i in range(10):
            log.record(agent_id=f"a-{i}", tool_name="t", verdict="allowed", reason="ok")

        assert log.size == 5
        assert log.total_appended == 10

        # Oldest should be a-5 (a-0 through a-4 evicted)
        entries = log.query(5)
        assert entries[0].agent_id == "a-5"
        assert entries[-1].agent_id == "a-9"

    def test_max_entries_configurable(self):
        """max_entries is configurable."""
        log = DecisionLog(max_entries=100)
        assert log.max_entries == 100

    def test_default_max_entries(self):
        """Default max_entries is 10000."""
        log = DecisionLog()
        assert log.max_entries == 10000


class TestJSONLinesExport:
    """JSON Lines export for CloudWatch Logs compatibility."""

    def test_export_single_entry(self):
        """Single entry exports as one JSON line."""
        log = DecisionLog()
        log.record(
            agent_id="agent-x",
            tool_name="datadog/get_metrics",
            verdict="allowed",
            reason="ok",
            impedance=0.05,
            latency_us=12,
        )

        output = log.export_json_lines()
        lines = output.strip().split("\n")
        assert len(lines) == 1

        parsed = json.loads(lines[0])
        assert parsed["agent_id"] == "agent-x"
        assert parsed["tool_name"] == "datadog/get_metrics"
        assert parsed["verdict"] == "allowed"
        assert parsed["latency_us"] == 12

    def test_export_multiple_entries(self):
        """Multiple entries export as multiple JSON lines."""
        log = DecisionLog()
        for i in range(5):
            log.record(agent_id=f"a-{i}", tool_name="t", verdict="allowed", reason="ok")

        output = log.export_json_lines()
        lines = output.strip().split("\n")
        assert len(lines) == 5

        # Each line is valid JSON
        for line in lines:
            parsed = json.loads(line)
            assert "agent_id" in parsed
            assert "timestamp" in parsed

    def test_export_with_limit(self):
        """export_json_lines(n) limits output."""
        log = DecisionLog()
        for i in range(10):
            log.record(agent_id=f"a-{i}", tool_name="t", verdict="allowed", reason="ok")

        output = log.export_json_lines(n=3)
        lines = output.strip().split("\n")
        assert len(lines) == 3

    def test_empty_log_exports_empty(self):
        """Empty log exports empty string."""
        log = DecisionLog()
        assert log.export_json_lines() == ""


class TestEntrySerializaton:
    """DecisionEntry serialization and deserialization."""

    def test_to_dict_roundtrip(self):
        """to_dict() and from_dict() are inverses."""
        entry = DecisionEntry(
            timestamp=1717500000.0,
            agent_id="test-agent",
            tool_name="aws-cli/describe_instances",
            verdict="blocked",
            reason="agent_budget_exceeded",
            impedance=0.75,
            flow_state="throttled",
            tokens_consumed=45000.0,
            schema_version="1.0",
            latency_us=250,
        )

        d = entry.to_dict()
        restored = DecisionEntry.from_dict(d)

        assert restored.timestamp == entry.timestamp
        assert restored.agent_id == entry.agent_id
        assert restored.tool_name == entry.tool_name
        assert restored.verdict == entry.verdict
        assert restored.reason == entry.reason
        assert restored.impedance == entry.impedance
        assert restored.flow_state == entry.flow_state
        assert restored.tokens_consumed == entry.tokens_consumed
        assert restored.schema_version == entry.schema_version
        assert restored.latency_us == entry.latency_us

    def test_to_json_line_is_valid_json(self):
        """to_json_line() produces valid JSON."""
        entry = DecisionEntry(
            timestamp=time.time(),
            agent_id="a",
            tool_name="t",
            verdict="allowed",
            reason="ok",
        )

        line = entry.to_json_line()
        parsed = json.loads(line)
        assert parsed["agent_id"] == "a"
        assert "\n" not in line  # Single line


class TestReplayInputs:
    """Deterministic replay input extraction."""

    def test_get_replay_inputs(self):
        """get_replay_inputs returns minimal replay data."""
        log = DecisionLog()
        log.record(agent_id="a1", tool_name="t1", verdict="allowed", reason="ok")
        log.record(agent_id="a2", tool_name="t2", verdict="blocked", reason="cycle")

        inputs = log.get_replay_inputs()
        assert len(inputs) == 2
        assert inputs[0].agent_id == "a1"
        assert inputs[0].tool_name == "t1"
        assert inputs[0].timestamp > 0
        assert inputs[1].agent_id == "a2"

    def test_replay_inputs_match_entries(self):
        """Each replay input corresponds to a log entry."""
        log = DecisionLog()
        for i in range(5):
            log.record(agent_id=f"a-{i}", tool_name=f"t-{i}", verdict="allowed", reason="ok")

        inputs = log.get_replay_inputs()
        entries = log.query(5)

        for inp, entry in zip(inputs, entries):
            assert inp.agent_id == entry.agent_id
            assert inp.tool_name == entry.tool_name
            assert inp.timestamp == entry.timestamp


class TestFiltering:
    """Filtering by agent and verdict."""

    def test_query_by_agent(self):
        """query_by_agent returns only entries for that agent."""
        log = DecisionLog()
        log.record(agent_id="a", tool_name="t1", verdict="allowed", reason="ok")
        log.record(agent_id="b", tool_name="t2", verdict="blocked", reason="x")
        log.record(agent_id="a", tool_name="t3", verdict="blocked", reason="y")

        a_entries = log.query_by_agent("a")
        assert len(a_entries) == 2
        assert all(e.agent_id == "a" for e in a_entries)

    def test_query_blocked(self):
        """query_blocked returns only blocked entries."""
        log = DecisionLog()
        log.record(agent_id="a", tool_name="t1", verdict="allowed", reason="ok")
        log.record(agent_id="b", tool_name="t2", verdict="blocked", reason="x")
        log.record(agent_id="c", tool_name="t3", verdict="blocked", reason="y")

        blocked = log.query_blocked()
        assert len(blocked) == 2
        assert all(e.verdict == "blocked" for e in blocked)

    def test_query_by_agent_with_limit(self):
        """query_by_agent respects limit."""
        log = DecisionLog()
        for i in range(10):
            log.record(agent_id="target", tool_name=f"t-{i}", verdict="allowed", reason="ok")

        result = log.query_by_agent("target", n=3)
        assert len(result) == 3


class TestStatistics:
    """Log statistics computation."""

    def test_empty_stats(self):
        """Empty log returns zero stats."""
        log = DecisionLog()
        stats = log.get_stats()
        assert stats["total_entries"] == 0
        assert stats["allowed_count"] == 0
        assert stats["blocked_count"] == 0

    def test_stats_counts(self):
        """Stats correctly count allowed and blocked."""
        log = DecisionLog()
        log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok")
        log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok")
        log.record(agent_id="b", tool_name="t", verdict="blocked", reason="x")

        stats = log.get_stats()
        assert stats["total_entries"] == 3
        assert stats["allowed_count"] == 2
        assert stats["blocked_count"] == 1
        assert stats["allowed_rate"] == pytest.approx(2 / 3)
        assert stats["blocked_rate"] == pytest.approx(1 / 3)

    def test_stats_unique_agents_and_tools(self):
        """Stats counts unique agents and tools."""
        log = DecisionLog()
        log.record(agent_id="a", tool_name="t1", verdict="allowed", reason="ok")
        log.record(agent_id="a", tool_name="t2", verdict="allowed", reason="ok")
        log.record(agent_id="b", tool_name="t1", verdict="allowed", reason="ok")

        stats = log.get_stats()
        assert stats["unique_agents"] == 2
        assert stats["unique_tools"] == 2

    def test_stats_avg_latency(self):
        """Stats computes average latency."""
        log = DecisionLog()
        log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok", latency_us=100)
        log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok", latency_us=200)
        log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok", latency_us=300)

        stats = log.get_stats()
        assert stats["avg_latency_us"] == 200.0


class TestDecisionLogPropertyBased:
    """Property-based tests for decision log invariants."""

    @given(
        n_entries=st.integers(min_value=0, max_value=100),
        max_entries=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=50)
    def test_size_never_exceeds_max(self, n_entries, max_entries):
        """Invariant: log.size <= log.max_entries always."""
        log = DecisionLog(max_entries=max_entries)

        for i in range(n_entries):
            log.record(agent_id=f"a-{i}", tool_name="t", verdict="allowed", reason="ok")

        assert log.size <= max_entries
        assert log.total_appended == n_entries

    @given(n_entries=st.integers(min_value=1, max_value=50))
    @settings(max_examples=30)
    def test_replay_inputs_length_matches_entries(self, n_entries):
        """Invariant: len(replay_inputs) == log.size."""
        log = DecisionLog()

        for i in range(n_entries):
            log.record(agent_id=f"a-{i}", tool_name=f"t-{i}", verdict="allowed", reason="ok")

        inputs = log.get_replay_inputs()
        assert len(inputs) == log.size

    def test_clear_resets_to_empty(self):
        """clear() empties the log."""
        log = DecisionLog()
        for i in range(10):
            log.record(agent_id="a", tool_name="t", verdict="allowed", reason="ok")

        log.clear()
        assert log.size == 0
        assert log.query() == []
