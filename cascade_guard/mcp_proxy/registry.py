"""Tool Registry & Target Server Management.

Discovers, aggregates, and namespaces tools from registered target servers.
Handles reconnection with exponential backoff and hot-add of new servers.

Tool namespace format: {server_name}/{tool_name}
This prevents collisions when multiple servers expose same-named tools.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ServerStatus(str, Enum):
    """Connection status for a target server."""

    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    UNAVAILABLE = "unavailable"
    DISCONNECTED = "disconnected"


@dataclass
class ToolDefinition:
    """An MCP tool definition aggregated from a target server.

    Attributes:
        name: Fully-qualified name ({server}/{tool}).
        server_name: Origin server name.
        original_name: Tool name on the target server.
        description: Tool description.
        input_schema: JSON Schema for tool arguments.
    """

    name: str  # Namespaced: "server/tool"
    server_name: str
    original_name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExponentialBackoff:
    """Exponential backoff state for reconnection.

    Attributes:
        initial_delay: Starting delay in seconds (default 1.0).
        max_delay: Maximum delay in seconds (default 60.0).
        multiplier: Growth factor per attempt (default 2.0).
        current_attempt: Number of consecutive failed attempts.
        next_retry_at: Unix timestamp for next allowed retry.
    """

    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    current_attempt: int = 0
    next_retry_at: float = 0.0

    def record_failure(self) -> float:
        """Record a connection failure and compute next retry delay.

        Returns:
            The delay in seconds before next retry.
        """
        delay = min(
            self.initial_delay * (self.multiplier ** self.current_attempt),
            self.max_delay,
        )
        self.current_attempt += 1
        self.next_retry_at = time.time() + delay
        return delay

    def record_success(self) -> None:
        """Record a successful connection, resetting backoff."""
        self.current_attempt = 0
        self.next_retry_at = 0.0

    def can_retry(self) -> bool:
        """Check if enough time has passed for a retry.

        Returns:
            True if current time >= next_retry_at.
        """
        return time.time() >= self.next_retry_at

    @property
    def current_delay(self) -> float:
        """Current delay in seconds (without recording)."""
        if self.current_attempt == 0:
            return 0.0
        return min(
            self.initial_delay * (self.multiplier ** (self.current_attempt - 1)),
            self.max_delay,
        )


@dataclass
class ServerHealth:
    """Health status for a target server.

    Attributes:
        server_name: Name of the target server.
        status: Current connection status.
        last_success: Unix timestamp of last successful communication.
        last_failure: Unix timestamp of last failure (0 if none).
        consecutive_failures: Number of failures since last success.
        backoff: Current reconnection backoff state.
        tools_count: Number of tools registered from this server.
    """

    server_name: str
    status: ServerStatus = ServerStatus.DISCONNECTED
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    backoff: ExponentialBackoff = field(default_factory=ExponentialBackoff)
    tools_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for monitoring."""
        return {
            "server_name": self.server_name,
            "status": self.status.value,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "consecutive_failures": self.consecutive_failures,
            "current_backoff_delay": self.backoff.current_delay,
            "tools_count": self.tools_count,
        }


@dataclass
class InvocationResult:
    """Result of a tool invocation on a target server.

    Attributes:
        success: Whether the invocation succeeded.
        result: The tool response data (if success).
        error: Error message (if failure).
        server_name: Which server handled the invocation.
        latency_ms: Time taken for the invocation in milliseconds.
    """

    success: bool
    result: Any = None
    error: str = ""
    server_name: str = ""
    latency_ms: float = 0.0


class ToolRegistry:
    """Registry of tools aggregated from target servers.

    Discovers tools from connected servers, namespaces them to prevent
    collisions, and routes invocations to the correct server.

    Supports:
    - Multi-server tool aggregation with namespace prefixing
    - Hot-add of new servers without restart
    - Exponential backoff reconnection on failures
    - Availability tracking per server

    Thread-safety: Not thread-safe. Use in asyncio context (single-threaded).
    """

    def __init__(
        self,
        timeout_seconds: float = 30.0,
    ):
        """Initialize the tool registry.

        Args:
            timeout_seconds: Default timeout for target server invocations.
        """
        self._timeout = timeout_seconds
        self._tools: Dict[str, ToolDefinition] = {}  # namespaced_name → definition
        self._server_health: Dict[str, ServerHealth] = {}  # server_name → health
        self._server_tools: Dict[str, List[str]] = {}  # server_name → [namespaced_names]
        self._invoke_handlers: Dict[str, Callable] = {}  # server_name → invoke function

    @property
    def timeout_seconds(self) -> float:
        """Default invocation timeout."""
        return self._timeout

    def register_server(
        self,
        server_name: str,
        tools: List[Dict[str, Any]],
        invoke_handler: Optional[Callable] = None,
    ) -> int:
        """Register a target server and its tools.

        Namespaces all tools as {server_name}/{tool_name}.
        If a server with the same name already exists, its tools are replaced.

        Args:
            server_name: Unique name for the target server.
            tools: List of tool definitions from the server.
                   Each dict should have: name, description, inputSchema.
            invoke_handler: Optional callable for invoking tools on this server.
                            Signature: (tool_name: str, arguments: dict) -> Any

        Returns:
            Number of tools registered.
        """
        # Remove old tools for this server if re-registering
        self._remove_server_tools(server_name)

        # Register new tools with namespace prefix
        namespaced_names = []
        for tool_def in tools:
            original_name = tool_def.get("name", "")
            if not original_name:
                continue

            namespaced = f"{server_name}/{original_name}"
            self._tools[namespaced] = ToolDefinition(
                name=namespaced,
                server_name=server_name,
                original_name=original_name,
                description=tool_def.get("description", ""),
                input_schema=tool_def.get("inputSchema", {}),
            )
            namespaced_names.append(namespaced)

        self._server_tools[server_name] = namespaced_names

        # Set up health tracking
        self._server_health[server_name] = ServerHealth(
            server_name=server_name,
            status=ServerStatus.CONNECTED,
            last_success=time.time(),
            tools_count=len(namespaced_names),
        )

        # Store invoke handler
        if invoke_handler:
            self._invoke_handlers[server_name] = invoke_handler

        return len(namespaced_names)

    def deregister_server(self, server_name: str) -> None:
        """Remove a target server and all its tools.

        Args:
            server_name: The server to remove.
        """
        self._remove_server_tools(server_name)
        self._server_health.pop(server_name, None)
        self._server_tools.pop(server_name, None)
        self._invoke_handlers.pop(server_name, None)

    def list_tools(self) -> List[ToolDefinition]:
        """List all available tools across all servers.

        Only includes tools from servers in CONNECTED status.

        Returns:
            List of ToolDefinition for all available tools.
        """
        available = []
        for name, tool_def in self._tools.items():
            health = self._server_health.get(tool_def.server_name)
            if health and health.status == ServerStatus.CONNECTED:
                available.append(tool_def)
        return available

    def list_all_tools(self) -> List[ToolDefinition]:
        """List all registered tools regardless of server status.

        Returns:
            List of all ToolDefinition objects.
        """
        return list(self._tools.values())

    def get_tool(self, namespaced_name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by its namespaced name.

        Args:
            namespaced_name: Fully-qualified tool name (server/tool).

        Returns:
            ToolDefinition if found, None otherwise.
        """
        return self._tools.get(namespaced_name)

    def invoke_tool(
        self,
        namespaced_name: str,
        arguments: Dict[str, Any],
    ) -> InvocationResult:
        """Invoke a tool on its target server.

        Routes to the correct server based on the namespace prefix.
        Returns error if server is unavailable or tool not found.

        Args:
            namespaced_name: Fully-qualified tool name.
            arguments: Tool arguments (passed unmodified to target).

        Returns:
            InvocationResult with success/error and response data.
        """
        tool_def = self._tools.get(namespaced_name)
        if tool_def is None:
            return InvocationResult(
                success=False,
                error=f"Tool '{namespaced_name}' not found in registry",
            )

        server_name = tool_def.server_name
        health = self._server_health.get(server_name)

        # Check server availability
        if health and health.status == ServerStatus.UNAVAILABLE:
            return InvocationResult(
                success=False,
                error=f"Target server '{server_name}' is unavailable. "
                      f"Retry after {health.backoff.current_delay:.1f}s backoff.",
                server_name=server_name,
            )

        if health and health.status == ServerStatus.RECONNECTING:
            if not health.backoff.can_retry():
                return InvocationResult(
                    success=False,
                    error=f"Target server '{server_name}' is reconnecting. "
                          f"Next retry in {health.backoff.next_retry_at - time.time():.1f}s.",
                    server_name=server_name,
                )

        # Get invoke handler
        handler = self._invoke_handlers.get(server_name)
        if handler is None:
            return InvocationResult(
                success=False,
                error=f"No invoke handler registered for server '{server_name}'",
                server_name=server_name,
            )

        # Invoke the tool
        start = time.perf_counter_ns()
        try:
            result = handler(tool_def.original_name, arguments)
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000

            # Record success
            if health:
                health.status = ServerStatus.CONNECTED
                health.last_success = time.time()
                health.consecutive_failures = 0
                health.backoff.record_success()

            return InvocationResult(
                success=True,
                result=result,
                server_name=server_name,
                latency_ms=latency_ms,
            )

        except TimeoutError:
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000
            self._record_server_failure(server_name)
            return InvocationResult(
                success=False,
                error=f"Timeout invoking '{namespaced_name}' on server '{server_name}' "
                      f"after {self._timeout}s",
                server_name=server_name,
                latency_ms=latency_ms,
            )

        except Exception as e:
            latency_ms = (time.perf_counter_ns() - start) / 1_000_000
            self._record_server_failure(server_name)
            return InvocationResult(
                success=False,
                error=f"Error invoking '{namespaced_name}': {str(e)}",
                server_name=server_name,
                latency_ms=latency_ms,
            )

    def mark_unavailable(self, server_name: str) -> None:
        """Mark a server as unavailable.

        Its tools will return errors until the server recovers.

        Args:
            server_name: The server to mark unavailable.
        """
        health = self._server_health.get(server_name)
        if health:
            health.status = ServerStatus.UNAVAILABLE

    def mark_connected(self, server_name: str) -> None:
        """Mark a server as connected (recovered).

        Args:
            server_name: The server that recovered.
        """
        health = self._server_health.get(server_name)
        if health:
            health.status = ServerStatus.CONNECTED
            health.last_success = time.time()
            health.consecutive_failures = 0
            health.backoff.record_success()

    def get_server_health(self, server_name: str) -> Optional[ServerHealth]:
        """Get health status for a target server.

        Args:
            server_name: The server to query.

        Returns:
            ServerHealth if server exists, None otherwise.
        """
        return self._server_health.get(server_name)

    def get_all_server_health(self) -> Dict[str, Dict[str, Any]]:
        """Get health status for all servers.

        Returns:
            Dict of server_name → health status dict.
        """
        return {
            name: health.to_dict()
            for name, health in self._server_health.items()
        }

    def server_names(self) -> List[str]:
        """List all registered server names.

        Returns:
            List of server name strings.
        """
        return list(self._server_health.keys())

    def _record_server_failure(self, server_name: str) -> None:
        """Record a failure for a server and update backoff.

        Args:
            server_name: The server that failed.
        """
        health = self._server_health.get(server_name)
        if health is None:
            return

        health.consecutive_failures += 1
        health.last_failure = time.time()
        health.backoff.record_failure()
        health.status = ServerStatus.RECONNECTING

    def _remove_server_tools(self, server_name: str) -> None:
        """Remove all tools belonging to a server.

        Args:
            server_name: The server whose tools to remove.
        """
        if server_name in self._server_tools:
            for tool_name in self._server_tools[server_name]:
                self._tools.pop(tool_name, None)
            del self._server_tools[server_name]
