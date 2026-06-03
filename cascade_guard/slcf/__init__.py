"""SLCF — Scroll-LD Columnar Format.

Binary, self-describing, fractal-layered columnar storage with 7 layers:
  L1: Substrate
  L2: Resource Tethering
  L3: Agentic Runtime
  L4: Narrative
  L5: Governance
  L6: Glyph (composite VLM image bytes)
  L7: Isfet (filtered signal prompt)

Provides persistence for CascadeGuard delegation state with full L5 governance,
VLM-compressed glyph embedding, isfet-filtered signal persistence,
efficient predicate pushdown queries, and Maat-validated integrity.
"""

from cascade_guard.slcf.format import (
    SLCF_MAGIC,
    SLCF_VERSION,
    DeficiencyTracker,
    FlowState,
    FlowStateAction,
    FlowStateResponse,
    SLCFFooter,
    SLCFLayer,
    SLCFPage,
    classify_flow_state,
    compute_beta_one,
    compute_kappa_effective,
    compute_maat_hash,
    evaluate_flow_state,
)
from cascade_guard.slcf.reader import (
    GlyphLayerData,
    IsfetLayerData,
)

__all__ = [
    "SLCF_MAGIC",
    "SLCF_VERSION",
    "DeficiencyTracker",
    "FlowState",
    "FlowStateAction",
    "FlowStateResponse",
    "GlyphLayerData",
    "IsfetLayerData",
    "SLCFFooter",
    "SLCFLayer",
    "SLCFPage",
    "classify_flow_state",
    "compute_beta_one",
    "compute_kappa_effective",
    "compute_maat_hash",
    "evaluate_flow_state",
]
