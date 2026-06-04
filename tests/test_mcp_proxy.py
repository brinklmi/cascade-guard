"""Integration tests for MCP Proxy Server.

Verifies end-to-end processing pipeline:
1. tools/list returns aggregated + meta-tools
2. tools/call full pipeline (auth → envelope → velocity → engine → forward)
3. Blocked invocations return structured errors
4. Meta-tool handling (status, budget, decision_log, configure, schema_versions)
5. Graceful shutdown (drain rejects new requests)
6. Engine integration (cycle detection, budget enforcement)
7. Metrics and decision log populated correctly
"""

import pytest

from cascade_guard.engine import CascadeEngine
from cascade_guard.models import FlowState

from cascade_guard.mcp_proxy.auth import AgentIdentity, AgentPolicy, AgentRole, AuthManager, AuthMode
from cascade_guard.mcp_proxy.config import ConfigManager
from cascade_guard.mcp_proxy.decision_log import DecisionLog
from cascade_guard.mcp_proxy.envelope import EnvelopeParser
from cascade_guard.mcp_proxy.metrics import MetricsEmitter
from cascade_guard.mcp_proxy.registry import ToolRegistry
from cascade_guard.mcp_proxy.server import MCPProxyServer, MCPRequest, MCPResponse
from cascade_guard.mcp_proxy.shutdown import ShutdownManager
from cascade_guard.mcp_proxy.velocity import AgentVelocityController


@pytest.fixture
def mock_server():
    """Create a fully-wired MCPProxyServer with mock target server."""
    engine = CascadeEngine(
        max_velocity=50.0,
        depth_limit=10,
        fanout_limit=20,
        token_budget=100000,
    )

    policies = {
        "admin-agent": AgentPolicy(
            agent_id="admin-agent",
            role=AgentRole.ADMIN,
            allowed_namespaces=["*"],
        ),
        "reader-agent": AgentPolicy(
            agent_id="reader-agent",
            role=AgentRole.READER,
            allowed_namespaces=["datadog/*"],
        ),
        "restricted-agent": AgentPolicy(
            agent_id="restricted-agent",
            role=AgentRole.READER,
            allowed_namespaces=["splunk/*"],
        ),
    }

    auth_manager = AuthManager(auth_mode=AuthMode.NONE, policies=policies)
    envelope_parser = EnvelopeParser()
    velocity_controller = AgentVelocityController(
        window_seconds=60.0,
        global_max_velocity=50.0,
    )
    decision_log = DecisionLog()
    metrics = MetricsEmitter()
    config_manager = ConfigManager()
    config_manager.load()
    shutdown_manager = ShutdownManager(drain_timeout=5.0, persist_state=False)

    # Register a mock target server
    tool_registry = ToolRegistry()
    tool_registry.register_server(
        "datadog",
        [
            {"name": "list_monitors", "description": "List Datadog monitors", "inputSchema": {}},
            {"name": "get_events", "description": "Get Datadog events", "inputSchema": {}},
        ],
        invoke_handler=lambda name, args: {"status": "ok", "tool": name, "args": args},
    )
    tool_registry.register_server(
        "splunk",
        [{"name": "search", "description": "Run Splunk search", "inputSchema": {}}],
        invoke_handler=lambda name, args: {"results": [], "tool": name},
    )

    server = MCPProxyServer(
        engine=engine,
        auth_manager=auth_manager,
        envelope_parser=envelope_parser,
        velocity_controller=velocity_controller,
        tool_registry=tool_registry,
        decision_log=decision_log,
        metrics=metrics,
        config_manager=config_manager,
        shutdown_manager=shutdown_manager,
    )

    return server, engine, decision_log, metrics, shutdown_manager


def _make_identity(agent_id: str, role: AgentRole = AgentRole.READER) -> AgentIdentity:
    """Create a test AgentIdentity."""
    return AgentIdentity(agent_id=agent_id, role=role, auth_mode=AuthMode.NONE)


class TestToolsList:
    """tools/list endpoint."""

    def test_list_returns_available_tools(self, mock_server):
        """tools/list returns tools from connected servers."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(method="tools/list", id=1)
        response = server.handle_request(request, identity)

        assert response.error is None
        tool_names = [t["name"] for t in response.result["tools"]]
        assert "datadog/list_monitors" in tool_names
        assert "datadog/get_events" in tool_names
        assert "splunk/search" in tool_names

    def test_list_includes_meta_tools(self, mock_server):
        """tools/list includes CascadeGuard meta-tools."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(method="tools/list", id=1)
        response = server.handle_request(request, identity)

        tool_names = [t["name"] for t in response.result["tools"]]
        assert "cascadeguard/status" in tool_names
        assert "cascadeguard/configure" in tool_names

    def test_list_respects_namespace_authorization(self, mock_server):
        """tools/list only shows authorized tools."""
        server, *_ = mock_server
        # restricted-agent only has splunk/* access
        identity = _make_identity("restricted-agent", AgentRole.READER)

        request = MCPRequest(method="tools/list", id=1)
        response = server.handle_request(request, identity)

        tool_names = [t["name"] for t in response.result["tools"]]
        assert "splunk/search" in tool_names
        assert "datadog/list_monitors" not in tool_names


class TestToolsCall:
    """tools/call endpoint — full pipeline."""

    def test_allowed_invocation_forwards_to_target(self, mock_server):
        """Allowed tool call is forwarded to target server."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(
            method="tools/call",
            params={
                "name": "datadog/list_monitors",
                "arguments": {"filter": "env:prod"},
                "_cascadeguard": {
                    "schema_version": "1.0",
                    "agent_id": "admin-agent",
                    "caller_id": "root",
                    "token_budget": 50000,
                    "execution_seconds": 30,
                },
            },
            id=2,
        )
        response = server.handle_request(request, identity)

        assert response.error is None
        assert response.result["status"] == "ok"
        assert response.result["tool"] == "list_monitors"
        assert response.result["args"] == {"filter": "env:prod"}

    def test_unauthorized_tool_blocked(self, mock_server):
        """Tool call outside agent's namespace is blocked."""
        server, *_ = mock_server
        # restricted-agent only has splunk/* access
        identity = _make_identity("restricted-agent", AgentRole.READER)

        request = MCPRequest(
            method="tools/call",
            params={
                "name": "datadog/list_monitors",
                "arguments": {},
                "_cascadeguard": {
                    "schema_version": "1.0",
                    "agent_id": "restricted-agent",
                    "caller_id": "root",
                    "token_budget": 50000,
                    "execution_seconds": 30,
                },
            },
            id=3,
        )
        response = server.handle_request(request, identity)

        assert response.error is not None
        assert "unauthorized" in response.error["data"]["reason"]

    def test_unsupported_schema_rejected(self, mock_server):
        """Tool call with unsupported schema version is rejected."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(
            method="tools/call",
            params={
                "name": "datadog/list_monitors",
                "arguments": {},
                "_cascadeguard": {
                    "schema_version": "99.0",
                    "agent_id": "admin-agent",
                    "caller_id": "root",
                    "token_budget": 50000,
                    "execution_seconds": 30,
                },
            },
            id=4,
        )
        response = server.handle_request(request, identity)

        assert response.error is not None
        assert "envelope_parse_error" in response.error["data"]["reason"]

    def test_missing_envelope_uses_defaults(self, mock_server):
        """Tool call without envelope metadata still works (backward compat)."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(
            method="tools/call",
            params={
                "name": "datadog/list_monitors",
                "arguments": {"x": 1},
            },
            id=5,
        )
        response = server.handle_request(request, identity)

        assert response.error is None
        assert response.result["status"] == "ok"


class TestMetaTools:
    """CascadeGuard meta-tool handling."""

    def test_status_meta_tool(self, mock_server):
        """cascadeguard/status returns engine status."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(
            method="tools/call",
            params={"name": "cascadeguard/status", "arguments": {}},
            id=10,
        )
        response = server.handle_request(request, identity)

        assert response.error is None
        assert "flow_state" in response.result
        assert "total_agents" in response.result

    def test_schema_versions_meta_tool(self, mock_server):
        """cascadeguard/schema_versions returns supported versions."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(
            method="tools/call",
            params={"name": "cascadeguard/schema_versions", "arguments": {}},
            id=11,
        )
        response = server.handle_request(request, identity)

        assert response.error is None
        assert "1.0" in response.result["versions"]

    def test_configure_requires_admin(self, mock_server):
        """cascadeguard/configure blocked for non-admin agents."""
        server, *_ = mock_server
        identity = _make_identity("reader-agent", AgentRole.READER)

        request = MCPRequest(
            method="tools/call",
            params={
                "name": "cascadeguard/configure",
                "arguments": {"max_velocity": 100.0},
            },
            id=12,
        )
        response = server.handle_request(request, identity)

        assert response.error is not None
        assert "admin" in response.error["message"]

    def test_decision_log_meta_tool(self, mock_server):
        """cascadeguard/decision_log returns recent entries."""
        server, engine, decision_log, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        # Make a call first to populate log
        call_request = MCPRequest(
            method="tools/call",
            params={"name": "datadog/list_monitors", "arguments": {}},
            id=20,
        )
        server.handle_request(call_request, identity)

        # Now query log
        request = MCPRequest(
            method="tools/call",
            params={"name": "cascadeguard/decision_log", "arguments": {"n": 10}},
            id=21,
        )
        response = server.handle_request(request, identity)

        assert response.error is None
        assert len(response.result) >= 1
        assert response.result[0]["tool_name"] == "datadog/list_monitors"


class TestGracefulShutdown:
    """Shutdown and drain behavior."""

    def test_drain_rejects_new_requests(self, mock_server):
        """During drain, new requests are rejected."""
        server, _, _, _, shutdown_mgr = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        shutdown_mgr.start_drain()

        request = MCPRequest(method="tools/list", id=100)
        response = server.handle_request(request, identity)

        assert response.error is not None
        assert "service_shutting_down" in response.error["message"]


class TestDecisionLogPopulation:
    """Verify decisions are logged correctly."""

    def test_allowed_call_logged(self, mock_server):
        """Allowed invocations are logged."""
        server, _, decision_log, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(
            method="tools/call",
            params={"name": "datadog/list_monitors", "arguments": {}},
            id=30,
        )
        server.handle_request(request, identity)

        entries = decision_log.query(1)
        assert len(entries) == 1
        assert entries[0].verdict == "allowed"
        assert entries[0].agent_id == "admin-agent"
        assert entries[0].tool_name == "datadog/list_monitors"

    def test_blocked_call_logged(self, mock_server):
        """Blocked invocations are logged."""
        server, _, decision_log, *_ = mock_server
        identity = _make_identity("restricted-agent", AgentRole.READER)

        request = MCPRequest(
            method="tools/call",
            params={"name": "datadog/list_monitors", "arguments": {}},
            id=31,
        )
        server.handle_request(request, identity)

        entries = decision_log.query(1)
        assert len(entries) == 1
        assert entries[0].verdict == "blocked"


class TestMetricsPopulation:
    """Verify metrics are emitted correctly."""

    def test_invocation_counted(self, mock_server):
        """Invocations increment metrics counter."""
        server, _, _, metrics, _ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        assert metrics.total_invocations == 0

        request = MCPRequest(
            method="tools/call",
            params={"name": "datadog/list_monitors", "arguments": {}},
            id=40,
        )
        server.handle_request(request, identity)

        assert metrics.total_invocations == 1

    def test_blocked_counted(self, mock_server):
        """Blocked invocations increment blocked counter."""
        server, _, _, metrics, _ = mock_server
        identity = _make_identity("restricted-agent", AgentRole.READER)

        request = MCPRequest(
            method="tools/call",
            params={"name": "datadog/list_monitors", "arguments": {}},
            id=41,
        )
        server.handle_request(request, identity)

        assert metrics.total_blocked == 1


class TestUnknownMethod:
    """Unknown JSON-RPC methods."""

    def test_unknown_method_returns_error(self, mock_server):
        """Unknown methods return method not found error."""
        server, *_ = mock_server
        identity = _make_identity("admin-agent", AgentRole.ADMIN)

        request = MCPRequest(method="unknown/method", id=99)
        response = server.handle_request(request, identity)

        assert response.error is not None
        assert response.error["code"] == -32601
