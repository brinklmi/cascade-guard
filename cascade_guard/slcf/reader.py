"""SLCF Reader — reads SLCF files with footer-first validation.

Implements footer-first validation sequence:
1. Read footer (last N bytes)
2. Validate Maat hash (SHA-256 over page content)
3. Check Σδ(a) ≤ 2
4. Validate κ_eff in [0.0, 1.0]
5. Verify L3 page index exists

Supports predicate pushdown via L3 page index.
Supports L6 (Glyph) and L7 (Isfet) layer extraction when present.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cascade_guard.slcf.format import (
    SLCF_MAGIC,
    SLCF_VERSION,
    PageIndexEntry,
    SLCFFooter,
    SLCFLayer,
    SLCFPage,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SLCFIntegrityError(Exception):
    """Maat hash mismatch — file integrity compromised."""
    pass


class SLCFComplianceError(Exception):
    """Σδ > 2 — file is non-compliant."""
    pass


class SLCFStructureError(Exception):
    """Missing or corrupt L3 page index."""
    pass


class SLCFValidationError(Exception):
    """κ_eff out of valid range [0.0, 1.0]."""
    pass


# ---------------------------------------------------------------------------
# L6/L7 Extracted Data Structures
# ---------------------------------------------------------------------------


@dataclass
class GlyphLayerData:
    """Extracted L6 Glyph layer data."""

    image_bytes: bytes  # Raw PNG bytes
    format: str  # "png"
    width: int
    height: int
    compression_ratio: float
    geometry_type: str
    original_tokens: int
    compressed_tokens: int
    signature_hash: str
    element_count: int
    unclassified: bool
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_pil_image(self) -> Any:
        """Convert stored PNG bytes back to a PIL Image.

        Returns:
            PIL.Image.Image instance.
        """
        from PIL import Image
        return Image.open(io.BytesIO(self.image_bytes))


@dataclass
class IsfetLayerData:
    """Extracted L7 Isfet layer data."""

    text: str  # Filtered system prompt
    token_count: int
    metrics_extracted: list[str] = field(default_factory=list)
    geometries_extracted: list[str] = field(default_factory=list)
    source_glyph_id: str = ""
    vlm_raw_length: int = 0
    filtered_length: int = 0


# ---------------------------------------------------------------------------
# SLCF File Handle
# ---------------------------------------------------------------------------


class SLCFFile:
    """Represents an opened and validated SLCF file.

    After validation, provides access to pages and metadata.
    """

    def __init__(
        self,
        footer: SLCFFooter,
        pages: list[SLCFPage],
        raw_data: bytes,
    ):
        self.footer = footer
        self.pages = pages
        self._raw_data = raw_data

    @property
    def beta_one(self) -> int:
        """β₁ cycle count from footer metadata."""
        return self.footer.beta_one

    @property
    def kappa_effective(self) -> float:
        """κ_effective from footer metadata."""
        return self.footer.kappa_effective

    @property
    def sigma_delta(self) -> float:
        """Cumulative deficiency from footer."""
        return self.footer.sigma_delta

    @property
    def flow_state(self) -> str:
        """Metabolic flow state (laminar/turbulent/retreat)."""
        return self.footer.flow_state

    @property
    def requires_retreat(self) -> bool:
        """Whether κ_eff < 0.3 — system should enter preservation mode."""
        return self.footer.requires_retreat

    def get_pages_by_layer(self, layer: SLCFLayer) -> list[SLCFPage]:
        """Get all pages for a specific layer."""
        return [p for p in self.pages if p.layer_id == layer]

    def get_page_data(self, page: SLCFPage) -> dict[str, Any]:
        """Decode page data as JSON dict.

        Note: For L6 (Glyph) pages, use get_glyph_data() instead —
        L6 pages have a binary layout (metadata_len + metadata + image_bytes)
        that is not pure JSON.
        """
        return json.loads(page.data.decode("utf-8"))

    @property
    def has_glyph(self) -> bool:
        """Whether this SLCF file contains an L6 Glyph layer."""
        return any(p.layer_id == SLCFLayer.L6_GLYPH for p in self.pages)

    @property
    def has_isfet(self) -> bool:
        """Whether this SLCF file contains an L7 Isfet layer."""
        return any(p.layer_id == SLCFLayer.L7_ISFET for p in self.pages)

    def get_glyph_data(self) -> Optional[GlyphLayerData]:
        """Extract L6 Glyph layer data (composite image + metrics).

        Returns:
            GlyphLayerData with PNG bytes and metrics, or None if L6 not present.
        """
        l6_pages = self.get_pages_by_layer(SLCFLayer.L6_GLYPH)
        if not l6_pages:
            return None

        page = l6_pages[0]
        data = page.data

        # Parse: metadata_len(4) + metadata_json + image_bytes
        if len(data) < 4:
            return None

        metadata_len = struct.unpack("<I", data[:4])[0]
        metadata_bytes = data[4:4 + metadata_len]
        image_bytes = data[4 + metadata_len:]

        metadata = json.loads(metadata_bytes.decode("utf-8"))

        return GlyphLayerData(
            image_bytes=image_bytes,
            format=metadata.get("format", "png"),
            width=metadata.get("width", 0),
            height=metadata.get("height", 0),
            compression_ratio=metadata.get("compression_ratio", 0.0),
            geometry_type=metadata.get("geometry_type", "hexagon"),
            original_tokens=metadata.get("original_tokens", 0),
            compressed_tokens=metadata.get("compressed_tokens", 0),
            signature_hash=metadata.get("signature_hash", ""),
            element_count=metadata.get("element_count", 0),
            unclassified=metadata.get("unclassified", False),
            metrics=metadata.get("metrics", {}),
        )

    def get_isfet_data(self) -> Optional[IsfetLayerData]:
        """Extract L7 Isfet layer data (filtered signal prompt).

        Returns:
            IsfetLayerData with prompt text and extraction metadata,
            or None if L7 not present.
        """
        l7_pages = self.get_pages_by_layer(SLCFLayer.L7_ISFET)
        if not l7_pages:
            return None

        page = l7_pages[0]
        record = json.loads(page.data.decode("utf-8"))

        return IsfetLayerData(
            text=record.get("text", ""),
            token_count=record.get("token_count", 0),
            metrics_extracted=record.get("metrics_extracted", []),
            geometries_extracted=record.get("geometries_extracted", []),
            source_glyph_id=record.get("source_glyph_id", ""),
            vlm_raw_length=record.get("vlm_raw_length", 0),
            filtered_length=record.get("filtered_length", 0),
        )


# ---------------------------------------------------------------------------
# SLCF Reader
# ---------------------------------------------------------------------------


class SLCFReader:
    """Reads SLCF files with footer-first validation.

    Validation is performed before any page data is exposed.
    Supports predicate pushdown via L3 page index.
    """

    def open(self, data: bytes) -> SLCFFile:
        """Open and validate SLCF binary data.

        Performs footer-first validation:
        1. Read footer from end of file
        2. Validate Maat hash (SHA-256 over all page bytes)
        3. Check Σδ(a) ≤ 2
        4. Validate κ_eff ∈ [0.0, 1.0]
        5. Verify L3 page index exists

        Args:
            data: Complete SLCF binary data.

        Returns:
            Validated SLCFFile handle.

        Raises:
            SLCFIntegrityError: Maat hash mismatch.
            SLCFComplianceError: Σδ > 2.
            SLCFValidationError: κ_eff out of range.
            SLCFStructureError: Missing L3 page index.
        """
        # 1. Read footer
        footer = self._read_footer(data)

        # 2. Validate Maat hash
        page_bytes = self._extract_page_bytes(data)
        computed_hash = hashlib.sha256(page_bytes).digest()
        if computed_hash != footer.maat_hash:
            raise SLCFIntegrityError(
                f"Maat hash mismatch: expected {footer.maat_hash.hex()}, "
                f"computed {computed_hash.hex()}"
            )

        # 3. Check Σδ(a) ≤ 2
        if footer.sigma_delta > 2.0:
            raise SLCFComplianceError(
                f"File non-compliant: Σδ(a) = {footer.sigma_delta:.6f} exceeds 2.0"
            )

        # 4. Validate κ_eff in [0.0, 1.0]
        if not (0.0 <= footer.kappa_effective <= 1.0):
            raise SLCFValidationError(
                f"κ_effective = {footer.kappa_effective} is outside valid range [0.0, 1.0]"
            )

        # 5. Verify L3 page index exists
        has_l3 = any(
            entry.layer_id == SLCFLayer.L3_AGENTIC_RUNTIME
            for entry in footer.page_index
        )
        if not has_l3:
            raise SLCFStructureError(
                "Missing L3 (Agentic Runtime) page index — corrupt structure"
            )

        # Parse pages
        pages = self._parse_pages(data, footer.page_index)

        return SLCFFile(footer=footer, pages=pages, raw_data=data)

    def open_file(self, path: Path) -> SLCFFile:
        """Open and validate an SLCF file from disk.

        Args:
            path: Path to the SLCF file.

        Returns:
            Validated SLCFFile handle.
        """
        data = path.read_bytes()
        return self.open(data)

    def query(self, slcf_file: SLCFFile, predicate: dict[str, Any]) -> list[SLCFPage]:
        """Predicate pushdown — load only matching pages via L3 index.

        Args:
            slcf_file: Validated SLCF file handle.
            predicate: Query predicate dict with keys to match against
                      L3 agentic runtime data.

        Returns:
            List of matching pages. Empty list if no pages match
            (zero pages loaded into memory).
        """
        if not predicate:
            return slcf_file.get_pages_by_layer(SLCFLayer.L3_AGENTIC_RUNTIME)

        # Get L3 pages and filter by predicate
        l3_pages = slcf_file.get_pages_by_layer(SLCFLayer.L3_AGENTIC_RUNTIME)
        matching = []

        for page in l3_pages:
            page_data = slcf_file.get_page_data(page)
            if self._matches_predicate(page_data, predicate):
                matching.append(page)

        return matching

    def get_beta_one(self, slcf_file: SLCFFile) -> int:
        """Extract β₁ for Union-Find cycle hint seeding.

        Args:
            slcf_file: Validated SLCF file handle.

        Returns:
            β₁ (1-cycle count) from footer metadata.
        """
        return slcf_file.beta_one

    def get_kappa_effective(self, slcf_file: SLCFFile) -> float:
        """Extract κ_eff for metabolic fuse initialization.

        Args:
            slcf_file: Validated SLCF file handle.

        Returns:
            κ_effective from footer metadata.
        """
        return slcf_file.kappa_effective

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _read_footer(self, data: bytes) -> SLCFFooter:
        """Read footer from end of SLCF binary data."""
        # Last 4 bytes = footer length
        footer_len = struct.unpack("<I", data[-4:])[0]
        footer_bytes = data[-(4 + footer_len):-4]
        footer_dict = json.loads(footer_bytes.decode("utf-8"))

        page_index = [
            PageIndexEntry(
                page_id=entry["page_id"],
                layer_id=SLCFLayer(entry["layer_id"]),
                offset=entry["offset"],
                size=entry["size"],
            )
            for entry in footer_dict.get("page_index", [])
        ]

        return SLCFFooter(
            maat_hash=bytes.fromhex(footer_dict["maat_hash"]),
            sigma_delta=footer_dict["sigma_delta"],
            kappa_effective=footer_dict["kappa_effective"],
            beta_one=footer_dict["beta_one"],
            flow_state=footer_dict.get("flow_state", "laminar"),
            page_index=page_index,
            target_size=footer_dict.get("target_size", 64 * 1024 * 1024),
            actual_size=footer_dict.get("actual_size", 0),
            encryption_fields=footer_dict.get("encryption_fields", {}),
        )

    def _extract_page_bytes(self, data: bytes) -> bytes:
        """Extract raw page bytes (everything between header and footer)."""
        # Header: magic(4) + version(2) + flags(2) = 8 bytes
        header_size = len(SLCF_MAGIC) + 4
        # Footer: footer_data + footer_len(4)
        footer_len = struct.unpack("<I", data[-4:])[0]
        footer_total = footer_len + 4
        return data[header_size:-footer_total]

    def _parse_pages(self, data: bytes, page_index: list[PageIndexEntry]) -> list[SLCFPage]:
        """Parse pages from binary data using page index."""
        import zstandard

        pages = []
        decompressor = zstandard.ZstdDecompressor()

        for entry in page_index:
            page_binary = data[entry.offset:entry.offset + entry.size]
            # Page header: layer_id(1) + page_id(4) + row_count(4) + data_len(4) + compression(1) = 14 bytes
            if len(page_binary) < 14:
                continue
            layer_id, page_id, row_count, data_len, compression_flag = struct.unpack(
                "<BIIIB", page_binary[:14]
            )
            page_data = page_binary[14:14 + data_len]

            # Decompress if needed
            if compression_flag == 1:
                page_data = decompressor.decompress(page_data)

            pages.append(SLCFPage(
                layer_id=SLCFLayer(layer_id),
                page_id=page_id,
                row_count=row_count,
                data=page_data,
            ))

        return pages

    def _matches_predicate(self, page_data: dict[str, Any], predicate: dict[str, Any]) -> bool:
        """Check if page data matches a predicate."""
        for key, value in predicate.items():
            # Check if any record in page_data matches
            for record_id, record in page_data.items():
                if isinstance(record, dict) and record.get(key) == value:
                    return True
        return False
