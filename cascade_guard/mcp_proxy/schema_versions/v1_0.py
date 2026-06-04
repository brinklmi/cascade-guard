"""Envelope Schema v1.0 — initial schema specification.

Fields:
  - schema_version: str (always "1.0")
  - agent_id: str (identifier of source agent)
  - caller_id: str (identifier of caller chain)
  - token_budget: int (remaining token budget for this call)
  - execution_seconds: int (time budget for this call)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class FieldSpec:
    """Specification for a single envelope field."""

    name: str
    field_type: str  # "str" | "int"
    default: Any
    description: str


# V1.0 field definitions
V1_0_FIELDS: List[FieldSpec] = [
    FieldSpec(
        name="agent_id",
        field_type="str",
        default="",
        description="Identifier of the source agent",
    ),
    FieldSpec(
        name="caller_id",
        field_type="str",
        default="",
        description="Identifier of the caller chain",
    ),
    FieldSpec(
        name="token_budget",
        field_type="int",
        default=0,
        description="Remaining token budget for this invocation",
    ),
    FieldSpec(
        name="execution_seconds",
        field_type="int",
        default=0,
        description="Time budget in seconds for this invocation",
    ),
]


class SchemaV1_0:
    """Envelope schema version 1.0.

    Extracts integer routing fields from envelope metadata.
    Provides field definitions for introspection.
    """

    VERSION = "1.0"

    def __init__(self) -> None:
        self._fields = {f.name: f for f in V1_0_FIELDS}

    @property
    def version(self) -> str:
        return self.VERSION

    @property
    def fields(self) -> List[FieldSpec]:
        return list(V1_0_FIELDS)

    @property
    def field_names(self) -> List[str]:
        return [f.name for f in V1_0_FIELDS]

    def extract(self, envelope_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract fields from raw envelope data using v1.0 schema.

        Only reads declared fields. Unknown fields are ignored.
        Missing fields receive their default values.

        Args:
            envelope_data: Raw envelope metadata dict from tool call.

        Returns:
            Dict with extracted field values (typed appropriately).
        """
        result: Dict[str, Any] = {}

        for field_spec in V1_0_FIELDS:
            raw_value = envelope_data.get(field_spec.name, field_spec.default)

            if field_spec.field_type == "int":
                try:
                    result[field_spec.name] = int(raw_value) if raw_value is not None else field_spec.default
                except (ValueError, TypeError):
                    result[field_spec.name] = field_spec.default
            else:
                # str type
                result[field_spec.name] = str(raw_value) if raw_value is not None else field_spec.default

        return result

    def field_definitions(self) -> List[Dict[str, Any]]:
        """Return field definitions for introspection (used by cascadeguard/schema_versions tool).

        Returns:
            List of dicts with name, type, default, and description.
        """
        return [
            {
                "name": f.name,
                "type": f.field_type,
                "default": f.default,
                "description": f.description,
            }
            for f in V1_0_FIELDS
        ]
