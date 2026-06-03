"""Scroll Corpus Archival Pipeline.

JSON scrolls → Apache Arrow → Parquet with DuckDB predicate pushdown.
Integrates with Glyph VLM Compressor and Isfet Filter.

Dependencies: pyarrow, duckdb, Pillow, openai
"""

from cascade_guard.archival.pipeline import ArchivalPipeline, IngestionResult

__all__ = ["ArchivalPipeline", "IngestionResult"]
