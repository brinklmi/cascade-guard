"""Novelty Detector — Bloom filter of composite signatures.

Fast O(1) lookup to determine if a query's composite signature has been
seen before. If novel → skip experience replay, run full resonance.

Calibrated for ~100-500 queries/day with <1% false positive rate.
At 10 bits per entry and 10K capacity: ~12.5 KB memory footprint.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default capacity: 10K signatures (matches history buffer max)
DEFAULT_CAPACITY = 10_000

# Target false positive rate: 0.5% (conservative for safety)
DEFAULT_FP_RATE = 0.005


# ---------------------------------------------------------------------------
# Bloom Filter Implementation
# ---------------------------------------------------------------------------


class NoveltyDetector:
    """Bloom filter for detecting novel composite signatures.

    A signature is "novel" if it hasn't been seen before in the
    inference history. Novel queries skip experience replay and
    run the full resonance path.

    The Bloom filter is sized for:
    - 10K entries (matching history buffer capacity)
    - 0.5% false positive rate
    - ~14.4 KB memory (14,351 bytes / ~115K bits)
    - 7 hash functions (optimal for this size/fp trade-off)
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        fp_rate: float = DEFAULT_FP_RATE,
        persist_path: Optional[Path | str] = None,
    ):
        """Initialize the novelty detector.

        Args:
            capacity: Expected number of unique signatures.
            fp_rate: Target false positive rate.
            persist_path: Optional file path for persistence.
        """
        self.capacity = capacity
        self.fp_rate = fp_rate

        # Compute optimal parameters
        # m = -(n * ln(p)) / (ln(2)^2)
        self._num_bits = self._optimal_bits(capacity, fp_rate)
        # k = (m / n) * ln(2)
        self._num_hashes = self._optimal_hashes(self._num_bits, capacity)

        # Bit array (stored as bytearray)
        num_bytes = (self._num_bits + 7) // 8
        self._bits = bytearray(num_bytes)

        # Stats
        self._items_added = 0

        # Persistence
        self._persist_path = Path(persist_path) if persist_path else None
        if self._persist_path and self._persist_path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Number of items added to the filter."""
        return self._items_added

    @property
    def memory_bytes(self) -> int:
        """Memory footprint of the bit array."""
        return len(self._bits)

    def is_novel(self, composite_signature: str) -> bool:
        """Check if a composite signature is novel (unseen).

        Args:
            composite_signature: The composite_space_id from an InferenceTrace.

        Returns:
            True if the signature is definitely novel (not in filter).
            False if the signature has probably been seen before.
        """
        return not self._check(composite_signature)

    def register(self, composite_signature: str) -> None:
        """Register a composite signature as seen.

        Args:
            composite_signature: The composite_space_id to register.
        """
        self._add(composite_signature)
        self._items_added += 1

        # Auto-persist if path configured
        if self._persist_path and self._items_added % 100 == 0:
            self.save()

    def check_and_register(self, composite_signature: str) -> bool:
        """Atomic check-then-register.

        Returns True if novel (was not in filter), then registers it.
        Returns False if already seen.
        """
        novel = self.is_novel(composite_signature)
        if novel:
            self.register(composite_signature)
        return novel

    def estimated_fp_rate(self) -> float:
        """Estimate current false positive rate given items added.

        Returns:
            Estimated FP rate based on current fill level.
        """
        if self._items_added == 0:
            return 0.0
        # (1 - e^(-kn/m))^k
        exponent = -self._num_hashes * self._items_added / self._num_bits
        return (1 - math.exp(exponent)) ** self._num_hashes

    def save(self) -> None:
        """Persist Bloom filter to disk."""
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        # Header: num_bits(4) + num_hashes(4) + items_added(4) + bits
        header = struct.pack("<III", self._num_bits, self._num_hashes, self._items_added)
        self._persist_path.write_bytes(header + bytes(self._bits))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _add(self, key: str) -> None:
        """Add a key to the Bloom filter."""
        for idx in self._hash_indices(key):
            byte_idx = idx // 8
            bit_idx = idx % 8
            self._bits[byte_idx] |= (1 << bit_idx)

    def _check(self, key: str) -> bool:
        """Check if a key is probably in the Bloom filter."""
        for idx in self._hash_indices(key):
            byte_idx = idx // 8
            bit_idx = idx % 8
            if not (self._bits[byte_idx] & (1 << bit_idx)):
                return False
        return True

    def _hash_indices(self, key: str) -> list[int]:
        """Generate k hash indices for a key using double hashing.

        Uses SHA-256 split into two 64-bit values for double hashing:
        h(i) = (h1 + i * h2) mod m
        """
        digest = hashlib.sha256(key.encode()).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:16], "little")

        indices = []
        for i in range(self._num_hashes):
            idx = (h1 + i * h2) % self._num_bits
            indices.append(idx)
        return indices

    def _load(self) -> None:
        """Load Bloom filter from disk."""
        if not self._persist_path or not self._persist_path.exists():
            return
        data = self._persist_path.read_bytes()
        if len(data) < 12:
            return
        num_bits, num_hashes, items_added = struct.unpack("<III", data[:12])
        self._num_bits = num_bits
        self._num_hashes = num_hashes
        self._items_added = items_added
        self._bits = bytearray(data[12:])

    @staticmethod
    def _optimal_bits(n: int, p: float) -> int:
        """Compute optimal number of bits: m = -(n * ln(p)) / (ln(2)^2)."""
        if n <= 0 or p <= 0 or p >= 1:
            return 1024  # Fallback
        m = -(n * math.log(p)) / (math.log(2) ** 2)
        return max(64, int(math.ceil(m)))

    @staticmethod
    def _optimal_hashes(m: int, n: int) -> int:
        """Compute optimal number of hash functions: k = (m/n) * ln(2)."""
        if n <= 0:
            return 1
        k = (m / n) * math.log(2)
        return max(1, min(20, int(round(k))))
