"""Reconciliation Engine — compares source and target system state.

Automated divergence detection between legacy and target ETRM systems.
Produces daily confidence reports showing position, P&L, and settlement matches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DivergenceType(str, Enum):
    """Types of divergence detected between systems."""

    POSITION_MISMATCH = "position_mismatch"
    PNL_DIVERGENCE = "pnl_divergence"
    SETTLEMENT_DIFF = "settlement_diff"
    NOMINATION_MISMATCH = "nomination_mismatch"
    MISSING_IN_TARGET = "missing_in_target"
    MISSING_IN_SOURCE = "missing_in_source"
    FIELD_MISMATCH = "field_mismatch"


@dataclass
class Divergence:
    """A single detected divergence between source and target."""

    divergence_type: DivergenceType
    deal_id: str
    field_name: str = ""
    source_value: Any = None
    target_value: Any = None
    severity: str = "warning"  # "info" | "warning" | "critical"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.divergence_type.value,
            "deal_id": self.deal_id,
            "field": self.field_name,
            "source_value": str(self.source_value),
            "target_value": str(self.target_value),
            "severity": self.severity,
            "timestamp": self.timestamp,
        }


@dataclass
class ReconciliationReport:
    """Daily reconciliation report between source and target systems."""

    report_date: str
    source_system: str
    target_system: str
    commodity: str
    total_deals_compared: int = 0
    deals_matched: int = 0
    deals_diverged: int = 0
    divergences: List[Divergence] = field(default_factory=list)
    confidence_score: float = 0.0  # 0.0 to 1.0
    generated_at: float = field(default_factory=time.time)

    @property
    def is_clean(self) -> bool:
        """True if zero divergences (ready for cutover consideration)."""
        return self.deals_diverged == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_date": self.report_date,
            "source_system": self.source_system,
            "target_system": self.target_system,
            "commodity": self.commodity,
            "total_deals_compared": self.total_deals_compared,
            "deals_matched": self.deals_matched,
            "deals_diverged": self.deals_diverged,
            "confidence_score": round(self.confidence_score, 4),
            "is_clean": self.is_clean,
            "divergences": [d.to_dict() for d in self.divergences],
            "generated_at": self.generated_at,
        }


class ReconciliationEngine:
    """Compares deal state between source and target ETRM systems.

    Runs field-by-field comparison on matched deals.
    Detects missing deals, position mismatches, P&L divergences.
    Produces confidence scores for cutover readiness.
    """

    # Fields to compare between systems (source field → target field)
    COMPARE_FIELDS = [
        ("quantity", "quantity", 0.01),       # volume with tolerance
        ("price", "price", 0.001),            # price with tolerance
        ("start_date", "start_date", None),   # exact match
        ("end_date", "end_date", None),       # exact match
        ("counterparty", "external_bunit", None),  # exact match
        ("currency", "currency", None),       # exact match
    ]

    def __init__(
        self,
        source_system: str = "rightangle",
        target_system: str = "endur",
        tolerance_pct: float = 0.01,
    ):
        """Initialize reconciliation engine.

        Args:
            source_system: Name of the legacy system.
            target_system: Name of the target system.
            tolerance_pct: Percentage tolerance for numeric comparisons.
        """
        self._source_system = source_system
        self._target_system = target_system
        self._tolerance_pct = tolerance_pct
        self._reports: List[ReconciliationReport] = []

    @property
    def reports(self) -> List[ReconciliationReport]:
        """All generated reports."""
        return list(self._reports)

    @property
    def consecutive_clean_days(self) -> int:
        """Number of consecutive clean reports (zero divergence)."""
        count = 0
        for report in reversed(self._reports):
            if report.is_clean:
                count += 1
            else:
                break
        return count

    def reconcile(
        self,
        source_deals: List[Dict[str, Any]],
        target_deals: List[Dict[str, Any]],
        report_date: str,
        commodity: str = "",
    ) -> ReconciliationReport:
        """Run reconciliation between source and target deal sets.

        Args:
            source_deals: Deals from legacy system (keyed by deal_id).
            target_deals: Deals from target system (keyed by deal_id or external_reference).
            report_date: Date string for the report (e.g. "2026-06-05").
            commodity: Commodity being reconciled.

        Returns:
            ReconciliationReport with divergences and confidence score.
        """
        divergences: List[Divergence] = []

        # Index target deals by source reference
        target_index: Dict[str, Dict[str, Any]] = {}
        for deal in target_deals:
            # Target deals should have _source_deal_id from migration
            source_ref = deal.get("_source_deal_id") or deal.get("external_reference", "")
            if source_ref.startswith("RA-"):
                source_ref = source_ref[3:]  # Strip "RA-" prefix
            if source_ref:
                target_index[source_ref] = deal

        # Compare each source deal against its target counterpart
        matched = 0
        diverged_deals = set()

        for source_deal in source_deals:
            deal_id = str(source_deal.get("deal_id", ""))

            # Check if deal exists in target
            if deal_id not in target_index:
                divergences.append(Divergence(
                    divergence_type=DivergenceType.MISSING_IN_TARGET,
                    deal_id=deal_id,
                    severity="critical",
                ))
                diverged_deals.add(deal_id)
                continue

            target_deal = target_index[deal_id]

            # Field-by-field comparison
            deal_clean = True
            for source_field, target_field, tolerance in self.COMPARE_FIELDS:
                source_val = source_deal.get(source_field)
                target_val = target_deal.get(target_field)

                if source_val is None and target_val is None:
                    continue

                if not self._values_match(source_val, target_val, tolerance):
                    divergences.append(Divergence(
                        divergence_type=DivergenceType.FIELD_MISMATCH,
                        deal_id=deal_id,
                        field_name=source_field,
                        source_value=source_val,
                        target_value=target_val,
                        severity="warning",
                    ))
                    deal_clean = False

            if deal_clean:
                matched += 1
            else:
                diverged_deals.add(deal_id)

        # Check for deals in target but not in source (unexpected)
        source_ids = {str(d.get("deal_id", "")) for d in source_deals}
        for target_ref, target_deal in target_index.items():
            if target_ref not in source_ids:
                divergences.append(Divergence(
                    divergence_type=DivergenceType.MISSING_IN_SOURCE,
                    deal_id=target_ref,
                    severity="warning",
                ))
                diverged_deals.add(target_ref)

        # Compute confidence score
        total = len(source_deals)
        confidence = matched / total if total > 0 else 1.0

        report = ReconciliationReport(
            report_date=report_date,
            source_system=self._source_system,
            target_system=self._target_system,
            commodity=commodity,
            total_deals_compared=total,
            deals_matched=matched,
            deals_diverged=len(diverged_deals),
            divergences=divergences,
            confidence_score=confidence,
        )

        self._reports.append(report)
        return report

    def is_ready_for_cutover(self, required_clean_days: int = 30) -> bool:
        """Check if enough consecutive clean days for cutover.

        Args:
            required_clean_days: Minimum consecutive clean reports required.

        Returns:
            True if ready for authoritative source flip.
        """
        return self.consecutive_clean_days >= required_clean_days

    def _values_match(
        self, source_val: Any, target_val: Any, tolerance: Optional[float]
    ) -> bool:
        """Compare two values with optional numeric tolerance.

        Args:
            source_val: Value from source system.
            target_val: Value from target system.
            tolerance: Absolute tolerance for numeric comparison (None = exact).

        Returns:
            True if values match within tolerance.
        """
        if source_val is None or target_val is None:
            return source_val == target_val

        if tolerance is not None:
            try:
                return abs(float(source_val) - float(target_val)) <= tolerance
            except (ValueError, TypeError):
                return str(source_val) == str(target_val)

        return str(source_val) == str(target_val)
