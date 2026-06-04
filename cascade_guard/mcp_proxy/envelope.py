"""Envelope Parser — zero semantic overhead extraction of integer routing fields.

Reads only: agent_id, caller_id, token_budget, execution_seconds.
Never touches tool call arguments or payload content.
Round-trip property: parse(call).reconstruct() == extract_fields(call)

Performance target: <1ms per parse under normal conditions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .schema_versions.registry import SchemaRegistry, SchemaVersion


@dataclass(frozen=True)
class Envelope:
    """Immutable envelope extracted from an MCP tool call.

    Contains only integer routing metadata — never touches payload content.
    """

    schema_version: str = "1.0"
    agent_id: str = ""
    caller_id: str = ""
    token_budget: int = 0
    execution_seconds: int = 0

    def reconstruct(self) -> Dict[str, Any]:
        """Reconstruct envelope fields as a dictionary.

        Round-trip invariant: parse(call).reconstruct() == extract_fields(call)
        """
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "caller_id": self.caller_id,
            "token_budget": self.token_budget,
            "execution_seconds": self.execution_seconds,
        }


@dataclass
class ParseResult:
    """Result of parsing an envelope from an MCP tool call."""

    envelope: Envelope
    success: bool = True
    error: Optional[str] = None
    parse_time_us: int = 0  # Microseconds taken to parse


class EnvelopeParser:
    """Zero-overhead envelope parser for MCP tool calls.

    Extracts only integer routing fields from tool call metadata.
    Never reads, parses, tokenizes, or evaluates tool call arguments.

    Thread-safe: all state is in the SchemaRegistry (immutable after init).
    """

    # Metadata key where envelope fields are expected in the MCP tool call
    ENVELOPE_KEY = "_cascadeguard"

    def __init__(self, schema_registry: Optional[SchemaRegistry] = None):
        """Initialize parser with optional schema registry.

        Args:
            schema_registry: Registry of supported schema versions.
                             If None, creates default registry with v1.0 only.
        """
        self._registry = schema_registry or SchemaRegistry()

    @property
    def supported_versions(self) -> List[str]:
        """List of supported schema version strings."""
        return self._registry.supported_versions()

    def parse(self, tool_call: Dict[str, Any]) -> ParseResult:
        """Parse envelope fields from an MCP tool call.

        Extracts ONLY the integer routing metadata from the tool call.
        Never touches the 'arguments' field or any payload content.

        Args:
            tool_call: The raw MCP tools/call request dict. Expected structure:
                {
                    "name": "tool_name",
                    "arguments": { ... },  # NEVER READ
                    "_cascadeguard": {      # Envelope metadata
                        "schema_version": "1.0",
                        "agent_id": "12345",
                        "caller_id": "67890",
                        "token_budget": 50000,
                        "execution_seconds": 30
                    }
                }

        Returns:
            ParseResult with the extracted Envelope and timing metadata.
        """
        start = time.perf_counter_ns()

        # Extract envelope metadata block — never touch 'arguments'
        envelope_data = tool_call.get(self.ENVELOPE_KEY)

        if envelope_data is None:
            # No envelope metadata — return defaults (backward compat)
            elapsed_us = (time.perf_counter_ns() - start) // 1000
            return ParseResult(
                envelope=Envelope(),
                success=True,
                error=None,
                parse_time_us=elapsed_us,
            )

        if not isinstance(envelope_data, dict):
            elapsed_us = (time.perf_counter_ns() - start) // 1000
            return ParseResult(
                envelope=Envelope(),
                success=False,
                error=f"Envelope metadata must be a dict, got {type(envelope_data).__name__}",
                parse_time_us=elapsed_us,
            )

        # Detect schema version
        schema_version = str(envelope_data.get("schema_version", "1.0"))

        # Validate schema version is supported
        if not self._registry.is_supported(schema_version):
            elapsed_us = (time.perf_counter_ns() - start) // 1000
            return ParseResult(
                envelope=Envelope(),
                success=False,
                error=f"unsupported_schema_version: '{schema_version}'. Supported: {self.supported_versions}",
                parse_time_us=elapsed_us,
            )

        # Get the schema spec and extract fields
        schema = self._registry.get_schema(schema_version)
        extracted = schema.extract(envelope_data)

        envelope = Envelope(
            schema_version=schema_version,
            agent_id=str(extracted.get("agent_id", "")),
            caller_id=str(extracted.get("caller_id", "")),
            token_budget=int(extracted.get("token_budget", 0)),
            execution_seconds=int(extracted.get("execution_seconds", 0)),
        )

        elapsed_us = (time.perf_counter_ns() - start) // 1000

        return ParseResult(
            envelope=envelope,
            success=True,
            error=None,
            parse_time_us=elapsed_us,
        )

    def extract_fields(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        """Extract raw envelope fields from a tool call as a dict.

        This is the reference extraction used for round-trip verification.
        Returns the same structure that Envelope.reconstruct() should produce.

        Args:
            tool_call: The raw MCP tools/call request dict.

        Returns:
            Dict of envelope fields (or defaults if no envelope present).
        """
        envelope_data = tool_call.get(self.ENVELOPE_KEY)

        if envelope_data is None or not isinstance(envelope_data, dict):
            return Envelope().reconstruct()

        schema_version = str(envelope_data.get("schema_version", "1.0"))

        if not self._registry.is_supported(schema_version):
            return Envelope().reconstruct()

        schema = self._registry.get_schema(schema_version)
        extracted = schema.extract(envelope_data)

        return {
            "schema_version": schema_version,
            "agent_id": str(extracted.get("agent_id", "")),
            "caller_id": str(extracted.get("caller_id", "")),
            "token_budget": int(extracted.get("token_budget", 0)),
            "execution_seconds": int(extracted.get("execution_seconds", 0)),
        }
