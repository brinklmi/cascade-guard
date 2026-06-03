"""SLCF Binary Format — core data structures and computations.

Defines the binary layout, page structures, footer, deficiency tracking,
Kappa_Effective computation, Beta_One cycle counting, and Maat hash.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLCF_MAGIC = b"SLCF"
SLCF_VERSION = 2
SLCF_VERSION_V3 = 3  # Binary-encoded format (wire-compatible)

# Deficiency threshold for rare value routing
RARE_VALUE_THRESHOLD = 0.9
MAX_RARE_VALUES_PER_PAGE = 1024

# Maximum nesting depth for beta_one computation
MAX_NESTING_DEPTH = 16

# Default target size for Row_Group (64 MB)
DEFAULT_TARGET_SIZE = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SLCFLayer(IntEnum):
    """The 7 SLCF fractal layers."""

    L1_SUBSTRATE = 1
    L2_RESOURCE_TETHERING = 2
    L3_AGENTIC_RUNTIME = 3
    L4_NARRATIVE = 4
    L5_GOVERNANCE = 5
    L6_GLYPH = 6
    L7_ISFET = 7


class FlowState(str, Enum):
    """Metabolic flow state derived from κ_effective thresholds.

    LAMINAR:   κ_eff ≥ 0.7 — optimal, proceed normally
    TURBULENT: 0.3 ≤ κ_eff < 0.7 — caution, monitor
    RETREAT:   κ_eff < 0.3 — systemic retreat, preservation mode
    """

    LAMINAR = "laminar"
    TURBULENT = "turbulent"
    RETREAT = "retreat"


def classify_flow_state(kappa_effective: float) -> FlowState:
    """Classify flow state from κ_effective value.

    Args:
        kappa_effective: κ_eff in range [0.0, 1.0]

    Returns:
        FlowState enum value.
    """
    if kappa_effective >= 0.7:
        return FlowState.LAMINAR
    elif kappa_effective >= 0.3:
        return FlowState.TURBULENT
    else:
        return FlowState.RETREAT


# ---------------------------------------------------------------------------
# Flow State Breach Actions
# ---------------------------------------------------------------------------


class FlowStateAction(str, Enum):
    """Actions triggered by flow state transitions."""

    PROCEED = "proceed"  # Normal operation
    THROTTLE = "throttle"  # Reduce delegation rate, increase checkpoint frequency
    SHED_LOAD = "shed_load"  # Drop non-critical agents, compress aggressively
    HALT_DELEGATION = "halt_delegation"  # No new delegations until recovery


@dataclass
class FlowStateResponse:
    """Response to a flow state classification, including breach actions."""

    state: FlowState
    action: FlowStateAction
    kappa_effective: float
    message: str
    throttle_factor: float = 1.0  # 1.0 = no throttle, 0.0 = full stop


def evaluate_flow_state(kappa_effective: float) -> FlowStateResponse:
    """Evaluate κ_eff and return the appropriate breach response action.

    This is the active counterpart to classify_flow_state — it determines
    WHAT TO DO when κ_eff crosses a threshold boundary.

    Actions:
        LAMINAR  (κ_eff ≥ 0.7): PROCEED — normal operation
        TURBULENT (0.3 ≤ κ_eff < 0.7): THROTTLE — reduce delegation rate
        RETREAT  (0.1 ≤ κ_eff < 0.3): SHED_LOAD — drop non-critical work
        CRITICAL (κ_eff < 0.1): HALT_DELEGATION — full stop until recovery

    Args:
        kappa_effective: κ_eff in range [0.0, 1.0]

    Returns:
        FlowStateResponse with action, message, and throttle factor.
    """
    state = classify_flow_state(kappa_effective)

    if state == FlowState.LAMINAR:
        return FlowStateResponse(
            state=state,
            action=FlowStateAction.PROCEED,
            kappa_effective=kappa_effective,
            message="Flow optimal — proceeding normally",
            throttle_factor=1.0,
        )
    elif state == FlowState.TURBULENT:
        # Throttle proportionally: κ=0.7 → factor=1.0, κ=0.3 → factor=0.25
        throttle = max(0.25, (kappa_effective - 0.3) / 0.4)
        return FlowStateResponse(
            state=state,
            action=FlowStateAction.THROTTLE,
            kappa_effective=kappa_effective,
            message=f"Turbulent flow — throttling to {throttle:.0%} delegation rate",
            throttle_factor=throttle,
        )
    else:
        # RETREAT: distinguish between recoverable and critical
        if kappa_effective >= 0.1:
            return FlowStateResponse(
                state=state,
                action=FlowStateAction.SHED_LOAD,
                kappa_effective=kappa_effective,
                message="Retreat — shedding non-critical agents for preservation",
                throttle_factor=0.1,
            )
        else:
            return FlowStateResponse(
                state=state,
                action=FlowStateAction.HALT_DELEGATION,
                kappa_effective=kappa_effective,
                message="Critical — halting all delegation until κ_eff recovers",
                throttle_factor=0.0,
            )


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class SLCFPage:
    """A single page in the SLCF binary format.

    Each page belongs to one layer and contains columnar data
    with per-value deficiency tracking.
    """

    layer_id: SLCFLayer
    page_id: int
    row_count: int = 0
    compression: str = "none"  # "none", "zstd", "snappy"
    data: bytes = b""
    column_metadata: dict[str, Any] = field(default_factory=dict)
    deficiency_map: dict[str, float] = field(default_factory=dict)
    checksum: bytes = b""

    @property
    def size_bytes(self) -> int:
        """Actual size of this page's data in bytes."""
        return len(self.data)


@dataclass
class PageIndexEntry:
    """Index entry for a single page in the footer."""

    page_id: int
    layer_id: SLCFLayer
    offset: int
    size: int


@dataclass
class SLCFFooter:
    """SLCF file footer containing validation and index metadata.

    The footer is always read first and validated before any page access.
    """

    maat_hash: bytes = b""  # SHA-256 over all page content bytes
    sigma_delta: float = 0.0  # Cumulative deficiency Σδ(a)
    kappa_effective: float = 0.0  # tanh(actual_size / target_size)
    beta_one: int = 0  # 1-cycle count (β₁) from homology
    flow_state: str = "laminar"  # FlowState derived from κ_eff thresholds
    page_index: list[PageIndexEntry] = field(default_factory=list)
    encryption_fields: dict[str, Any] = field(default_factory=dict)
    target_size: int = DEFAULT_TARGET_SIZE
    actual_size: int = 0

    @property
    def is_valid_kappa(self) -> bool:
        """Check κ_eff is within valid range [0.0, 1.0]."""
        return 0.0 <= self.kappa_effective <= 1.0

    @property
    def is_compliant(self) -> bool:
        """Check cumulative deficiency is within bounds."""
        return self.sigma_delta <= 2.0

    @property
    def is_laminar(self) -> bool:
        """Whether flow state is optimal (κ_eff ≥ 0.7)."""
        return self.flow_state == FlowState.LAMINAR.value

    @property
    def requires_retreat(self) -> bool:
        """Whether system should enter preservation mode (κ_eff < 0.3)."""
        return self.flow_state == FlowState.RETREAT.value


# ---------------------------------------------------------------------------
# Deficiency Tracker
# ---------------------------------------------------------------------------


class DeficiencyTracker:
    """Per-value deficiency tracking with RARE_VALUE_PAGE routing.

    Tracks δ(a) for each value. Values with δ > 0.9 are routed
    to a dedicated RARE_VALUE_PAGE (max 1024 values per page).
    """

    def __init__(self) -> None:
        self._deficiencies: dict[str, float] = {}
        self._rare_values: list[str] = []

    @property
    def deficiencies(self) -> dict[str, float]:
        """All tracked deficiency values."""
        return dict(self._deficiencies)

    @property
    def rare_values(self) -> list[str]:
        """Values routed to RARE_VALUE_PAGE (δ > 0.9)."""
        return list(self._rare_values)

    @property
    def sigma_delta(self) -> float:
        """Cumulative deficiency Σδ(a)."""
        return sum(self._deficiencies.values())

    @property
    def rare_page_full(self) -> bool:
        """Whether RARE_VALUE_PAGE has reached capacity."""
        return len(self._rare_values) >= MAX_RARE_VALUES_PER_PAGE

    def track(self, value_id: str, deficiency: float) -> None:
        """Track deficiency for a value.

        Args:
            value_id: Unique identifier for the value
            deficiency: δ(a) in range [0.0, 1.0]

        Routes values with δ > 0.9 to RARE_VALUE_PAGE.
        """
        self._deficiencies[value_id] = deficiency
        if deficiency > RARE_VALUE_THRESHOLD:
            if value_id not in self._rare_values:
                if not self.rare_page_full:
                    self._rare_values.append(value_id)

    def track_batch(self, values: dict[str, float]) -> None:
        """Track deficiency for multiple values at once."""
        for value_id, deficiency in values.items():
            self.track(value_id, deficiency)

    def validate(self) -> tuple[bool, Optional[str]]:
        """Validate Σδ(a) ≤ 2.

        Returns:
            (is_valid, error_message) — error_message is None if valid.
        """
        sd = self.sigma_delta
        if sd > 2.0:
            return False, f"Cumulative deficiency Σδ(a) = {sd:.6f} exceeds limit 2.0"
        return True, None

    def reset(self) -> None:
        """Clear all tracked deficiencies."""
        self._deficiencies.clear()
        self._rare_values.clear()


# ---------------------------------------------------------------------------
# Columnar Encodings
# ---------------------------------------------------------------------------


class ColumnEncoding(IntEnum):
    """Page data encoding types."""

    PLAIN = 0           # Raw JSON bytes (default)
    RLE = 1             # Run-length encoding (repeated values)
    DICTIONARY = 2      # Dictionary + index encoding (string columns)
    DELTA_BINARY = 3    # Delta binary packed (sequential integers)


def encode_rle(values: list) -> bytes:
    """Run-Length Encode a list of values.

    Format: count(4) + value_len(4) + value_bytes, repeated.
    Best for: repeated model_ids, repeated status strings.

    Returns:
        RLE-encoded bytes.
    """
    import json as _json
    if not values:
        return b""

    runs: list[tuple[int, Any]] = []
    current = values[0]
    count = 1
    for v in values[1:]:
        if v == current:
            count += 1
        else:
            runs.append((count, current))
            current = v
            count = 1
    runs.append((count, current))

    parts = []
    for count, value in runs:
        value_bytes = _json.dumps(value).encode("utf-8")
        parts.append(struct.pack("<II", count, len(value_bytes)))
        parts.append(value_bytes)

    return b"".join(parts)


def decode_rle(data: bytes) -> list:
    """Decode RLE-encoded bytes back to values."""
    import json as _json
    values = []
    offset = 0
    while offset < len(data):
        if offset + 8 > len(data):
            break
        count, value_len = struct.unpack("<II", data[offset:offset + 8])
        offset += 8
        value_bytes = data[offset:offset + value_len]
        offset += value_len
        value = _json.loads(value_bytes.decode("utf-8"))
        values.extend([value] * count)
    return values


def encode_dictionary(values: list[str]) -> bytes:
    """Dictionary-encode a list of string values.

    Format: dict_count(4) + [str_len(4) + str_bytes]... + [index(4)]...
    Best for: model_id columns, geometry type columns.

    Returns:
        Dictionary-encoded bytes.
    """
    if not values:
        return b""

    # Build dictionary
    unique: list[str] = []
    index_map: dict[str, int] = {}
    for v in values:
        s = str(v) if v is not None else ""
        if s not in index_map:
            index_map[s] = len(unique)
            unique.append(s)

    # Encode dictionary
    parts = [struct.pack("<I", len(unique))]
    for s in unique:
        s_bytes = s.encode("utf-8")
        parts.append(struct.pack("<I", len(s_bytes)))
        parts.append(s_bytes)

    # Encode indices
    for v in values:
        s = str(v) if v is not None else ""
        parts.append(struct.pack("<I", index_map[s]))

    return b"".join(parts)


def decode_dictionary(data: bytes) -> list[str]:
    """Decode dictionary-encoded bytes back to string values."""
    if not data:
        return []

    offset = 0
    dict_count = struct.unpack("<I", data[offset:offset + 4])[0]
    offset += 4

    dictionary: list[str] = []
    for _ in range(dict_count):
        str_len = struct.unpack("<I", data[offset:offset + 4])[0]
        offset += 4
        dictionary.append(data[offset:offset + str_len].decode("utf-8"))
        offset += str_len

    # Read indices
    values = []
    while offset + 4 <= len(data):
        idx = struct.unpack("<I", data[offset:offset + 4])[0]
        offset += 4
        values.append(dictionary[idx] if idx < len(dictionary) else "")

    return values


def encode_delta_binary(values: list[int]) -> bytes:
    """Delta binary pack a list of integers.

    Format: base_value(8 signed) + count(4) + [delta(4 signed)]...
    Best for: sequential page numbers, depths, token counts.

    Returns:
        Delta-encoded bytes.
    """
    if not values:
        return b""

    base = values[0]
    deltas = [v - values[i] for i, v in enumerate(values[1:], 0)]
    # deltas[i] = values[i+1] - values[i]

    parts = [struct.pack("<qI", base, len(deltas))]
    for d in deltas:
        parts.append(struct.pack("<i", d))

    return b"".join(parts)


def decode_delta_binary(data: bytes) -> list[int]:
    """Decode delta binary packed bytes back to integers."""
    if len(data) < 12:
        return []

    base, count = struct.unpack("<qI", data[:12])
    offset = 12
    values = [base]

    for _ in range(count):
        if offset + 4 > len(data):
            break
        delta = struct.unpack("<i", data[offset:offset + 4])[0]
        offset += 4
        values.append(values[-1] + delta)

    return values


# ---------------------------------------------------------------------------
# Computation Functions
# ---------------------------------------------------------------------------


def compute_kappa_effective(actual_size: int, target_size: int = DEFAULT_TARGET_SIZE) -> float:
    """Compute κ_effective as tanh(actual_size / target_size).

    Args:
        actual_size: Actual size in bytes of the Row_Group
        target_size: Target size in bytes (default 64 MB)

    Returns:
        κ_eff value in range (0.0, 1.0)
    """
    if target_size <= 0:
        return 1.0
    return math.tanh(actual_size / target_size)


def compute_maat_hash(page_data: bytes | list[bytes]) -> bytes:
    """Compute Maat hash (SHA-256) over page content bytes.

    Args:
        page_data: Either raw bytes of all pages concatenated,
                   or a list of page byte arrays.

    Returns:
        32-byte SHA-256 digest.
    """
    hasher = hashlib.sha256()
    if isinstance(page_data, list):
        for chunk in page_data:
            hasher.update(chunk)
    else:
        hasher.update(page_data)
    return hasher.digest()


def compute_beta_one(
    graph: dict[str, list[str]],
    max_depth: int = MAX_NESTING_DEPTH,
) -> int:
    """Compute β₁ (1-cycle count) for nested agent references.

    Uses DFS-based cycle detection up to max_depth levels.
    β₁ counts the number of independent cycles (first Betti number)
    present in the agent reference graph.

    Args:
        graph: Adjacency list mapping agent_id → list of referenced agent_ids
        max_depth: Maximum nesting depth to explore (default 16)

    Returns:
        Count of 1-cycles (β₁) found in the graph.
    """
    # β₁ = |edges| - |vertices| + |connected_components|
    # for an undirected graph. For directed graphs with back-edges,
    # we count actual cycles via DFS.
    visited: set[str] = set()
    in_stack: set[str] = set()
    cycle_count = 0

    def _dfs(node: str, depth: int) -> int:
        nonlocal cycle_count
        if depth > max_depth:
            return 0
        if node in in_stack:
            # Back edge found — this is a cycle
            return 1
        if node in visited:
            return 0

        visited.add(node)
        in_stack.add(node)
        cycles_found = 0

        for neighbor in graph.get(node, []):
            cycles_found += _dfs(neighbor, depth + 1)

        in_stack.discard(node)
        return cycles_found

    for node in graph:
        if node not in visited:
            cycle_count += _dfs(node, 0)

    return cycle_count
