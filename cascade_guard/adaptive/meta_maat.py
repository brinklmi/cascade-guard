"""Meta-Maat Validator — 8th pillar: Temporal Consistency.

Validates that current inference Maat scores align with historical patterns.
Uses z-score anomaly detection: |z| > 2 triggers fallback to full resonance.

This extends the existing 7-pillar MaatValidator with temporal awareness —
the system can now detect when its own performance deviates from its
established baseline.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# z-score threshold for anomaly detection
Z_SCORE_THRESHOLD = 2.0

# Minimum number of historical scores needed for meaningful statistics
MIN_HISTORY_SIZE = 10


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class TemporalConsistencyResult:
    """Result of temporal consistency validation."""

    is_consistent: bool
    z_score: float
    current_score: float
    historical_mean: float
    historical_std: float
    sample_size: int
    action: str  # "proceed" or "fallback_full_resonance"
    message: str = ""

    @property
    def is_anomaly(self) -> bool:
        """Whether the current score is a statistical anomaly."""
        return abs(self.z_score) > Z_SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# Meta-Maat Validator
# ---------------------------------------------------------------------------


class MetaMaatValidator:
    """Temporal Consistency validator — the 8th Maat pillar.

    Tracks historical Maat scores and detects when current inference
    deviates significantly from the established baseline.

    Decision logic:
        - |z| <= 2: Consistent → proceed with experience replay
        - |z| > 2: Anomaly → fallback to full resonance, log anomaly

    The z-score measures how many standard deviations the current Maat
    score is from the historical mean. A high |z| indicates the system
    is behaving differently from its recent history, which could signal:
        - Distribution shift in incoming queries
        - Model degradation
        - Adversarial inputs
        - Legitimate novel territory (captured by novelty detector)
    """

    def __init__(self, z_threshold: float = Z_SCORE_THRESHOLD):
        """Initialize Meta-Maat validator.

        Args:
            z_threshold: z-score threshold for anomaly detection.
        """
        self.z_threshold = z_threshold
        self._anomaly_log: list[dict] = []

    def validate(
        self,
        current_maat_score: float,
        historical_scores: list[float],
    ) -> TemporalConsistencyResult:
        """Validate temporal consistency of the current Maat score.

        Args:
            current_maat_score: The Maat score from the current inference.
            historical_scores: List of recent historical Maat scores
                             (from InferenceHistoryBuffer.get_maat_scores()).

        Returns:
            TemporalConsistencyResult with action recommendation.
        """
        sample_size = len(historical_scores)

        # Insufficient history — cannot compute meaningful statistics
        if sample_size < MIN_HISTORY_SIZE:
            return TemporalConsistencyResult(
                is_consistent=True,
                z_score=0.0,
                current_score=current_maat_score,
                historical_mean=current_maat_score,
                historical_std=0.0,
                sample_size=sample_size,
                action="proceed",
                message=f"Insufficient history ({sample_size}/{MIN_HISTORY_SIZE}) — proceeding",
            )

        # Compute statistics
        mean = sum(historical_scores) / sample_size
        variance = sum((s - mean) ** 2 for s in historical_scores) / sample_size
        std = math.sqrt(variance) if variance > 0 else 0.0

        # Avoid division by zero (constant history = no deviation possible)
        if std < 1e-10:
            return TemporalConsistencyResult(
                is_consistent=True,
                z_score=0.0,
                current_score=current_maat_score,
                historical_mean=mean,
                historical_std=std,
                sample_size=sample_size,
                action="proceed",
                message="Constant historical baseline — no deviation detectable",
            )

        # Compute z-score
        z = (current_maat_score - mean) / std

        # Decision
        is_anomaly = abs(z) > self.z_threshold
        action = "fallback_full_resonance" if is_anomaly else "proceed"

        if is_anomaly:
            direction = "below" if z < 0 else "above"
            message = (
                f"Anomaly detected: current score {current_maat_score:.3f} is "
                f"{abs(z):.1f}σ {direction} mean ({mean:.3f} ± {std:.3f})"
            )
            self._log_anomaly(current_maat_score, z, mean, std)
        else:
            message = (
                f"Temporally consistent: z={z:.2f} within ±{self.z_threshold}"
            )

        return TemporalConsistencyResult(
            is_consistent=not is_anomaly,
            z_score=z,
            current_score=current_maat_score,
            historical_mean=mean,
            historical_std=std,
            sample_size=sample_size,
            action=action,
            message=message,
        )

    @property
    def anomaly_count(self) -> int:
        """Number of anomalies detected."""
        return len(self._anomaly_log)

    @property
    def anomaly_log(self) -> list[dict]:
        """Full anomaly log."""
        return self._anomaly_log.copy()

    def _log_anomaly(
        self, score: float, z: float, mean: float, std: float
    ) -> None:
        """Log an anomaly for later analysis by pattern mining."""
        self._anomaly_log.append({
            "timestamp": time.time(),
            "score": score,
            "z_score": z,
            "mean": mean,
            "std": std,
        })
