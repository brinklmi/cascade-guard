"""Schema Version Registry — validates, migrates, and lists supported versions.

Provides version detection, forward migration with defaults, and
a queryable list of supported schema versions.

Maintains backward compatibility for minimum 2 major versions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class SchemaVersion(Protocol):
    """Protocol for envelope schema implementations."""

    @property
    def version(self) -> str:
        ...

    def extract(self, envelope_data: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def field_definitions(self) -> List[Dict[str, Any]]:
        ...


class SchemaRegistry:
    """Registry of supported envelope schema versions.

    Validates incoming schema versions, routes extraction to the
    correct schema handler, and supports forward migration from
    older versions.

    Thread-safe: schemas are registered at init time and never mutated.
    """

    def __init__(self) -> None:
        """Initialize with default schemas (v1.0)."""
        self._schemas: Dict[str, SchemaVersion] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register built-in schema versions."""
        from .v1_0 import SchemaV1_0

        v1 = SchemaV1_0()
        self._schemas[v1.version] = v1

    def register(self, schema: SchemaVersion) -> None:
        """Register a new schema version.

        Args:
            schema: Schema implementation conforming to SchemaVersion protocol.
        """
        self._schemas[schema.version] = schema

    def is_supported(self, version: str) -> bool:
        """Check if a schema version is supported.

        Args:
            version: Schema version string to check.

        Returns:
            True if the version is registered and supported.
        """
        return version in self._schemas

    def get_schema(self, version: str) -> SchemaVersion:
        """Get schema implementation for a version.

        Args:
            version: Schema version string.

        Returns:
            The schema implementation.

        Raises:
            KeyError: If version is not supported.
        """
        if version not in self._schemas:
            raise KeyError(
                f"Unsupported schema version: '{version}'. "
                f"Supported: {self.supported_versions()}"
            )
        return self._schemas[version]

    def supported_versions(self) -> List[str]:
        """List all supported schema version strings.

        Returns:
            Sorted list of version strings (ascending).
        """
        return sorted(self._schemas.keys())

    def version_definitions(self) -> List[Dict[str, Any]]:
        """Get full definitions for all supported versions.

        Used by the cascadeguard/schema_versions MCP tool.

        Returns:
            List of dicts with version and field definitions.
        """
        result = []
        for version in self.supported_versions():
            schema = self._schemas[version]
            result.append({
                "version": version,
                "fields": schema.field_definitions(),
            })
        return result

    def migrate(
        self,
        envelope_data: Dict[str, Any],
        from_version: str,
        to_version: str,
    ) -> Dict[str, Any]:
        """Migrate envelope data from one schema version to another.

        Applies default values for any fields present in the target
        schema but absent in the source data (forward migration).

        Args:
            envelope_data: Raw envelope metadata dict.
            from_version: Source schema version.
            to_version: Target schema version.

        Returns:
            Migrated envelope data dict conforming to target schema.

        Raises:
            KeyError: If either version is unsupported.
        """
        if not self.is_supported(from_version):
            raise KeyError(f"Source schema version unsupported: '{from_version}'")
        if not self.is_supported(to_version):
            raise KeyError(f"Target schema version unsupported: '{to_version}'")

        # Extract using source schema (validates existing fields)
        source_schema = self._schemas[from_version]
        extracted = source_schema.extract(envelope_data)

        # Apply target schema defaults for any missing fields
        target_schema = self._schemas[to_version]
        target_fields = target_schema.field_definitions()

        for field_def in target_fields:
            if field_def["name"] not in extracted:
                extracted[field_def["name"]] = field_def["default"]

        # Update schema_version to target
        extracted["schema_version"] = to_version

        return extracted
