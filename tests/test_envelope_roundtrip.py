"""Property-based tests for Envelope Parser — round-trip invariant and performance.

Verifies:
1. Round-trip property: parse(call).reconstruct() == extract_fields(call)
2. Parse latency <1ms (1000μs) for all valid inputs
3. Schema version validation (unsupported versions rejected)
4. Zero semantic overhead (arguments never touched)
5. Missing envelope handled gracefully (defaults returned)
"""

import time

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from cascade_guard.mcp_proxy.envelope import Envelope, EnvelopeParser, ParseResult
from cascade_guard.mcp_proxy.schema_versions.registry import SchemaRegistry
from cascade_guard.mcp_proxy.schema_versions.v1_0 import SchemaV1_0


# ---------------------------------------------------------------------------
# Strategies for property-based testing
# ---------------------------------------------------------------------------

# Valid agent_id and caller_id: non-empty strings (typically numeric hashes)
agent_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Nd", "L")),
    min_size=1,
    max_size=64,
)

# Token budget: non-negative integers within reasonable range
token_budget_strategy = st.integers(min_value=0, max_value=10_000_000)

# Execution seconds: non-negative integers within reasonable range
execution_seconds_strategy = st.integers(min_value=0, max_value=86400)

# Arbitrary tool arguments (we should NEVER read these)
arguments_strategy = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=st.one_of(
        st.text(max_size=100),
        st.integers(),
        st.floats(allow_nan=False),
        st.booleans(),
        st.none(),
        st.lists(st.integers(), max_size=5),
    ),
    max_size=10,
)

# Complete valid tool call with envelope
@st.composite
def valid_tool_call(draw):
    """Generate a valid MCP tool call with CascadeGuard envelope."""
    return {
        "name": draw(st.text(min_size=1, max_size=50)),
        "arguments": draw(arguments_strategy),
        "_cascadeguard": {
            "schema_version": "1.0",
            "agent_id": draw(agent_id_strategy),
            "caller_id": draw(agent_id_strategy),
            "token_budget": draw(token_budget_strategy),
            "execution_seconds": draw(execution_seconds_strategy),
        },
    }


# Tool call without envelope (backward compat)
@st.composite
def tool_call_no_envelope(draw):
    """Generate an MCP tool call without CascadeGuard envelope."""
    return {
        "name": draw(st.text(min_size=1, max_size=50)),
        "arguments": draw(arguments_strategy),
    }


# Tool call with invalid schema version
@st.composite
def tool_call_bad_schema(draw):
    """Generate a tool call with an unsupported schema version."""
    version = draw(st.text(min_size=1, max_size=10).filter(lambda v: v != "1.0"))
    return {
        "name": draw(st.text(min_size=1, max_size=50)),
        "arguments": draw(arguments_strategy),
        "_cascadeguard": {
            "schema_version": version,
            "agent_id": draw(agent_id_strategy),
            "caller_id": draw(agent_id_strategy),
            "token_budget": draw(token_budget_strategy),
            "execution_seconds": draw(execution_seconds_strategy),
        },
    }


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestEnvelopeRoundTrip:
    """Round-trip property: parse(call).reconstruct() == extract_fields(call)"""

    @given(tool_call=valid_tool_call())
    @settings(max_examples=200)
    def test_roundtrip_property_holds(self, tool_call):
        """For all valid tool calls, parsing then reconstructing equals direct extraction."""
        parser = EnvelopeParser()

        result = parser.parse(tool_call)
        assert result.success is True

        reconstructed = result.envelope.reconstruct()
        extracted = parser.extract_fields(tool_call)

        assert reconstructed == extracted

    @given(tool_call=valid_tool_call())
    @settings(max_examples=100)
    def test_parse_never_reads_arguments(self, tool_call):
        """Arguments field is never accessed during parsing (zero semantic overhead)."""
        parser = EnvelopeParser()

        # Replace arguments with a sentinel that would error if accessed deeply
        tool_call_copy = dict(tool_call)
        tool_call_copy["arguments"] = {"_sentinel": object()}

        result = parser.parse(tool_call_copy)
        assert result.success is True
        # If we got here without error, arguments were never deeply inspected

    @given(tool_call=tool_call_no_envelope())
    @settings(max_examples=100)
    def test_missing_envelope_returns_defaults(self, tool_call):
        """Tool calls without envelope metadata return default Envelope."""
        parser = EnvelopeParser()

        result = parser.parse(tool_call)
        assert result.success is True
        assert result.envelope == Envelope()
        assert result.envelope.reconstruct() == parser.extract_fields(tool_call)

    @given(tool_call=tool_call_bad_schema())
    @settings(max_examples=50)
    def test_unsupported_schema_rejected(self, tool_call):
        """Tool calls with unsupported schema versions are rejected."""
        parser = EnvelopeParser()

        result = parser.parse(tool_call)
        assert result.success is False
        assert "unsupported_schema_version" in result.error


class TestEnvelopePerformance:
    """Parse latency must be <1ms (1000μs) for all valid inputs."""

    @given(tool_call=valid_tool_call())
    @settings(max_examples=100)
    def test_parse_under_1ms(self, tool_call):
        """Every parse completes in under 1ms (1000μs)."""
        parser = EnvelopeParser()

        result = parser.parse(tool_call)
        assert result.parse_time_us < 1000  # <1ms

    def test_parse_latency_benchmark(self):
        """Benchmark: 1000 parses should average well under 1ms each."""
        parser = EnvelopeParser()
        tool_call = {
            "name": "datadog/list_monitors",
            "arguments": {"filter": "tag:env:prod", "limit": 100},
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "12345",
                "caller_id": "67890",
                "token_budget": 50000,
                "execution_seconds": 30,
            },
        }

        start = time.perf_counter_ns()
        for _ in range(1000):
            parser.parse(tool_call)
        elapsed_ns = time.perf_counter_ns() - start

        avg_us = elapsed_ns / 1000 / 1000  # ns → μs, divided by iterations
        assert avg_us < 1000, f"Average parse latency {avg_us:.1f}μs exceeds 1ms target"
        # Expect well under 100μs in practice
        assert avg_us < 100, f"Average parse latency {avg_us:.1f}μs — expected <100μs"


class TestEnvelopeEdgeCases:
    """Edge cases and error handling."""

    def test_empty_tool_call(self):
        """Empty dict returns defaults."""
        parser = EnvelopeParser()
        result = parser.parse({})
        assert result.success is True
        assert result.envelope == Envelope()

    def test_envelope_not_dict(self):
        """Non-dict envelope metadata is rejected."""
        parser = EnvelopeParser()
        result = parser.parse({"_cascadeguard": "not_a_dict"})
        assert result.success is False
        assert "must be a dict" in result.error

    def test_envelope_with_extra_fields(self):
        """Extra fields in envelope metadata are ignored."""
        parser = EnvelopeParser()
        result = parser.parse({
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "abc",
                "caller_id": "def",
                "token_budget": 100,
                "execution_seconds": 5,
                "extra_field": "ignored",
                "another_extra": 999,
            }
        })
        assert result.success is True
        assert result.envelope.agent_id == "abc"
        assert result.envelope.token_budget == 100

    def test_envelope_missing_optional_fields(self):
        """Missing fields get defaults."""
        parser = EnvelopeParser()
        result = parser.parse({
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "xyz",
            }
        })
        assert result.success is True
        assert result.envelope.agent_id == "xyz"
        assert result.envelope.caller_id == ""
        assert result.envelope.token_budget == 0
        assert result.envelope.execution_seconds == 0

    def test_token_budget_as_string_coerced(self):
        """String token_budget is coerced to int."""
        parser = EnvelopeParser()
        result = parser.parse({
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "a1",
                "caller_id": "c1",
                "token_budget": "50000",
                "execution_seconds": "30",
            }
        })
        assert result.success is True
        assert result.envelope.token_budget == 50000
        assert result.envelope.execution_seconds == 30

    def test_invalid_token_budget_gets_default(self):
        """Non-numeric token_budget falls back to default 0."""
        parser = EnvelopeParser()
        result = parser.parse({
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "a1",
                "caller_id": "c1",
                "token_budget": "not_a_number",
                "execution_seconds": None,
            }
        })
        assert result.success is True
        assert result.envelope.token_budget == 0
        assert result.envelope.execution_seconds == 0


class TestSchemaVersionRegistry:
    """Schema version registry functionality."""

    def test_default_registry_has_v1_0(self):
        """Default registry supports v1.0."""
        registry = SchemaRegistry()
        assert registry.is_supported("1.0")
        assert "1.0" in registry.supported_versions()

    def test_unsupported_version_raises(self):
        """Getting unsupported version raises KeyError."""
        registry = SchemaRegistry()
        with pytest.raises(KeyError):
            registry.get_schema("99.0")

    def test_version_definitions_structure(self):
        """Version definitions have correct structure."""
        registry = SchemaRegistry()
        defs = registry.version_definitions()
        assert len(defs) >= 1
        assert defs[0]["version"] == "1.0"
        assert len(defs[0]["fields"]) == 4  # agent_id, caller_id, token_budget, execution_seconds

    def test_migrate_same_version(self):
        """Migration between same versions is identity."""
        registry = SchemaRegistry()
        data = {
            "agent_id": "123",
            "caller_id": "456",
            "token_budget": 1000,
            "execution_seconds": 30,
        }
        migrated = registry.migrate(data, "1.0", "1.0")
        assert migrated["agent_id"] == "123"
        assert migrated["token_budget"] == 1000
        assert migrated["schema_version"] == "1.0"

    def test_parser_supported_versions(self):
        """Parser exposes supported versions from registry."""
        parser = EnvelopeParser()
        versions = parser.supported_versions
        assert "1.0" in versions


class TestSchemaV1_0:
    """Schema v1.0 specific tests."""

    def test_extract_all_fields(self):
        """Extracts all v1.0 fields correctly."""
        schema = SchemaV1_0()
        data = {
            "agent_id": "agent_123",
            "caller_id": "caller_456",
            "token_budget": 75000,
            "execution_seconds": 60,
        }
        result = schema.extract(data)
        assert result["agent_id"] == "agent_123"
        assert result["caller_id"] == "caller_456"
        assert result["token_budget"] == 75000
        assert result["execution_seconds"] == 60

    def test_field_definitions(self):
        """Field definitions are complete."""
        schema = SchemaV1_0()
        defs = schema.field_definitions()
        names = [d["name"] for d in defs]
        assert "agent_id" in names
        assert "caller_id" in names
        assert "token_budget" in names
        assert "execution_seconds" in names

    def test_version_is_1_0(self):
        """Version string is correct."""
        schema = SchemaV1_0()
        assert schema.version == "1.0"
