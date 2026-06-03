"""Maat Validation Framework — 7-principle governance for SLCF operations.

Validates every write/distribution operation against 7 weighted principles.
Overall threshold: ≥ 0.9 weighted score required.
Per-principle minimum: configurable (default 0.5).

Produces audit trail entries stored in SLCF L5 governance layer,
with memory buffer fallback when L5 is unavailable.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MaatValidationError(Exception):
    """Raised when Maat validation fails."""

    def __init__(
        self,
        message: str,
        failed_principles: list[str] | None = None,
        overall_score: float = 0.0,
    ):
        self.failed_principles = failed_principles or []
        self.overall_score = overall_score
        super().__init__(message)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class ValidationContext:
    """Context provided to Maat validation for principle evaluation."""

    sigma_delta: float = 0.0  # Σδ(a) for page group
    kappa_effective: float = 1.0  # κ_eff for Row_Group
    beta_one: int = 0  # β₁ cycle count
    agent_count: int = 0
    edge_count: int = 0
    max_depth: int = 0
    operation: str = ""  # Operation being validated
    resource_id: str = ""  # Target SLCF resource
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MaatPrinciple:
    """A single Maat principle with weight and threshold."""

    name: str
    weight: float  # [0.0, 1.0], all weights sum to 1.0
    min_threshold: float = 0.5  # Configurable [0.0, 1.0]
    score: float = 0.0

    @property
    def passed(self) -> bool:
        """Whether this principle meets its minimum threshold."""
        return self.score >= self.min_threshold


@dataclass
class MaatResult:
    """Result of a Maat validation evaluation."""

    passed: bool
    overall_score: float
    principles: list[MaatPrinciple]
    failed_principles: list[str] = field(default_factory=list)
    operation: str = ""
    resource_id: str = ""

    @property
    def summary(self) -> str:
        """Human-readable summary."""
        status = "PASS" if self.passed else "FAIL"
        return (
            f"Maat [{status}] score={self.overall_score:.3f} "
            f"op={self.operation} failures={self.failed_principles}"
        )


@dataclass
class AuditEntry:
    """Audit trail entry for Maat validation decisions."""

    timestamp: float
    operation: str
    resource_id: str
    overall_score: float
    passed: bool
    principle_scores: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for SLCF L5 storage."""
        return {
            "timestamp": self.timestamp,
            "operation": self.operation,
            "resource_id": self.resource_id,
            "overall_score": self.overall_score,
            "passed": self.passed,
            "principle_scores": self.principle_scores,
        }


# ---------------------------------------------------------------------------
# Principle Evaluation Functions
# ---------------------------------------------------------------------------

# Default principle checks — bound to specific SLCF invariants
DEFAULT_PRINCIPLE_CHECKS: dict[str, Callable[[ValidationContext], float]] = {
    "truth": lambda ctx: 1.0 if ctx.sigma_delta <= 2.0 and ctx.kappa_effective >= 0.0 else 0.3,
    "justice": lambda ctx: 1.0 if ctx.agent_count > 0 else 0.5,
    "harmony": lambda ctx: 1.0 if ctx.kappa_effective >= 0.3 else ctx.kappa_effective / 0.3,
    "balance": lambda ctx: 1.0 if ctx.sigma_delta <= 2.0 else max(0.0, 1.0 - (ctx.sigma_delta - 2.0)),
    "order": lambda ctx: 1.0 if ctx.max_depth <= 16 else max(0.0, 1.0 - (ctx.max_depth - 16) / 16),
    "reciprocity": lambda ctx: 1.0 if ctx.beta_one == 0 else max(0.4, 1.0 - ctx.beta_one * 0.1),
    "propriety": lambda ctx: 1.0 if ctx.operation != "" else 0.5,
}


# ---------------------------------------------------------------------------
# Maat Validator
# ---------------------------------------------------------------------------


class MaatValidator:
    """7-principle validation framework for SLCF operations.

    Overall threshold: ≥ 0.9 weighted score required.
    Per-principle minimum: configurable (default 0.5 each).
    """

    PRINCIPLES = [
        "truth", "justice", "harmony",
        "balance", "order", "reciprocity", "propriety",
    ]
    OVERALL_THRESHOLD = 0.9
    MAX_BUFFER_SIZE = 10_000

    def __init__(
        self,
        weights: Optional[dict[str, float]] = None,
        thresholds: Optional[dict[str, float]] = None,
        principle_checks: Optional[dict[str, Callable[[ValidationContext], float]]] = None,
    ):
        """Initialize Maat validator.

        Args:
            weights: Per-principle weights. Must sum to 1.0.
                    Defaults to equal weights (1/7 each).
            thresholds: Per-principle minimum thresholds.
                       Defaults to 0.5 for each.
            principle_checks: Custom principle evaluation functions.
                            Defaults to SLCF-bound checks.
        """
        # Default equal weights
        default_weight = 1.0 / len(self.PRINCIPLES)
        self._weights = {p: default_weight for p in self.PRINCIPLES}
        if weights:
            total = sum(weights.values())
            if abs(total - 1.0) > 1e-6:
                # Normalize
                for k in weights:
                    weights[k] /= total
            self._weights.update(weights)

        # Default thresholds
        self._thresholds = {p: 0.5 for p in self.PRINCIPLES}
        if thresholds:
            self._thresholds.update(thresholds)

        # Principle check functions
        self._checks = dict(DEFAULT_PRINCIPLE_CHECKS)
        if principle_checks:
            self._checks.update(principle_checks)

        # Audit buffer (used when L5 storage is unavailable)
        self._audit_buffer: deque[AuditEntry] = deque(maxlen=self.MAX_BUFFER_SIZE)
        self._l5_available = False
        self._l5_storage: list[AuditEntry] = []

    @property
    def audit_buffer_size(self) -> int:
        """Number of entries in memory buffer."""
        return len(self._audit_buffer)

    def set_l5_available(self, available: bool) -> None:
        """Set L5 governance layer availability.

        If becoming available, flushes buffered entries.
        """
        was_unavailable = not self._l5_available
        self._l5_available = available
        if available and was_unavailable:
            self._flush_buffer()

    def validate(
        self,
        operation: str,
        context: ValidationContext,
    ) -> MaatResult:
        """Validate operation against all 7 Maat principles.

        Args:
            operation: Name of the operation being validated
            context: ValidationContext with metrics for evaluation

        Returns:
            MaatResult with pass/fail, per-principle scores, overall score.

        Raises:
            MaatValidationError: If overall score < 0.9 or any principle
                                below its minimum threshold.
        """
        context.operation = operation

        principles = []
        failed = []

        for name in self.PRINCIPLES:
            check_fn = self._checks.get(name, lambda _: 1.0)
            score = max(0.0, min(1.0, check_fn(context)))

            principle = MaatPrinciple(
                name=name,
                weight=self._weights[name],
                min_threshold=self._thresholds[name],
                score=score,
            )
            principles.append(principle)

            if not principle.passed:
                failed.append(name)

        # Compute overall weighted score
        overall = sum(p.weight * p.score for p in principles)

        # Determine pass/fail
        passed = overall >= self.OVERALL_THRESHOLD and len(failed) == 0

        result = MaatResult(
            passed=passed,
            overall_score=overall,
            principles=principles,
            failed_principles=failed,
            operation=operation,
            resource_id=context.resource_id,
        )

        # Create and store audit entry
        self._store_audit(result, context.resource_id)

        # Raise if failed
        if not passed:
            principle_details = "; ".join(
                f"{p.name}={p.score:.3f} (min={p.min_threshold})"
                for p in principles
                if not p.passed
            )
            score_msg = f"overall={overall:.3f} (min={self.OVERALL_THRESHOLD})"
            if failed:
                msg = f"Maat validation failed for '{operation}': {score_msg}; failed principles: {principle_details}"
            else:
                msg = f"Maat validation failed for '{operation}': {score_msg}"
            raise MaatValidationError(
                msg,
                failed_principles=failed,
                overall_score=overall,
            )

        return result

    def evaluate(
        self,
        operation: str,
        context: ValidationContext,
    ) -> MaatResult:
        """Evaluate without raising — returns result regardless of pass/fail.

        Use this when you want to check validation without blocking.
        """
        context.operation = operation

        principles = []
        failed = []

        for name in self.PRINCIPLES:
            check_fn = self._checks.get(name, lambda _: 1.0)
            score = max(0.0, min(1.0, check_fn(context)))

            principle = MaatPrinciple(
                name=name,
                weight=self._weights[name],
                min_threshold=self._thresholds[name],
                score=score,
            )
            principles.append(principle)

            if not principle.passed:
                failed.append(name)

        overall = sum(p.weight * p.score for p in principles)
        passed = overall >= self.OVERALL_THRESHOLD and len(failed) == 0

        result = MaatResult(
            passed=passed,
            overall_score=overall,
            principles=principles,
            failed_principles=failed,
            operation=operation,
            resource_id=context.resource_id,
        )

        self._store_audit(result, context.resource_id)
        return result

    def _store_audit(self, result: MaatResult, resource_id: str) -> None:
        """Store audit entry to L5 or buffer."""
        entry = AuditEntry(
            timestamp=time.time(),
            operation=result.operation,
            resource_id=resource_id,
            overall_score=result.overall_score,
            passed=result.passed,
            principle_scores={p.name: p.score for p in result.principles},
        )

        if self._l5_available:
            self._l5_storage.append(entry)
        else:
            self._audit_buffer.append(entry)

    def _flush_buffer(self) -> None:
        """Flush buffered audit entries to L5 storage."""
        while self._audit_buffer:
            entry = self._audit_buffer.popleft()
            self._l5_storage.append(entry)

    def get_audit_trail(self) -> list[dict[str, Any]]:
        """Retrieve all audit entries (L5 + buffer)."""
        entries = [e.to_dict() for e in self._l5_storage]
        entries.extend(e.to_dict() for e in self._audit_buffer)
        return entries
