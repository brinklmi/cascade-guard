"""Inference History Buffer — ZSTD-compressed traces with harmonic decay.

Stores inference traces keyed by composite_space_id (harmonic 4D + embedding 768D).
Uses LMDB-style directory layout for persistence. Supports configurable decay (τ),
eviction by lowest decay-weighted score, and max capacity of 10K traces.

This is the foundational component of Phase 5: Adaptive Resonance.
All other components (novelty detector, experience replay, pattern mining)
read from this buffer.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import zstandard


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TAU_SECONDS = 3 * 24 * 3600  # 3 days in seconds
DEFAULT_MAX_TRACES = 10_000
ZSTD_COMPRESSION_LEVEL = 3

# Harmonic frequencies used for signature encoding
HARMONIC_FREQUENCIES = (432, 528, 741, 852)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class InferenceTrace:
    """A single inference trace recording the full path through the system.

    Captures: query → resonance path → gate decision → outcome.
    """

    # Identity
    trace_id: str = ""  # SHA-256 of composite_space_id
    timestamp: float = 0.0  # Unix timestamp

    # Input
    query_hash: str = ""  # SHA-256 of query text
    query_embedding: list[float] = field(default_factory=list)  # 768D nomic-embed

    # Resonance path
    harmonic_signature: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    flow_state: str = "laminar"  # laminar/turbulent/retreat
    kappa_effective: float = 0.0

    # Gate decision
    gate_action: str = "proceed"  # proceed/throttle/shed_load/halt
    throttle_factor: float = 1.0

    # Outcome
    maat_passed: bool = False
    maat_score: float = 0.0
    oracle_confidence: float = 0.0
    inference_time_ms: float = 0.0
    model_used: str = ""

    # Derived
    success: bool = False  # maat_passed AND oracle_confidence >= 0.95

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.trace_id:
            self.trace_id = self._compute_trace_id()
        self.success = self.maat_passed and self.oracle_confidence >= 0.95

    def _compute_trace_id(self) -> str:
        """Compute trace ID from composite space (harmonic + query hash)."""
        composite = self.query_hash + "|" + "|".join(
            f"{h:.4f}" for h in self.harmonic_signature
        )
        return hashlib.sha256(composite.encode()).hexdigest()[:16]

    @property
    def composite_space_id(self) -> str:
        """The full composite space identifier for retrieval."""
        return self.trace_id

    def decay_weight(self, tau: float = DEFAULT_TAU_SECONDS, now: Optional[float] = None) -> float:
        """Compute harmonic decay weight for this trace.

        Uses exponential decay: w = exp(-age / tau)
        Modulated by harmonic amplitude for resonance weighting.

        Args:
            tau: Decay time constant in seconds (default 3 days).
            now: Current time (defaults to time.time()).

        Returns:
            Decay-weighted score in [0.0, 1.0].
        """
        if now is None:
            now = time.time()
        age = max(0.0, now - self.timestamp)
        base_decay = math.exp(-age / tau)

        # Harmonic amplitude modulation (mean of absolute harmonic values)
        harmonic_amp = sum(abs(h) for h in self.harmonic_signature) / len(self.harmonic_signature)
        harmonic_mod = min(1.0, harmonic_amp)  # Cap at 1.0

        return base_decay * (0.7 + 0.3 * harmonic_mod)

    def to_bytes(self) -> bytes:
        """Serialize trace to ZSTD-compressed JSON bytes."""
        data = json.dumps(asdict(self), separators=(",", ":")).encode("utf-8")
        compressor = zstandard.ZstdCompressor(level=ZSTD_COMPRESSION_LEVEL)
        return compressor.compress(data)

    @classmethod
    def from_bytes(cls, data: bytes) -> "InferenceTrace":
        """Deserialize trace from ZSTD-compressed JSON bytes."""
        decompressor = zstandard.ZstdDecompressor()
        json_bytes = decompressor.decompress(data)
        d = json.loads(json_bytes)
        return cls(**d)


# ---------------------------------------------------------------------------
# History Buffer
# ---------------------------------------------------------------------------


class InferenceHistoryBuffer:
    """LMDB-layout persistence for inference traces.

    Directory structure:
        {store_path}/
            traces/          # Individual trace files (trace_id.zst)
            index.json       # Lightweight index: trace_id → {timestamp, decay_score}
            stats.json       # Buffer statistics

    This avoids a hard dependency on the lmdb Python package while
    maintaining the same access patterns (key-value, append-heavy,
    memory-efficient reads).
    """

    def __init__(
        self,
        store_path: Path | str,
        max_traces: int = DEFAULT_MAX_TRACES,
        tau: float = DEFAULT_TAU_SECONDS,
    ):
        """Initialize the history buffer.

        Args:
            store_path: Directory for trace storage.
            max_traces: Maximum number of traces before eviction.
            tau: Harmonic decay time constant in seconds.
        """
        self.store_path = Path(store_path)
        self.max_traces = max_traces
        self.tau = tau

        # Ensure directory structure
        self._traces_dir = self.store_path / "traces"
        self._traces_dir.mkdir(parents=True, exist_ok=True)

        # Load or create index
        self._index_path = self.store_path / "index.json"
        self._index: dict[str, dict[str, Any]] = self._load_index()

        # Stats tracking
        self._stats_path = self.store_path / "stats.json"
        self._total_stored = len(self._index)
        self._total_evicted = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Number of traces currently stored."""
        return len(self._index)

    @property
    def is_full(self) -> bool:
        """Whether buffer has reached max capacity."""
        return self.size >= self.max_traces

    def store(self, trace: InferenceTrace) -> str:
        """Store an inference trace.

        If buffer is full, evicts the trace with lowest decay-weighted score.

        Args:
            trace: InferenceTrace to store.

        Returns:
            trace_id of the stored trace.
        """
        # Evict if at capacity
        if self.is_full:
            self._evict_lowest()

        # Write compressed trace
        trace_file = self._traces_dir / f"{trace.trace_id}.zst"
        trace_file.write_bytes(trace.to_bytes())

        # Update index
        self._index[trace.trace_id] = {
            "timestamp": trace.timestamp,
            "success": trace.success,
            "kappa_effective": trace.kappa_effective,
            "maat_score": trace.maat_score,
        }
        self._persist_index()

        self._total_stored += 1
        return trace.trace_id

    def retrieve(self, trace_id: str) -> Optional[InferenceTrace]:
        """Retrieve a trace by ID.

        Args:
            trace_id: The trace_id to look up.

        Returns:
            InferenceTrace if found, None otherwise.
        """
        if trace_id not in self._index:
            return None

        trace_file = self._traces_dir / f"{trace_id}.zst"
        if not trace_file.exists():
            # Index inconsistency — remove from index
            del self._index[trace_id]
            self._persist_index()
            return None

        return InferenceTrace.from_bytes(trace_file.read_bytes())

    def get_recent(self, n: int = 50) -> list[InferenceTrace]:
        """Get the N most recent traces.

        Args:
            n: Number of traces to retrieve.

        Returns:
            List of traces sorted by timestamp (newest first).
        """
        sorted_ids = sorted(
            self._index.keys(),
            key=lambda tid: self._index[tid]["timestamp"],
            reverse=True,
        )[:n]

        traces = []
        for tid in sorted_ids:
            trace = self.retrieve(tid)
            if trace:
                traces.append(trace)
        return traces

    def get_successful(self, min_decay_weight: float = 0.1) -> list[InferenceTrace]:
        """Get successful traces above minimum decay weight.

        Used by the Experience Replay Controller.

        Args:
            min_decay_weight: Minimum decay weight threshold.

        Returns:
            List of successful traces above threshold.
        """
        now = time.time()
        results = []
        for tid, meta in self._index.items():
            if not meta.get("success"):
                continue
            # Quick decay check from index metadata
            age = now - meta["timestamp"]
            if math.exp(-age / self.tau) < min_decay_weight:
                continue
            trace = self.retrieve(tid)
            if trace:
                results.append(trace)
        return results

    def get_maat_scores(self, n: int = 100) -> list[float]:
        """Get historical Maat scores for temporal consistency analysis.

        Used by the Meta-Maat Validator.

        Args:
            n: Maximum number of scores to return.

        Returns:
            List of Maat scores (most recent first).
        """
        sorted_ids = sorted(
            self._index.keys(),
            key=lambda tid: self._index[tid]["timestamp"],
            reverse=True,
        )[:n]
        return [self._index[tid]["maat_score"] for tid in sorted_ids]

    def trigger_mining(self) -> bool:
        """Check if pattern mining should be triggered.

        Returns True when 50+ new traces have accumulated since last mining.
        """
        stats = self._load_stats()
        traces_since_mining = self._total_stored - stats.get("last_mining_trace_count", 0)
        return traces_since_mining >= 50

    def mark_mining_complete(self) -> None:
        """Mark that pattern mining has been performed."""
        stats = self._load_stats()
        stats["last_mining_trace_count"] = self._total_stored
        stats["last_mining_timestamp"] = time.time()
        self._save_stats(stats)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_lowest(self) -> None:
        """Evict the trace with lowest harmonic-decay-weighted score."""
        if not self._index:
            return

        now = time.time()
        lowest_id = None
        lowest_score = float("inf")

        for tid, meta in self._index.items():
            age = now - meta["timestamp"]
            score = math.exp(-age / self.tau)
            if score < lowest_score:
                lowest_score = score
                lowest_id = tid

        if lowest_id:
            # Remove file
            trace_file = self._traces_dir / f"{lowest_id}.zst"
            if trace_file.exists():
                trace_file.unlink()
            # Remove from index
            del self._index[lowest_id]
            self._persist_index()
            self._total_evicted += 1

    def _load_index(self) -> dict[str, dict[str, Any]]:
        """Load index from disk."""
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _persist_index(self) -> None:
        """Write index to disk."""
        self._index_path.write_text(
            json.dumps(self._index, separators=(",", ":"))
        )

    def _load_stats(self) -> dict[str, Any]:
        """Load stats from disk."""
        if self._stats_path.exists():
            try:
                return json.loads(self._stats_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_stats(self, stats: dict[str, Any]) -> None:
        """Write stats to disk."""
        self._stats_path.write_text(json.dumps(stats, indent=2))
