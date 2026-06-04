"""Tests for Tool Registry & Target Server Management.

Verifies:
1. Multi-server tool registration with namespace prefixing
2. Tool listing (available only from connected servers)
3. Invocation routing to correct server
4. Server failure handling and reconnection backoff
5. Hot-add of new servers without restart
6. Deregistration removes all server tools
7. Exponential backoff mechanics
"""

import time

import pytest

from cascade_guard.mcp_proxy.registry import (
    ExponentialBackoff,
    InvocationResult,
    ServerHealth,
    ServerStatus,
    ToolDefinition,
    ToolRegistry,
)


class TestToolRegistration:
    """Multi-server tool registration with namespacing."""

    def test_register_server_with_tools(self):
        """Registering a server adds namespaced tools."""
        registry = ToolRegistry()
        tools = [
            {"name": "list_monitors", "description": "List monitors"},
            {"name": "get_events", "description": "Get events"},
        ]

        count = registry.register_server("datadog", tools)
        assert count == 2

        all_tools = registry.list_tools()
        names = [t.name for t in all_tools]
        assert "datadog/list_monitors" in names
        assert "datadog/get_events" in names

    def test_namespace_prevents_collisions(self):
        """Same tool name on different servers don't collide."""
        registry = ToolRegistry()
        registry.register_server("server-a", [{"name": "search", "description": "A"}])
        registry.register_server("server-b", [{"name": "search", "description": "B"}])

        all_tools = registry.list_tools()
        names = [t.name for t in all_tools]
        assert "server-a/search" in names
        assert "server-b/search" in names
        assert len(all_tools) == 2

    def test_tool_definition_preserves_details(self):
        """Tool definition stores server, original name, and schema."""
        registry = ToolRegistry()
        registry.register_server("splunk", [
            {
                "name": "run_query",
                "description": "Run a Splunk query",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ])

        tool = registry.get_tool("splunk/run_query")
        assert tool is not None
        assert tool.server_name == "splunk"
        assert tool.original_name == "run_query"
        assert tool.description == "Run a Splunk query"
        assert "properties" in tool.input_schema

    def test_empty_name_tools_skipped(self):
        """Tools with empty names are skipped."""
        registry = ToolRegistry()
        count = registry.register_server("srv", [
            {"name": "valid", "description": "ok"},
            {"name": "", "description": "skip me"},
        ])
        assert count == 1

    def test_re_register_replaces_tools(self):
        """Re-registering a server replaces its tools."""
        registry = ToolRegistry()
        registry.register_server("datadog", [
            {"name": "old_tool", "description": "old"},
        ])
        registry.register_server("datadog", [
            {"name": "new_tool", "description": "new"},
        ])

        all_tools = registry.list_all_tools()
        names = [t.name for t in all_tools]
        assert "datadog/new_tool" in names
        assert "datadog/old_tool" not in names


class TestToolListing:
    """Tool listing respects server availability."""

    def test_only_connected_servers_in_list(self):
        """list_tools only shows tools from connected servers."""
        registry = ToolRegistry()
        registry.register_server("healthy", [{"name": "t1", "description": ""}])
        registry.register_server("down", [{"name": "t2", "description": ""}])

        registry.mark_unavailable("down")

        available = registry.list_tools()
        names = [t.name for t in available]
        assert "healthy/t1" in names
        assert "down/t2" not in names

    def test_list_all_tools_ignores_status(self):
        """list_all_tools includes all regardless of status."""
        registry = ToolRegistry()
        registry.register_server("healthy", [{"name": "t1", "description": ""}])
        registry.register_server("down", [{"name": "t2", "description": ""}])
        registry.mark_unavailable("down")

        all_tools = registry.list_all_tools()
        assert len(all_tools) == 2

    def test_get_tool_returns_none_for_unknown(self):
        """get_tool returns None for unregistered tools."""
        registry = ToolRegistry()
        assert registry.get_tool("unknown/tool") is None


class TestInvocationRouting:
    """Tool invocation routes to correct server."""

    def test_invoke_routes_to_handler(self):
        """invoke_tool calls the correct server's handler."""
        results = []

        def mock_handler(tool_name, args):
            results.append((tool_name, args))
            return {"status": "ok"}

        registry = ToolRegistry()
        registry.register_server(
            "datadog",
            [{"name": "list_monitors", "description": ""}],
            invoke_handler=mock_handler,
        )

        result = registry.invoke_tool("datadog/list_monitors", {"filter": "env:prod"})

        assert result.success is True
        assert result.result == {"status": "ok"}
        assert result.server_name == "datadog"
        assert result.latency_ms > 0
        assert results == [("list_monitors", {"filter": "env:prod"})]

    def test_invoke_unknown_tool_fails(self):
        """Invoking unknown tool returns error."""
        registry = ToolRegistry()
        result = registry.invoke_tool("ghost/tool", {})

        assert result.success is False
        assert "not found" in result.error

    def test_invoke_unavailable_server_fails(self):
        """Invoking tool on unavailable server returns error."""
        registry = ToolRegistry()
        registry.register_server(
            "down-server",
            [{"name": "tool", "description": ""}],
            invoke_handler=lambda n, a: None,
        )
        registry.mark_unavailable("down-server")

        result = registry.invoke_tool("down-server/tool", {})
        assert result.success is False
        assert "unavailable" in result.error

    def test_invoke_no_handler_fails(self):
        """Invoking without a handler returns error."""
        registry = ToolRegistry()
        registry.register_server("no-handler", [{"name": "t", "description": ""}])

        result = registry.invoke_tool("no-handler/t", {})
        assert result.success is False
        assert "No invoke handler" in result.error


class TestServerFailureAndReconnection:
    """Server failure handling with exponential backoff."""

    def test_exception_records_failure(self):
        """Handler exception transitions server to reconnecting."""
        def failing_handler(name, args):
            raise ConnectionError("Connection refused")

        registry = ToolRegistry()
        registry.register_server(
            "flaky",
            [{"name": "tool", "description": ""}],
            invoke_handler=failing_handler,
        )

        result = registry.invoke_tool("flaky/tool", {})
        assert result.success is False
        assert "Connection refused" in result.error

        health = registry.get_server_health("flaky")
        assert health.status == ServerStatus.RECONNECTING
        assert health.consecutive_failures == 1

    def test_timeout_records_failure(self):
        """Timeout exception records failure."""
        def timeout_handler(name, args):
            raise TimeoutError("Timed out")

        registry = ToolRegistry()
        registry.register_server(
            "slow",
            [{"name": "tool", "description": ""}],
            invoke_handler=timeout_handler,
        )

        result = registry.invoke_tool("slow/tool", {})
        assert result.success is False
        assert "Timeout" in result.error

    def test_success_resets_failure_count(self):
        """Successful invocation after failure resets counters."""
        call_count = [0]

        def sometimes_fails(name, args):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("fail")
            return "ok"

        registry = ToolRegistry()
        registry.register_server(
            "flaky",
            [{"name": "tool", "description": ""}],
            invoke_handler=sometimes_fails,
        )

        # First call fails
        registry.invoke_tool("flaky/tool", {})
        health = registry.get_server_health("flaky")
        assert health.consecutive_failures == 1

        # Manually allow retry (reset backoff timer)
        health.backoff.next_retry_at = 0

        # Second call succeeds
        result = registry.invoke_tool("flaky/tool", {})
        assert result.success is True
        health = registry.get_server_health("flaky")
        assert health.consecutive_failures == 0
        assert health.status == ServerStatus.CONNECTED

    def test_mark_connected_recovers_server(self):
        """mark_connected restores server to healthy state."""
        registry = ToolRegistry()
        registry.register_server("srv", [{"name": "t", "description": ""}])
        registry.mark_unavailable("srv")

        assert registry.get_server_health("srv").status == ServerStatus.UNAVAILABLE

        registry.mark_connected("srv")
        assert registry.get_server_health("srv").status == ServerStatus.CONNECTED


class TestHotAddAndRemove:
    """Hot-add and deregistration of servers."""

    def test_hot_add_new_server(self):
        """Adding a new server doesn't affect existing ones."""
        registry = ToolRegistry()
        registry.register_server("existing", [{"name": "t1", "description": ""}])

        # Hot-add
        registry.register_server("new", [{"name": "t2", "description": ""}])

        all_tools = registry.list_tools()
        names = [t.name for t in all_tools]
        assert "existing/t1" in names
        assert "new/t2" in names

    def test_deregister_removes_all_tools(self):
        """Deregistering a server removes all its tools."""
        registry = ToolRegistry()
        registry.register_server("doomed", [
            {"name": "t1", "description": ""},
            {"name": "t2", "description": ""},
        ])
        registry.register_server("safe", [{"name": "t3", "description": ""}])

        registry.deregister_server("doomed")

        all_tools = registry.list_all_tools()
        names = [t.name for t in all_tools]
        assert "doomed/t1" not in names
        assert "doomed/t2" not in names
        assert "safe/t3" in names

    def test_deregister_removes_health(self):
        """Deregistering removes health tracking."""
        registry = ToolRegistry()
        registry.register_server("srv", [{"name": "t", "description": ""}])
        registry.deregister_server("srv")

        assert registry.get_server_health("srv") is None

    def test_server_names_list(self):
        """server_names returns all registered servers."""
        registry = ToolRegistry()
        registry.register_server("a", [{"name": "t", "description": ""}])
        registry.register_server("b", [{"name": "t", "description": ""}])

        names = registry.server_names()
        assert "a" in names
        assert "b" in names


class TestExponentialBackoff:
    """Exponential backoff mechanics."""

    def test_initial_delay(self):
        """First failure gives initial delay."""
        backoff = ExponentialBackoff(initial_delay=1.0, max_delay=60.0)
        delay = backoff.record_failure()
        assert delay == 1.0
        assert backoff.current_attempt == 1

    def test_exponential_growth(self):
        """Delay doubles on each failure."""
        backoff = ExponentialBackoff(initial_delay=1.0, multiplier=2.0)
        backoff.record_failure()  # 1s
        delay = backoff.record_failure()  # 2s
        assert delay == 2.0
        delay = backoff.record_failure()  # 4s
        assert delay == 4.0

    def test_max_delay_cap(self):
        """Delay never exceeds max_delay."""
        backoff = ExponentialBackoff(initial_delay=1.0, max_delay=10.0, multiplier=2.0)
        for _ in range(20):
            delay = backoff.record_failure()
        assert delay <= 10.0

    def test_success_resets(self):
        """Success resets attempt counter."""
        backoff = ExponentialBackoff()
        backoff.record_failure()
        backoff.record_failure()
        assert backoff.current_attempt == 2

        backoff.record_success()
        assert backoff.current_attempt == 0

    def test_can_retry_after_delay(self):
        """can_retry is False during backoff, True after."""
        backoff = ExponentialBackoff(initial_delay=0.05)
        backoff.record_failure()

        # Immediately after failure — can't retry yet
        assert backoff.can_retry() is False

        # Wait for delay to pass
        time.sleep(0.06)
        assert backoff.can_retry() is True

    def test_can_retry_initially_true(self):
        """Can retry before any failures."""
        backoff = ExponentialBackoff()
        assert backoff.can_retry() is True


class TestServerHealthStatus:
    """Server health status reporting."""

    def test_get_all_server_health(self):
        """get_all_server_health returns status for all servers."""
        registry = ToolRegistry()
        registry.register_server("a", [{"name": "t", "description": ""}])
        registry.register_server("b", [{"name": "t", "description": ""}])

        health = registry.get_all_server_health()
        assert "a" in health
        assert "b" in health
        assert health["a"]["status"] == "connected"
        assert health["a"]["tools_count"] == 1

    def test_health_to_dict(self):
        """ServerHealth.to_dict() has expected fields."""
        health = ServerHealth(server_name="test", status=ServerStatus.CONNECTED)
        d = health.to_dict()
        assert d["server_name"] == "test"
        assert d["status"] == "connected"
        assert "consecutive_failures" in d
        assert "current_backoff_delay" in d
