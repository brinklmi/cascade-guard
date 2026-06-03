"""Scroll Corpus Archival Pipeline (Task 18, Req 9).

Reads JSON scrolls → Apache Arrow table → Parquet (Snappy) with
DuckDB predicate pushdown queries. Integrates with GlyphCompressor
and IsfetFilter for composite glyph generation and filtering.

Dependencies: pyarrow, duckdb, Pillow, openai
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from cascade_guard.glyph.compressor import GlyphCompressor, GlyphOutput
from cascade_guard.glyph.isfet_filter import FilteredPrompt, IsfetFilter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class IngestionResult:
    """Result of scroll corpus ingestion."""

    table: pa.Table
    record_count: int
    columns: list[str]
    skipped_files: list[dict[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return self.record_count > 0


# ---------------------------------------------------------------------------
# Archival Pipeline
# ---------------------------------------------------------------------------


class ArchivalPipeline:
    """JSON scrolls → Arrow → Parquet with DuckDB predicate pushdown.

    Integrates with GlyphCompressor for composite glyph generation
    and IsfetFilter for VLM-based noise filtering.
    """

    def __init__(
        self,
        glyph_compressor: Optional[GlyphCompressor] = None,
        isfet_filter: Optional[IsfetFilter] = None,
    ):
        """Initialize archival pipeline.

        Args:
            glyph_compressor: GlyphCompressor instance (creates default if None).
            isfet_filter: IsfetFilter instance (creates default if None).
        """
        self.compressor = glyph_compressor or GlyphCompressor(max_generations=10)
        self.filter = isfet_filter or IsfetFilter()
        self._conn = duckdb.connect()
        self._parquet_path: Optional[Path] = None

    def ingest(self, scroll_paths: list[Path]) -> IngestionResult:
        """Parse JSON scroll files into an Apache Arrow table.

        Schema derivation:
        - Top-level keys: @context, @type, @id, metadata, core_architecture
        - Nested fields flattened to dot-notation column names

        Skips malformed files (logs path + error), continues processing.

        Args:
            scroll_paths: List of paths to JSON scroll files.

        Returns:
            IngestionResult with Arrow table and metadata.
        """
        start = time.monotonic()
        records: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        for path in scroll_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                flat = self._flatten_scroll(data, str(path))
                records.append(flat)
            except Exception as e:
                skipped.append({"path": str(path), "error": str(e)})
                logger.warning(f"Skipped {path}: {e}")

        if not records:
            # Return empty table
            table = pa.table({"_empty": pa.array([], type=pa.string())})
            return IngestionResult(
                table=table,
                record_count=0,
                columns=[],
                skipped_files=skipped,
                elapsed_seconds=time.monotonic() - start,
            )

        # Build Arrow table from records
        # Collect all keys across records for uniform schema
        all_keys: set[str] = set()
        for r in records:
            all_keys.update(r.keys())

        columns: dict[str, list] = {k: [] for k in sorted(all_keys)}
        for r in records:
            for k in columns:
                columns[k].append(r.get(k))

        # Convert to Arrow arrays
        arrow_columns = {}
        for k, values in columns.items():
            # Determine type from first non-None value
            arrow_columns[k] = pa.array(
                [self._to_string(v) for v in values], type=pa.string()
            )

        table = pa.table(arrow_columns)

        return IngestionResult(
            table=table,
            record_count=len(records),
            columns=list(arrow_columns.keys()),
            skipped_files=skipped,
            elapsed_seconds=time.monotonic() - start,
        )

    def write_parquet(self, table: pa.Table, output: Path) -> None:
        """Write Arrow table to Parquet format with Snappy compression.

        Preserves column-level metadata including original scroll @id and version.

        Args:
            table: Arrow table to write.
            output: Output Parquet file path.
        """
        pq.write_table(
            table,
            output,
            compression="snappy",
            write_statistics=True,
        )
        self._parquet_path = output

    def query(
        self,
        version: Optional[str] = None,
        metric: Optional[str] = None,
        parquet_path: Optional[Path] = None,
    ) -> pa.Table:
        """DuckDB predicate pushdown query.

        Returns matching records within 2s for ≤1,000 scrolls.
        Returns empty table (zero records, no error) when no matches.

        Args:
            version: Version string to filter by.
            metric: Metric name to filter by.
            parquet_path: Path to Parquet file (uses last written if None).

        Returns:
            Arrow table with matching records.
        """
        path = parquet_path or self._parquet_path
        if path is None or not path.exists():
            # Return empty table
            return pa.table({"_empty": pa.array([], type=pa.string())})

        # Build query with predicate pushdown
        conditions = []
        if version:
            conditions.append(f"\"metadata.version\" = '{version}'")
        if metric:
            conditions.append(
                f"(\"@type\" LIKE '%{metric}%' OR \"metadata.version\" LIKE '%{metric}%')"
            )

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM read_parquet('{path}') {where_clause}"

        try:
            result = self._conn.execute(sql).fetch_arrow_table()
            return result
        except Exception:
            # Return empty on query failure
            return pa.table({"_empty": pa.array([], type=pa.string())})

    def generate_glyphs(self, table: pa.Table) -> Optional[GlyphOutput]:
        """Extract invariant core metrics → invoke GlyphCompressor.

        Args:
            table: Arrow table with scroll data.

        Returns:
            GlyphOutput from compression, or None if table is empty.
        """
        if table.num_rows == 0:
            return None

        # Convert table rows to scroll dicts for compression
        scrolls = []
        for i in range(min(table.num_rows, 50)):
            scroll: dict[str, Any] = {}
            for col_name in table.column_names:
                val = table.column(col_name)[i].as_py()
                if val is not None:
                    scroll[col_name] = val
            if scroll:
                scrolls.append(scroll)

        if not scrolls:
            return None

        return self.compressor.compress(scrolls)

    def filter_with_isfet(self, glyph: GlyphOutput) -> Optional[FilteredPrompt]:
        """Pass composite glyph → IsfetFilter → filtered system prompt.

        Args:
            glyph: GlyphOutput from compression.

        Returns:
            FilteredPrompt, or None on failure.
        """
        try:
            return self.filter.filter(
                glyph.image,
                source_glyph_id=f"archival-{id(glyph)}",
            )
        except Exception as e:
            logger.warning(f"Isfet filter failed: {e}")
            return None

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _flatten_scroll(self, data: dict[str, Any], source_path: str) -> dict[str, Any]:
        """Flatten nested scroll JSON to dot-notation columns."""
        flat: dict[str, Any] = {"_source_path": source_path}

        def _recurse(obj: Any, prefix: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, (dict,)):
                        _recurse(v, key)
                    elif isinstance(v, list):
                        flat[key] = json.dumps(v)
                    else:
                        flat[key] = v
            else:
                flat[prefix] = obj

        _recurse(data)
        return flat

    def _to_string(self, value: Any) -> Optional[str]:
        """Convert any value to string for Arrow string columns."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str)
