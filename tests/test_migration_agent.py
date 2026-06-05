"""Tests for ETRM Migration Agent and Reconciliation Engine.

Verifies:
1. RightAngle → Endur schema transformation
2. Migration agent event processing pipeline
3. CascadeGuard envelope attachment
4. Commodity filtering
5. Reconciliation: match detection, divergence detection, confidence scoring
6. Cutover readiness (consecutive clean days)
"""

import pytest

from cascade_guard.mcp_proxy.migration.agent import (
    ETRMEvent,
    EventType,
    MigrationAgent,
    MigrationResult,
    RightAngleToEndurTransformer,
)
from cascade_guard.mcp_proxy.migration.reconciler import (
    Divergence,
    DivergenceType,
    ReconciliationEngine,
    ReconciliationReport,
)


class TestRightAngleToEndurTransformer:
    """Schema transformation from RightAngle to Endur."""

    def test_deal_created_maps_to_create_deal(self):
        """DEAL_CREATED event maps to endur/create_deal tool."""
        transformer = RightAngleToEndurTransformer()
        event = ETRMEvent(
            event_type=EventType.DEAL_CREATED,
            source_system="rightangle",
            deal_id="12345",
            commodity="NaturalGas",
        )
        tool = transformer.get_target_tool(event)
        assert tool == "endur/create_deal"

    def test_deal_confirmed_maps_to_confirm_deal(self):
        """DEAL_CONFIRMED maps to endur/confirm_deal."""
        transformer = RightAngleToEndurTransformer()
        event = ETRMEvent(event_type=EventType.DEAL_CONFIRMED, source_system="rightangle", deal_id="1")
        assert transformer.get_target_tool(event) == "endur/confirm_deal"

    def test_transform_common_fields(self):
        """Transform maps core fields from RA schema to Endur schema."""
        transformer = RightAngleToEndurTransformer()
        event = ETRMEvent(
            event_type=EventType.DEAL_CREATED,
            source_system="rightangle",
            deal_id="99001",
            commodity="NaturalGas",
            counterparty="BP Energy",
            portfolio="GasTrading",
            payload={
                "deal_num": "99001",
                "deal_type": "physical",
                "counterparty": "BP Energy",
                "internal_company": "MyFirm",
                "book": "GasTrading",
                "pipeline": "Transco",
                "delivery_point": "Henry Hub",
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
                "trade_date": "2026-06-05",
                "volume": 10000.0,
                "volume_unit": "MMBtu",
                "price": 2.85,
                "price_unit": "$/MMBtu",
                "currency": "USD",
            },
        )

        args = transformer.transform(event, "endur/create_deal")

        assert args["external_reference"] == "RA-99001"
        assert args["ins_type"] == "PHYS"
        assert args["tran_type"] == "NEW"
        assert args["tran_status"] == "Pending"
        assert args["external_bunit"] == "BP Energy"
        assert args["internal_portfolio"] == "GasTrading"
        assert args["commodity"] == "NaturalGas"
        assert args["pipeline"] == "Transco"
        assert args["location"] == "Henry Hub"
        assert args["quantity"] == 10000.0
        assert args["price"] == 2.85
        assert args["currency"] == "USD"
        assert args["_migration_source"] == "rightangle"
        assert args["_source_deal_id"] == "99001"

    def test_financial_deal_type(self):
        """Financial deal type maps to FIN ins_type."""
        transformer = RightAngleToEndurTransformer()
        event = ETRMEvent(
            event_type=EventType.DEAL_CREATED,
            source_system="rightangle",
            deal_id="500",
            payload={"deal_type": "financial"},
        )
        args = transformer.transform(event, "endur/create_deal")
        assert args["ins_type"] == "FIN"

    def test_swap_deal_type(self):
        """Swap deal type maps to SWAP ins_type."""
        transformer = RightAngleToEndurTransformer()
        event = ETRMEvent(
            event_type=EventType.DEAL_CREATED,
            source_system="rightangle",
            deal_id="501",
            payload={"deal_type": "swap"},
        )
        args = transformer.transform(event, "endur/create_deal")
        assert args["ins_type"] == "SWAP"


class TestMigrationAgent:
    """Migration agent event processing."""

    def test_process_event_dry_run(self):
        """Process event without proxy (dry-run mode) succeeds."""
        agent = MigrationAgent(
            agent_id="test-agent",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
            mcp_proxy_call=None,  # Dry-run
        )

        event = ETRMEvent(
            event_type=EventType.DEAL_CREATED,
            source_system="rightangle",
            deal_id="1001",
            commodity="NaturalGas",
            payload={"deal_type": "physical", "volume": 5000},
        )

        result = agent.process_event(event)
        assert result.success is True
        assert result.governed is False  # No proxy in dry-run
        assert result.target_tool == "endur/create_deal"
        assert result.latency_ms > 0

    def test_commodity_filter(self):
        """Events for non-matching commodities are filtered."""
        agent = MigrationAgent(
            agent_id="gas-only-agent",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
            commodity_filter=["NaturalGas"],
        )

        gas_event = ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id="1", commodity="NaturalGas")
        power_event = ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id="2", commodity="Power")

        gas_result = agent.process_event(gas_event)
        power_result = agent.process_event(power_event)

        assert gas_result.success is True
        assert gas_result.error == ""
        assert power_result.success is True
        assert power_result.error == "filtered_by_commodity"

    def test_stats_tracking(self):
        """Agent tracks processing statistics."""
        agent = MigrationAgent(
            agent_id="stats-agent",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
            commodity_filter=["Gas"],
        )

        agent.process_event(ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id="1", commodity="Gas"))
        agent.process_event(ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id="2", commodity="Power"))
        agent.process_event(ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id="3", commodity="Gas"))

        stats = agent.stats
        assert stats["processed"] == 3
        assert stats["succeeded"] == 2  # Gas deals
        assert stats["filtered"] == 1  # Power deal

    def test_process_with_mock_proxy(self):
        """Process event through mock CascadeGuard proxy."""
        proxy_calls = []

        def mock_proxy(request):
            proxy_calls.append(request)
            return {"result": {"status": "created"}}

        agent = MigrationAgent(
            agent_id="governed-agent",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
            mcp_proxy_call=mock_proxy,
        )

        event = ETRMEvent(
            event_type=EventType.DEAL_CREATED,
            source_system="rightangle",
            deal_id="2001",
            commodity="NaturalGas",
            payload={"deal_type": "physical"},
        )

        result = agent.process_event(event)

        assert result.success is True
        assert result.governed is True
        assert len(proxy_calls) == 1

        # Verify CascadeGuard envelope was attached
        request = proxy_calls[0]
        assert request["params"]["_cascadeguard"]["agent_id"] == "governed-agent"
        assert request["params"]["_cascadeguard"]["schema_version"] == "1.0"
        assert request["params"]["name"] == "endur/create_deal"

    def test_proxy_error_handled(self):
        """Proxy error response is captured gracefully."""
        def error_proxy(request):
            return {"error": {"code": -32000, "message": "agent_velocity_exceeded"}}

        agent = MigrationAgent(
            agent_id="throttled-agent",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
            mcp_proxy_call=error_proxy,
        )

        event = ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id="3001")
        result = agent.process_event(event)

        assert result.success is False
        assert "velocity" in result.error
        assert result.governed is True

    def test_batch_processing(self):
        """Batch processing handles multiple events."""
        agent = MigrationAgent(
            agent_id="batch-agent",
            source_system="rightangle",
            target_server="endur",
            transformer=RightAngleToEndurTransformer(),
        )

        events = [
            ETRMEvent(event_type=EventType.DEAL_CREATED, source_system="rightangle", deal_id=f"{i}", commodity="Gas")
            for i in range(5)
        ]

        results = agent.process_batch(events)
        assert len(results) == 5
        assert all(r.success for r in results)


class TestReconciliationEngine:
    """Reconciliation between source and target systems."""

    def test_perfect_match(self):
        """All deals match — confidence 1.0."""
        engine = ReconciliationEngine()

        source = [
            {"deal_id": "1", "quantity": 10000, "price": 2.85, "start_date": "2026-07-01", "end_date": "2026-07-31", "counterparty": "BP", "currency": "USD"},
            {"deal_id": "2", "quantity": 5000, "price": 3.10, "start_date": "2026-08-01", "end_date": "2026-08-31", "counterparty": "Shell", "currency": "USD"},
        ]
        target = [
            {"_source_deal_id": "1", "quantity": 10000, "price": 2.85, "start_date": "2026-07-01", "end_date": "2026-07-31", "external_bunit": "BP", "currency": "USD"},
            {"_source_deal_id": "2", "quantity": 5000, "price": 3.10, "start_date": "2026-08-01", "end_date": "2026-08-31", "external_bunit": "Shell", "currency": "USD"},
        ]

        report = engine.reconcile(source, target, "2026-06-05", "NaturalGas")

        assert report.is_clean is True
        assert report.confidence_score == 1.0
        assert report.deals_matched == 2
        assert report.deals_diverged == 0

    def test_missing_in_target(self):
        """Deal in source but not target detected."""
        engine = ReconciliationEngine()

        source = [{"deal_id": "1", "quantity": 10000, "price": 2.85}]
        target = []

        report = engine.reconcile(source, target, "2026-06-05")

        assert report.is_clean is False
        assert report.deals_diverged == 1
        assert report.divergences[0].divergence_type == DivergenceType.MISSING_IN_TARGET

    def test_quantity_divergence(self):
        """Quantity mismatch beyond tolerance detected."""
        engine = ReconciliationEngine()

        source = [{"deal_id": "1", "quantity": 10000, "price": 2.85}]
        target = [{"_source_deal_id": "1", "quantity": 9500, "price": 2.85}]  # 500 off

        report = engine.reconcile(source, target, "2026-06-05")

        assert report.is_clean is False
        assert any(d.field_name == "quantity" for d in report.divergences)

    def test_price_within_tolerance(self):
        """Price difference within tolerance is not flagged."""
        engine = ReconciliationEngine()

        source = [{"deal_id": "1", "quantity": 10000, "price": 2.850}]
        target = [{"_source_deal_id": "1", "quantity": 10000, "price": 2.8505}]  # Within 0.001

        report = engine.reconcile(source, target, "2026-06-05")
        assert report.is_clean is True

    def test_consecutive_clean_days(self):
        """Tracks consecutive clean reports for cutover readiness."""
        engine = ReconciliationEngine()

        source = [{"deal_id": "1", "quantity": 100, "price": 2.0}]
        target = [{"_source_deal_id": "1", "quantity": 100, "price": 2.0}]

        # 3 clean days
        engine.reconcile(source, target, "2026-06-01")
        engine.reconcile(source, target, "2026-06-02")
        engine.reconcile(source, target, "2026-06-03")

        assert engine.consecutive_clean_days == 3
        assert engine.is_ready_for_cutover(required_clean_days=3) is True
        assert engine.is_ready_for_cutover(required_clean_days=5) is False

    def test_divergence_resets_clean_count(self):
        """A divergence resets the consecutive clean day counter."""
        engine = ReconciliationEngine()

        clean_source = [{"deal_id": "1", "quantity": 100, "price": 2.0}]
        clean_target = [{"_source_deal_id": "1", "quantity": 100, "price": 2.0}]
        dirty_target = [{"_source_deal_id": "1", "quantity": 999, "price": 2.0}]

        engine.reconcile(clean_source, clean_target, "2026-06-01")
        engine.reconcile(clean_source, clean_target, "2026-06-02")
        engine.reconcile(clean_source, dirty_target, "2026-06-03")  # Divergence!
        engine.reconcile(clean_source, clean_target, "2026-06-04")

        assert engine.consecutive_clean_days == 1  # Only day 4 is clean after reset

    def test_external_reference_prefix_stripped(self):
        """RA- prefix on external_reference is stripped for matching."""
        engine = ReconciliationEngine()

        source = [{"deal_id": "1001", "quantity": 100, "price": 5.0}]
        target = [{"external_reference": "RA-1001", "quantity": 100, "price": 5.0}]

        report = engine.reconcile(source, target, "2026-06-05")
        assert report.deals_matched == 1
        assert report.is_clean is True
