"""PDF→SLCF Ingestor — combines multiple PDFs into a single SLCF file.

Maps PDF content to the 7-layer SLCF model:
  L1 Substrate: Raw page byte sizes and image references
  L2 Resource Tethering: Extracted text per page
  L3 Agentic Runtime: Document structure (sections, paragraphs — queryable index)
  L4 Narrative: Semantic content (full text with context)
  L5 Governance: Provenance metadata (source filename, page number, timestamps)
  L6 Glyph: Composite VLM-compressed image (GlyphCompressor output)
  L7 Isfet: Filtered signal prompt (IsfetFilter output)

Supports predicate pushdown by page number, document name, or section heading.

Dependencies: pymupdf (fitz), Pillow
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cascade_guard.glyph.compressor import GlyphCompressor, GlyphOutput
from cascade_guard.glyph.isfet_filter import FilteredPrompt, IsfetFilter
from cascade_guard.slcf.format import (
    DEFAULT_TARGET_SIZE,
    DeficiencyTracker,
    PageIndexEntry,
    SLCFFooter,
    SLCFLayer,
    SLCFPage,
    compute_beta_one,
    compute_kappa_effective,
    compute_maat_hash,
)
from cascade_guard.slcf.writer import SLCFWriter


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class PDFPageRecord:
    """Extracted data from a single PDF page."""

    source_file: str
    page_number: int  # 0-indexed
    text: str = ""
    byte_size: int = 0
    sections: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    word_count: int = 0
    char_count: int = 0
    has_images: bool = False
    image_count: int = 0
    extraction_timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PDFDocument:
    """Metadata and pages from a single PDF."""

    filename: str
    path: str
    page_count: int = 0
    title: str = ""
    author: str = ""
    subject: str = ""
    creation_date: str = ""
    total_chars: int = 0
    total_words: int = 0
    pages: list[PDFPageRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PDFIngestionError(Exception):
    """Raised when PDF ingestion fails."""
    pass


# ---------------------------------------------------------------------------
# Section Detection
# ---------------------------------------------------------------------------

# Heuristic: lines that are short, capitalized, or match heading patterns
_HEADING_PATTERNS = [
    re.compile(r"^\d+\.?\s+[A-Z]"),           # "1. Introduction" or "1 OVERVIEW"
    re.compile(r"^[A-Z][A-Z\s]{3,}$"),         # "INTRODUCTION"
    re.compile(r"^(?:Chapter|Section|Part)\s+", re.IGNORECASE),
    re.compile(r"^[IVXLC]+\.\s+"),             # "III. Methods"
]


def _detect_sections(text: str) -> list[str]:
    """Detect section headings from extracted text."""
    sections = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        for pattern in _HEADING_PATTERNS:
            if pattern.match(stripped):
                sections.append(stripped)
                break
    return sections


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs (double-newline separated)."""
    raw = re.split(r"\n\s*\n", text)
    return [p.strip() for p in raw if p.strip() and len(p.strip()) > 20]


# ---------------------------------------------------------------------------
# PDF Ingestor
# ---------------------------------------------------------------------------


class PDFIngestor:
    """Ingests multiple PDF files into a single SLCF binary.

    Usage:
        ingestor = PDFIngestor()
        result = ingestor.ingest([Path("doc1.pdf"), Path("doc2.pdf")])
        # result.binary is the SLCF bytes (L1–L7)
        # result.documents has per-document metadata
        # result.glyph_output has the L6 composite image
        # result.filtered_prompt has the L7 filtered signal
    """

    def __init__(
        self,
        target_size_bytes: int = DEFAULT_TARGET_SIZE,
        glyph_compressor: Optional[GlyphCompressor] = None,
        isfet_filter: Optional[IsfetFilter] = None,
        enable_l6: bool = True,
        enable_l7: bool = True,
    ):
        """Initialize PDF ingestor.

        Args:
            target_size_bytes: Target Row_Group size for κ_eff (default 64MB).
            glyph_compressor: GlyphCompressor instance (creates default if None).
            isfet_filter: IsfetFilter instance (creates default if None).
            enable_l6: Whether to generate L6 Glyph layer (default True).
            enable_l7: Whether to generate L7 Isfet layer (default True).
        """
        self.target_size = target_size_bytes
        self.compressor = glyph_compressor or GlyphCompressor(max_generations=10)
        self.filter = isfet_filter or IsfetFilter()
        self.enable_l6 = enable_l6
        self.enable_l7 = enable_l7

    def ingest(self, pdf_paths: list[Path]) -> "PDFIngestionResult":
        """Ingest multiple PDFs into a single SLCF file.

        Args:
            pdf_paths: List of paths to PDF files.

        Returns:
            PDFIngestionResult with binary data and metadata.

        Raises:
            PDFIngestionError: If no valid PDFs could be processed.
        """
        import fitz  # pymupdf

        documents: list[PDFDocument] = []
        all_pages: list[PDFPageRecord] = []
        errors: list[dict[str, str]] = []

        for path in pdf_paths:
            try:
                doc = self._extract_document(path, fitz)
                documents.append(doc)
                all_pages.extend(doc.pages)
            except Exception as e:
                errors.append({"file": str(path), "error": str(e)})

        if not all_pages:
            raise PDFIngestionError(
                f"No valid PDF pages extracted from {len(pdf_paths)} files. "
                f"Errors: {errors}"
            )

        # Generate L6: Glyph compression
        glyph_output: Optional[GlyphOutput] = None
        if self.enable_l6:
            glyph_output = self._generate_glyph(all_pages)

        # Generate L7: Isfet-filtered signal
        filtered_prompt: Optional[FilteredPrompt] = None
        if self.enable_l7 and glyph_output is not None:
            filtered_prompt = self._generate_isfet(glyph_output)

        # Build SLCF with all layers
        binary = self._build_slcf(documents, all_pages, glyph_output, filtered_prompt)

        return PDFIngestionResult(
            binary=binary,
            documents=documents,
            total_pages=len(all_pages),
            total_documents=len(documents),
            errors=errors,
            glyph_output=glyph_output,
            filtered_prompt=filtered_prompt,
        )

    def ingest_to_file(self, pdf_paths: list[Path], output: Path) -> "PDFIngestionResult":
        """Ingest PDFs and write SLCF to disk.

        Args:
            pdf_paths: List of PDF file paths.
            output: Output path for the .slcf file.

        Returns:
            PDFIngestionResult with metadata.
        """
        result = self.ingest(pdf_paths)
        output.write_bytes(result.binary)
        return result

    def _extract_document(self, path: Path, fitz_module) -> PDFDocument:
        """Extract all content from a single PDF."""
        doc = fitz_module.open(str(path))

        pdf_doc = PDFDocument(
            filename=path.name,
            path=str(path),
            page_count=len(doc),
            title=doc.metadata.get("title", "") or "",
            author=doc.metadata.get("author", "") or "",
            subject=doc.metadata.get("subject", "") or "",
            creation_date=doc.metadata.get("creationDate", "") or "",
        )

        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            images = page.get_images(full=True)

            record = PDFPageRecord(
                source_file=path.name,
                page_number=page_num,
                text=text,
                byte_size=len(text.encode("utf-8")),
                sections=_detect_sections(text),
                paragraphs=_split_paragraphs(text),
                word_count=len(text.split()),
                char_count=len(text),
                has_images=len(images) > 0,
                image_count=len(images),
                extraction_timestamp=time.time(),
            )
            pdf_doc.pages.append(record)
            pdf_doc.total_chars += record.char_count
            pdf_doc.total_words += record.word_count

        doc.close()
        return pdf_doc

    def _build_slcf(
        self,
        documents: list[PDFDocument],
        all_pages: list[PDFPageRecord],
        glyph_output: Optional[GlyphOutput] = None,
        filtered_prompt: Optional[FilteredPrompt] = None,
    ) -> bytes:
        """Build SLCF binary from extracted PDF content (L1–L7)."""
        writer = SLCFWriter(target_size_bytes=self.target_size)

        # Convert PDF data into the agent_graph format expected by SLCFWriter
        agent_graph = self._map_to_agent_graph(documents, all_pages)

        return writer.checkpoint(
            agent_graph,
            trigger="pdf_ingestion",
            glyph_output=glyph_output,
            filtered_prompt=filtered_prompt,
        )

    def _generate_glyph(self, all_pages: list[PDFPageRecord]) -> Optional[GlyphOutput]:
        """Run GlyphCompressor over PDF page content.

        Converts page text into scroll-like dicts for compression.
        Produces composite image ≤2048×2048 with geometry classification.
        """
        scrolls: list[dict[str, Any]] = []
        for page in all_pages[:50]:  # GlyphCompressor max batch = 50
            if not page.text.strip():
                continue
            scroll = {
                "text": page.text,
                "source_file": page.source_file,
                "page_number": page.page_number,
                "sections": page.sections,
                "@type": "hierarchical" if page.sections else "categorical",
            }
            scrolls.append(scroll)

        if not scrolls:
            return None

        try:
            return self.compressor.compress(scrolls)
        except Exception:
            return None

    def _generate_isfet(self, glyph_output: GlyphOutput) -> Optional[FilteredPrompt]:
        """Run IsfetFilter on the composite glyph with template-guided reading.

        Pass 1: Geometry distribution from L6 is injected as the structural
        template (a₀) — telling the VLM WHAT TO LOOK FOR.
        Pass 2: VLM reads the glyph with that focus, extracting operational
        truths (not summaries).
        """
        try:
            # Build geometry template from L6 metrics for Pass 1
            geometry_template = {
                "geometry_distribution": glyph_output.metrics.get("geometry_distribution", {}),
                "compression_ratio": glyph_output.compression_ratio,
                "element_count": len(glyph_output.elements),
                "original_tokens": glyph_output.original_tokens,
                "compressed_tokens": glyph_output.compressed_tokens,
            }
            return self.filter.filter(
                glyph_output.image,
                source_glyph_id=f"pdf-ingest-{id(glyph_output)}",
                geometry_template=geometry_template,
            )
        except Exception:
            return None

    def _map_to_agent_graph(
        self, documents: list[PDFDocument], all_pages: list[PDFPageRecord]
    ) -> dict[str, Any]:
        """Map PDF content to CascadeGuard agent_graph format for SLCF writer.

        Layer mapping:
          L1 Substrate: Page byte sizes, image flags (structural)
          L2 Resource Tethering: Extracted text per page (resource data)
          L3 Agentic Runtime: Document structure index (queryable)
          L4 Narrative: Full semantic content
          L5 Governance: Provenance metadata
        """
        agents: dict[str, Any] = {}
        edges: dict[str, list[str]] = {}

        for page in all_pages:
            page_id = f"{page.source_file}:p{page.page_number}"
            agents[page_id] = {
                # L1 data (substrate)
                "model_id": "pdf-page",
                "depth": 0,
                "parent_id": None,
                "children": [],
                # L2 data (resource tethering)
                "token_budget": page.word_count,
                "tokens_consumed": float(page.char_count),
                "cost_per_1k_tokens": 0.0,
                # Metadata carries L3/L4/L5 content
                "metadata": {
                    # L3: Structure (queryable)
                    "sections": page.sections,
                    "paragraph_count": len(page.paragraphs),
                    "word_count": page.word_count,
                    # L4: Narrative
                    "text": page.text,
                    "paragraphs": page.paragraphs,
                    # L5: Provenance
                    "source_file": page.source_file,
                    "page_number": page.page_number,
                    "byte_size": page.byte_size,
                    "has_images": page.has_images,
                    "image_count": page.image_count,
                    "extraction_timestamp": page.extraction_timestamp,
                },
            }
            edges[page_id] = []

        # Build document-level parent nodes
        for doc in documents:
            doc_id = f"doc:{doc.filename}"
            child_ids = [
                f"{doc.filename}:p{p.page_number}" for p in doc.pages
            ]
            agents[doc_id] = {
                "model_id": "pdf-document",
                "depth": 0,
                "parent_id": None,
                "children": child_ids,
                "token_budget": doc.total_words,
                "tokens_consumed": float(doc.total_chars),
                "cost_per_1k_tokens": 0.0,
                "metadata": {
                    "title": doc.title,
                    "author": doc.author,
                    "subject": doc.subject,
                    "creation_date": doc.creation_date,
                    "page_count": doc.page_count,
                    "total_words": doc.total_words,
                    "source_path": doc.path,
                },
            }
            edges[doc_id] = child_ids
            # Update children parent refs
            for cid in child_ids:
                if cid in agents:
                    agents[cid]["parent_id"] = doc_id
                    agents[cid]["depth"] = 1

        return {
            "agents": agents,
            "edges": edges,
            "metadata": {
                "ingestion_type": "pdf",
                "document_count": len(documents),
                "total_pages": len(all_pages),
                "ingestion_timestamp": time.time(),
            },
            "flow_state": "nominal",
            "total_delegations": 0,
            "cycles_detected": 0,
        }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class PDFIngestionResult:
    """Result of PDF→SLCF ingestion."""

    binary: bytes
    documents: list[PDFDocument]
    total_pages: int
    total_documents: int
    errors: list[dict[str, str]] = field(default_factory=list)
    glyph_output: Optional[GlyphOutput] = None
    filtered_prompt: Optional[FilteredPrompt] = None

    @property
    def size_bytes(self) -> int:
        """Total SLCF binary size."""
        return len(self.binary)

    @property
    def success(self) -> bool:
        """Whether ingestion produced valid output."""
        return len(self.binary) > 0 and self.total_pages > 0

    @property
    def has_l6(self) -> bool:
        """Whether L6 Glyph layer was generated."""
        return self.glyph_output is not None

    @property
    def has_l7(self) -> bool:
        """Whether L7 Isfet layer was generated."""
        return self.filtered_prompt is not None

    def summary(self) -> str:
        """Human-readable summary."""
        layers = "L1-L5"
        if self.has_l6 and self.has_l7:
            layers = "L1-L7"
        elif self.has_l6:
            layers = "L1-L6"
        return (
            f"PDF→SLCF ({layers}): {self.total_documents} docs, "
            f"{self.total_pages} pages, "
            f"{self.size_bytes:,} bytes SLCF output"
            f"{f', {len(self.errors)} errors' if self.errors else ''}"
        )
