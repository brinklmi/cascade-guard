"""Experience Replay Controller — replays successful resonance paths.

When a query's composite signature is similar to a previously successful
inference (cosine similarity > 0.85 in composite space), replays the
historical path rather than running full resonance.

Uses Beta(α, β) conjugate priors per gate threshold for Bayesian updating
of success rates. Replays only when success_rate > 0.9.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from cascade_guard.adaptive.history_buffer import InferenceHistoryBuffer, InferenceTrace
from cascade_guard.adaptive.meta_maat import MetaMaatValidator, TemporalConsistencyResult
from cascade_guard.adaptive.novelty_detector import NoveltyDetector


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cosine similarity threshold in composite space (harmonic 4D + embedding 768D)
SIMILARITY_THRESHOLD = 0.85

# Success rate threshold for replay
REPLAY_SUCCESS_THRESHOLD = 0.9

# Initial Beta prior: Beta(1,1) = uniform (no prior bias)
INITIAL_ALPHA = 1.0
INITIAL_BETA = 1.0


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class BetaPrior:
    """Beta(α, β) conjugate prior for a gate threshold.

    Posterior mean = α / (α + β) = estimated success rate.
    Updated incrementally: α += 1 on success, β += 1 on failure.
    """

    alpha: float = INITIAL_ALPHA
    beta: float = INITIAL_BETA

    @property
    def success_rate(self) -> float:
        """Posterior mean: estimated probability of success."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def confidence(self) -> float:
        """Confidence in the estimate (inverse variance proxy)."""
        total = self.alpha + self.beta
        return 1.0 - 1.0 / (total + 1)

    @property
    def sample_count(self) -> int:
        """Total observations (excluding prior pseudo-counts)."""
        return int(self.alpha + self.beta - INITIAL_ALPHA - INITIAL_BETA)

    def update_success(self) -> None:
        """Record a successful outcome."""
        self.alpha += 1.0

    def update_failure(self) -> None:
        """Record a failed outcome."""
        self.beta += 1.0

    def should_replay(self) -> bool:
        """Whether this gate's success rate warrants replay."""
        return self.success_rate > REPLAY_SUCCESS_THRESHOLD


@dataclass
class ReplayDecision:
    """Result of the replay controller's decision for a query."""

    should_replay: bool
    reason: str
    matched_trace_id: Optional[str] = None
    similarity: float = 0.0
    gate_success_rate: float = 0.0
    temporal_consistency: Optional[TemporalConsistencyResult] = None


# ---------------------------------------------------------------------------
# Experience Replay Controller
# ---------------------------------------------------------------------------


class ExperienceReplayController:
    """Decides whether to replay a successful path or run full resonance.

    Decision flow:
    1. Novelty check (Bloom filter) → if novel, full resonance
    2. Similarity search in history buffer → find closest successful trace
    3. Check gate's Beta prior → if success_rate > 0.9, replay
    4. Meta-Maat temporal check → if anomaly, full resonance

    The controller maintains per-gate Beta priors that are updated
    after each inference completes (success or failure).
    """

    def __init__(
        self,
        history_buffer: InferenceHistoryBuffer,
        novelty_detector: NoveltyDetector,
        meta_maat: MetaMaatValidator,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ):
        """Initialize the replay controller.

        Args:
            history_buffer: The inference history buffer.
            novelty_detector: Bloom filter for novelty detection.
            meta_maat: Meta-Maat validator for temporal consistency.
            similarity_threshold: Minimum cosine similarity for replay.
        """
        self.history = history_buffer
        self.novelty = novelty_detector
        self.meta_maat = meta_maat
        self.similarity_threshold = similarity_threshold

        # Per-gate Beta priors: gate_action → BetaPrior
        self._gate_priors: dict[str, BetaPrior] = {}

    def decide(
        self,
        query_embedding: list[float],
        harmonic_signature: list[float],
        current_maat_score: Optional[float] = None,
    ) -> ReplayDecision:
        """Decide whether to replay a successful path or run full resonance.

        Args:
            query_embedding: 768D embedding from nomic-embed-text.
            harmonic_signature: 4D harmonic signature [432, 528, 741, 852].
            current_maat_score: Optional current Maat score for temporal check.

        Returns:
            ReplayDecision with recommendation.
        """
        # Build composite signature
        composite_sig = self._build_composite_id(harmonic_signature, query_embedding)

        # Step 1: Novelty check
        if self.novelty.is_novel(composite_sig):
            self.novelty.register(composite_sig)
            return ReplayDecision(
                should_replay=False,
                reason="novel_signature",
            )

        # Step 2: Temporal consistency check (if score available)
        if current_maat_score is not None:
            historical_scores = self.history.get_maat_scores(n=100)
            temporal_result = self.meta_maat.validate(current_maat_score, historical_scores)
            if temporal_result.is_anomaly:
                return ReplayDecision(
                    should_replay=False,
                    reason="temporal_anomaly",
                    temporal_consistency=temporal_result,
                )

        # Step 3: Find similar successful trace
        successful_traces = self.history.get_successful(min_decay_weight=0.1)
        best_match = self._find_best_match(
            query_embedding, harmonic_signature, successful_traces
        )

        if best_match is None:
            return ReplayDecision(
                should_replay=False,
                reason="no_similar_success_found",
            )

        matched_trace, similarity = best_match

        # Step 4: Check gate prior
        gate_action = matched_trace.gate_action
        prior = self._get_or_create_prior(gate_action)

        if not prior.should_replay():
            return ReplayDecision(
                should_replay=False,
                reason=f"gate_success_rate_too_low ({prior.success_rate:.2f})",
                matched_trace_id=matched_trace.trace_id,
                similarity=similarity,
                gate_success_rate=prior.success_rate,
            )

        # All checks passed — recommend replay
        return ReplayDecision(
            should_replay=True,
            reason="replay_recommended",
            matched_trace_id=matched_trace.trace_id,
            similarity=similarity,
            gate_success_rate=prior.success_rate,
        )

    def record_outcome(self, gate_action: str, success: bool) -> None:
        """Record the outcome of an inference for Bayesian updating.

        Call this after every inference completes.

        Args:
            gate_action: The gate action that was taken.
            success: Whether the inference was successful
                    (maat_passed AND confidence >= 0.95).
        """
        prior = self._get_or_create_prior(gate_action)
        if success:
            prior.update_success()
        else:
            prior.update_failure()

    def get_gate_stats(self) -> dict[str, dict[str, float]]:
        """Get statistics for all gate priors."""
        return {
            gate: {
                "success_rate": prior.success_rate,
                "confidence": prior.confidence,
                "samples": prior.sample_count,
                "alpha": prior.alpha,
                "beta": prior.beta,
            }
            for gate, prior in self._gate_priors.items()
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_best_match(
        self,
        query_embedding: list[float],
        harmonic_signature: list[float],
        candidates: list[InferenceTrace],
    ) -> Optional[tuple[InferenceTrace, float]]:
        """Find the most similar successful trace above threshold.

        Uses cosine similarity in composite space (4D harmonic + 768D embedding).
        """
        if not candidates:
            return None

        # Build query vector in composite space
        query_vector = harmonic_signature + query_embedding

        best_trace = None
        best_sim = -1.0

        for trace in candidates:
            # Build candidate vector
            candidate_vector = trace.harmonic_signature + trace.query_embedding

            # Skip if dimensions don't match
            if len(candidate_vector) != len(query_vector):
                continue

            sim = self._cosine_similarity(query_vector, candidate_vector)
            if sim > best_sim:
                best_sim = sim
                best_trace = trace

        if best_trace is not None and best_sim >= self.similarity_threshold:
            return (best_trace, best_sim)
        return None

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a < 1e-10 or norm_b < 1e-10:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _build_composite_id(
        harmonic_signature: list[float], query_embedding: list[float]
    ) -> str:
        """Build a string composite ID for Bloom filter lookup."""
        # Quantize to reduce sensitivity to floating point noise
        h_quantized = [f"{v:.2f}" for v in harmonic_signature]
        # Use first 8 dims of embedding for Bloom key (full 768D is too long)
        e_quantized = [f"{v:.3f}" for v in query_embedding[:8]]
        return "|".join(h_quantized + e_quantized)

    def _get_or_create_prior(self, gate_action: str) -> BetaPrior:
        """Get or create a Beta prior for a gate action."""
        if gate_action not in self._gate_priors:
            self._gate_priors[gate_action] = BetaPrior()
        return self._gate_priors[gate_action]
