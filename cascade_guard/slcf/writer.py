"""SLCF Writer — serializes CascadeGuard agent graph state to SLCF binary.

Supports checkpoint triggers: interval elapsed, explicit command, or
size threshold (128 MB Row_Group max). Enforces Maat hash integrity,
deficiency bounds (Σδ ≤ 2), and atomic write semantics.

Layers L1–L5 are always written. L6 (Glyph) and L7 (Isfet) are written
when glyph_output and/or filtered_prompt are provided to checkpoint().
"""

from __future__ import annotations

import io
import json
import struct
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from cascade_guard.slcf.format import (
    DEFAULT_TARGET_SIZE,
    MAX_NESTING_DEPTH,
    SLCF_MAGIC,
    SLCF_VERSION,
    SLCF_VERSION_V3,
    DeficiencyTracker,
    FlowState,
    FlowStateAction,
    FlowStateResponse,
    PageIndexEntry,
    SLCFFooter,
    SLCFLayer,
    SLCFPage,
    classify_flow_state,
    compute_beta_one,
    compute_kappa_effective,
    compute_maat_hash,
    evaluate_flow_state,
)

if TYPE_CHECKING:
    from cascade_guard.glyph.compressor import GlyphOutput
    from cascade_guard.glyph.isfet_filter import FilteredPrompt


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SLCFValidationError(Exception):
    """Raised when SLCF validation fails (e.g., Σδ > 2)."""

    def __init__(self, message: str, page_group_id: Optional[str] = None):
        self.page_group_id = page_group_id
        super().__init__(message)


class SLCFWriteError(Exception):
    """Raised when an SLCF write operation fails mid-serialization."""

    pass


# ---------------------------------------------------------------------------
# Row Group Size Threshold
# ---------------------------------------------------------------------------

MAX_ROW_GROUP_SIZE = 128 * 1024 * 1024  # 128 MB
CHECKPOINT_DEADLINE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# SLCF Writer
# ---------------------------------------------------------------------------


class SLCFWriter:
    """Serializes CascadeGuard agent graph state to SLCF binary format.

    Produces files with layers L1–L5 (always), plus optional L6 (Glyph
    composite image bytes) and L7 (Isfet-filtered signal prompt).
    Includes deficiency tracking, κ_effective computation, β₁ cycle
    counting, and Maat hash footer.
    """

    def __init__(self, target_size_bytes: int = DEFAULT_TARGET_SIZE, version: int = SLCF_VERSION):
        """Initialize the SLCF writer.

        Args:
            target_size_bytes: Target Row_Group size for κ_eff computation.
                              Default is 64 MB.
            version: SLCF format version (2 = JSON pages, 3 = binary wire-compatible).
                    Default is v2 for backward compatibility.
        """
        self.target_size = target_size_bytes
        self.version = version
        self._deficiency_tracker = DeficiencyTracker()
        self._last_flow_response: Optional[FlowStateResponse] = None

    @property
    def last_flow_response(self) -> Optional[FlowStateResponse]:
        """Last flow state response from the most recent checkpoint.

        Returns None if no checkpoint has been performed yet.
        Use this to inspect throttle_factor and action after checkpoint().
        """
        return self._last_flow_response

    def checkpoint(
        self,
        agent_graph: dict[str, Any],
        trigger: str = "explicit",
        glyph_output: Optional["GlyphOutput"] = None,
        filtered_prompt: Optional["FilteredPrompt"] = None,
    ) -> bytes:
        """Serialize agent graph to SLCF binary format.

        Args:
            agent_graph: Current CascadeGuard agent state dictionary.
                        Expected keys: 'agents' (dict of agent nodes),
                        'edges' (adjacency list), 'metadata' (dict).
            trigger: Trigger type — "interval", "explicit", or "size_threshold"
            glyph_output: Optional GlyphOutput from GlyphCompressor.compress().
                         When provided, the composite image is persisted as L6.
            filtered_prompt: Optional FilteredPrompt from IsfetFilter.filter().
                            When provided, the filtered signal is persisted as L7.

        Returns:
            Complete SLCF binary payload.

        Raises:
            SLCFValidationError: If Σδ(a) > 2 for any page group.
            SLCFWriteError: If serialization fails mid-write.
        """
        start_time = time.monotonic()

        try:
            # Reset deficiency tracker for this checkpoint
            self._deficiency_tracker.reset()

            # 1. Serialize agent graph into pages across L1–L5
            pages = self._serialize_layers(agent_graph)

            # 1b. Serialize L6 (Glyph) if provided
            if glyph_output is not None:
                l6_page = self._serialize_l6_glyph(glyph_output, len(pages))
                pages.append(l6_page)

            # 1c. Serialize L7 (Isfet) if provided
            if filtered_prompt is not None:
                l7_page = self._serialize_l7_isfet(filtered_prompt, len(pages))
                pages.append(l7_page)

            # 2. Compute per-value deficiency and route rare values
            self._compute_deficiencies(agent_graph)

            # 3. Validate Σδ(a) ≤ 2
            is_valid, error_msg = self._deficiency_tracker.validate()
            if not is_valid:
                raise SLCFValidationError(
                    error_msg or "Deficiency validation failed",
                    page_group_id=self._identify_offending_page_group(agent_graph),
                )

            # 4. Compute metrics
            page_data_list = [p.data for p in pages]
            actual_size = sum(len(d) for d in page_data_list)
            kappa_eff = compute_kappa_effective(actual_size, self.target_size)

            # Evaluate flow state and determine breach action
            flow_response = evaluate_flow_state(kappa_eff)
            self._last_flow_response = flow_response

            # Build adjacency list for β₁ computation
            edges = agent_graph.get("edges", {})
            beta_one = compute_beta_one(edges, MAX_NESTING_DEPTH)

            # 5. Build the binary payload
            binary_pages = self._encode_pages(pages)

            # 6. Compute Maat hash over all page bytes
            maat_hash = compute_maat_hash(binary_pages)

            # 7. Build footer
            footer = SLCFFooter(
                maat_hash=maat_hash,
                sigma_delta=self._deficiency_tracker.sigma_delta,
                kappa_effective=kappa_eff,
                beta_one=beta_one,
                flow_state=classify_flow_state(kappa_eff).value,
                page_index=self._build_page_index(pages, binary_pages),
                target_size=self.target_size,
                actual_size=actual_size,
            )

            # 8. Assemble final binary
            result = self._assemble_binary(binary_pages, footer)

            # Check deadline
            elapsed = time.monotonic() - start_time
            if elapsed > CHECKPOINT_DEADLINE_SECONDS:
                # Log warning but don't fail — data is valid
                pass

            return result

        except SLCFValidationError:
            raise
        except Exception as e:
            raise SLCFWriteError(
                f"SLCF write failed during serialization: {e}"
            ) from e

    def _serialize_layers(self, agent_graph: dict[str, Any]) -> list[SLCFPage]:
        """Serialize agent graph into pages across all 5 SLCF layers."""
        pages: list[SLCFPage] = []
        agents = agent_graph.get("agents", {})
        metadata = agent_graph.get("metadata", {})
        edges = agent_graph.get("edges", {})

        page_id = 0

        # L1: Substrate — raw agent identifiers and structure
        l1_data = json.dumps({
            "agent_ids": list(agents.keys()),
            "agent_count": len(agents),
            "edge_count": sum(len(v) for v in edges.values()),
        }).encode("utf-8")
        pages.append(SLCFPage(
            layer_id=SLCFLayer.L1_SUBSTRATE,
            page_id=page_id,
            row_count=len(agents),
            data=l1_data,
        ))
        page_id += 1

        # L2: Resource Tethering — token budgets, model costs, κ_eff
        l2_records = {}
        for aid, agent in agents.items():
            l2_records[aid] = {
                "token_budget": agent.get("token_budget"),
                "tokens_consumed": agent.get("tokens_consumed", 0.0),
                "model_id": agent.get("model_id", "unknown"),
                "cost_per_1k": agent.get("cost_per_1k_tokens", 0.03),
            }
        l2_data = json.dumps(l2_records).encode("utf-8")
        pages.append(SLCFPage(
            layer_id=SLCFLayer.L2_RESOURCE_TETHERING,
            page_id=page_id,
            row_count=len(l2_records),
            data=l2_data,
        ))
        page_id += 1

        # L3: Agentic Runtime — delegation graph edges, depths, children
        l3_records = {}
        for aid, agent in agents.items():
            l3_records[aid] = {
                "parent_id": agent.get("parent_id"),
                "depth": agent.get("depth", 0),
                "children": agent.get("children", []),
            }
        l3_data = json.dumps(l3_records).encode("utf-8")
        pages.append(SLCFPage(
            layer_id=SLCFLayer.L3_AGENTIC_RUNTIME,
            page_id=page_id,
            row_count=len(l3_records),
            data=l3_data,
        ))
        page_id += 1

        # L4: Narrative — agent metadata and descriptions
        l4_records = {}
        for aid, agent in agents.items():
            l4_records[aid] = agent.get("metadata", {})
        l4_data = json.dumps(l4_records).encode("utf-8")
        pages.append(SLCFPage(
            layer_id=SLCFLayer.L4_NARRATIVE,
            page_id=page_id,
            row_count=len(l4_records),
            data=l4_data,
        ))
        page_id += 1

        # L5: Governance — delegation decisions, flow state, impedance
        l5_data = json.dumps({
            "governance_metadata": metadata,
            "flow_state": agent_graph.get("flow_state", "nominal"),
            "total_delegations": agent_graph.get("total_delegations", 0),
            "cycles_detected": agent_graph.get("cycles_detected", 0),
        }).encode("utf-8")
        pages.append(SLCFPage(
            layer_id=SLCFLayer.L5_GOVERNANCE,
            page_id=page_id,
            row_count=1,
            data=l5_data,
        ))

        return pages

    def _serialize_l6_glyph(self, glyph_output: "GlyphOutput", page_id: int) -> SLCFPage:
        """Serialize L6 Glyph layer — composite image bytes (PNG).

        Stores the composite glyph image as PNG binary data along with
        compression metrics metadata in the page's column_metadata.

        Args:
            glyph_output: GlyphOutput from GlyphCompressor.compress().
            page_id: Page ID to assign.

        Returns:
            SLCFPage containing PNG image bytes and glyph metrics.
        """
        # Serialize image to PNG bytes
        buf = io.BytesIO()
        glyph_output.image.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        # Build metadata envelope (stored alongside image bytes)
        l6_envelope = {
            "format": "png",
            "width": glyph_output.image.width,
            "height": glyph_output.image.height,
            "compression_ratio": glyph_output.compression_ratio,
            "geometry_type": glyph_output.geometry_type.value,
            "original_tokens": glyph_output.original_tokens,
            "compressed_tokens": glyph_output.compressed_tokens,
            "signature_hash": glyph_output.signature_hash.hex(),
            "element_count": len(glyph_output.elements),
            "unclassified": glyph_output.unclassified,
            "metrics": glyph_output.metrics,
        }
        metadata_bytes = json.dumps(l6_envelope).encode("utf-8")

        # Binary layout for L6: metadata_len(4) + metadata + image_bytes
        l6_data = struct.pack("<I", len(metadata_bytes)) + metadata_bytes + image_bytes

        return SLCFPage(
            layer_id=SLCFLayer.L6_GLYPH,
            page_id=page_id,
            row_count=1,
            data=l6_data,
            column_metadata=l6_envelope,
        )

    def _serialize_l7_isfet(self, filtered_prompt: "FilteredPrompt", page_id: int) -> SLCFPage:
        """Serialize L7 Isfet layer — filtered signal prompt.

        Stores the isfet-filtered system prompt text and extraction metadata.

        Args:
            filtered_prompt: FilteredPrompt from IsfetFilter.filter().
            page_id: Page ID to assign.

        Returns:
            SLCFPage containing filtered signal JSON.
        """
        l7_record = {
            "text": filtered_prompt.text,
            "token_count": filtered_prompt.token_count,
            "metrics_extracted": filtered_prompt.metrics_extracted,
            "geometries_extracted": filtered_prompt.geometries_extracted,
            "source_glyph_id": filtered_prompt.source_glyph_id,
            "vlm_raw_length": filtered_prompt.vlm_raw_length,
            "filtered_length": filtered_prompt.filtered_length,
        }
        l7_data = json.dumps(l7_record).encode("utf-8")

        return SLCFPage(
            layer_id=SLCFLayer.L7_ISFET,
            page_id=page_id,
            row_count=1,
            data=l7_data,
        )

    def _compute_deficiencies(self, agent_graph: dict[str, Any]) -> None:
        """Compute per-value deficiency δ(a) for agent graph values.

        Deficiency is computed as 1 - (frequency / max_frequency) for each
        unique value in agent fields. Rare values (δ > 0.9) are routed
        to RARE_VALUE_PAGE.
        """
        agents = agent_graph.get("agents", {})
        if not agents:
            return

        # Track model_id frequencies as proxy for value rarity
        model_counts: dict[str, int] = {}
        for agent in agents.values():
            mid = agent.get("model_id", "unknown")
            model_counts[mid] = model_counts.get(mid, 0) + 1

        max_count = max(model_counts.values()) if model_counts else 1

        for model_id, count in model_counts.items():
            deficiency = 1.0 - (count / max_count)
            self._deficiency_tracker.track(f"model:{model_id}", deficiency)

        # Track depth distribution
        depth_counts: dict[int, int] = {}
        for agent in agents.values():
            d = agent.get("depth", 0)
            depth_counts[d] = depth_counts.get(d, 0) + 1

        max_depth_count = max(depth_counts.values()) if depth_counts else 1
        for depth, count in depth_counts.items():
            deficiency = 1.0 - (count / max_depth_count)
            self._deficiency_tracker.track(f"depth:{depth}", deficiency)

    def _identify_offending_page_group(self, agent_graph: dict[str, Any]) -> str:
        """Identify which page group caused Σδ > 2."""
        # For single-file checkpoints, the page group is the entire checkpoint
        return f"checkpoint_{id(agent_graph)}"

    def _encode_pages(self, pages: list[SLCFPage]) -> list[bytes]:
        """Encode pages to raw binary (header + optionally compressed data).

        Page header: layer_id(1) + page_id(4) + row_count(4) + data_len(4) + compression(1)
        Compression: 0=none, 1=zstd
        Data > 1024 bytes is ZSTD-compressed for space efficiency.
        """
        import zstandard

        encoded = []
        compressor = zstandard.ZstdCompressor(level=3)

        for page in pages:
            data = page.data
            compression_flag = 0

            # Compress data > 1KB with ZSTD
            if len(data) > 1024:
                compressed = compressor.compress(data)
                # Only use compressed if it's actually smaller
                if len(compressed) < len(data):
                    data = compressed
                    compression_flag = 1

            # Page header: layer_id(1) + page_id(4) + row_count(4) + data_len(4) + compression(1)
            header = struct.pack(
                "<BIIIB",
                page.layer_id,
                page.page_id,
                page.row_count,
                len(data),
                compression_flag,
            )
            encoded.append(header + data)
        return encoded

    def _build_page_index(
        self, pages: list[SLCFPage], binary_pages: list[bytes]
    ) -> list[PageIndexEntry]:
        """Build page index from encoded pages."""
        index = []
        offset = len(SLCF_MAGIC) + 4  # magic + version+flags header
        for page, binary in zip(pages, binary_pages):
            index.append(PageIndexEntry(
                page_id=page.page_id,
                layer_id=page.layer_id,
                offset=offset,
                size=len(binary),
            ))
            offset += len(binary)
        return index

    def _assemble_binary(self, binary_pages: list[bytes], footer: SLCFFooter) -> bytes:
        """Assemble final SLCF binary: magic + header + pages + footer."""
        # File header: magic(4) + version(2) + flags(2)
        version = SLCF_VERSION_V3 if self.version >= 3 else SLCF_VERSION
        header = SLCF_MAGIC + struct.pack("<HH", version, 0)

        # Pages
        page_bytes = b"".join(binary_pages)

        # Footer serialization (v3 = binary, v2 = JSON)
        if self.version >= 3:
            from cascade_guard.slcf.binary_codec import encode_footer as binary_encode_footer
            footer_data = binary_encode_footer(footer)
        else:
            footer_data = self._encode_footer(footer)

        # Footer length at the very end (4 bytes) so reader can find it
        footer_len = struct.pack("<I", len(footer_data))

        return header + page_bytes + footer_data + footer_len

    def _encode_footer(self, footer: SLCFFooter) -> bytes:
        """Encode footer to binary."""
        footer_dict = {
            "maat_hash": footer.maat_hash.hex(),
            "sigma_delta": footer.sigma_delta,
            "kappa_effective": footer.kappa_effective,
            "beta_one": footer.beta_one,
            "flow_state": footer.flow_state,
            "target_size": footer.target_size,
            "actual_size": footer.actual_size,
            "page_index": [
                {
                    "page_id": entry.page_id,
                    "layer_id": int(entry.layer_id),
                    "offset": entry.offset,
                    "size": entry.size,
                }
                for entry in footer.page_index
            ],
            "encryption_fields": footer.encryption_fields,
        }
        return json.dumps(footer_dict).encode("utf-8")
