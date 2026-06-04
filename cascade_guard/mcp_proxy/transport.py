"""Transport layer — stdio + HTTP/SSE with mTLS support.

Manages connection lifecycle, TLS hot-reload, and graceful draining.

Supports two transport modes:
- stdio: For local agent-proxy communication (process-spawned)
- HTTP/SSE: For networked deployment within VPC with mTLS
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .server import MCPRequest, MCPResponse


@dataclass
class Connection:
    """Represents an active MCP client connection.

    Attributes:
        connection_id: Unique identifier for this connection.
        agent_id: Authenticated agent (empty until auth completes).
        transport_type: "stdio" or "sse".
        connected_at: Unix timestamp of connection establishment.
        requests_processed: Number of requests handled on this connection.
    """

    connection_id: str
    agent_id: str = ""
    transport_type: str = "stdio"
    connected_at: float = 0.0
    requests_processed: int = 0


class StdioTransport:
    """MCP transport over stdin/stdout.

    Reads JSON-RPC messages from stdin (line-delimited JSON).
    Writes JSON-RPC responses to stdout.

    Used for local development and process-spawned agent connections.
    """

    def __init__(
        self,
        on_request: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        """Initialize stdio transport.

        Args:
            on_request: Callback to handle incoming requests.
                        Signature: (request_dict) -> response_dict
        """
        self._on_request = on_request
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the transport is actively processing."""
        return self._running

    def parse_message(self, line: str) -> Optional[MCPRequest]:
        """Parse a single line of input as an MCP request.

        Args:
            line: Raw JSON string from stdin.

        Returns:
            MCPRequest if valid, None if parsing fails.
        """
        try:
            data = json.loads(line)
            return MCPRequest(
                method=data.get("method", ""),
                params=data.get("params", {}),
                id=data.get("id"),
            )
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def format_response(self, response: MCPResponse) -> str:
        """Format an MCP response as a JSON string for stdout.

        Args:
            response: The response to serialize.

        Returns:
            JSON string (single line, no trailing newline).
        """
        return json.dumps(response.to_dict(), separators=(",", ":"))

    def send_response(self, response: MCPResponse) -> None:
        """Write a response to stdout.

        Args:
            response: The response to send.
        """
        line = self.format_response(response)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def send_error(self, error_code: int, message: str, request_id: Any = None) -> None:
        """Send an error response to stdout.

        Args:
            error_code: JSON-RPC error code.
            message: Error message.
            request_id: The request ID to echo back.
        """
        response = MCPResponse(
            error={"code": error_code, "message": message},
            id=request_id,
        )
        self.send_response(response)

    def start(self) -> None:
        """Mark transport as running."""
        self._running = True

    def stop(self) -> None:
        """Mark transport as stopped."""
        self._running = False


class TransportManager:
    """Manages transport lifecycle and connection tracking.

    Coordinates between stdio and SSE transports.
    Tracks active connections and handles graceful draining.
    """

    def __init__(self):
        """Initialize transport manager."""
        self._connections: Dict[str, Connection] = {}
        self._stdio: Optional[StdioTransport] = None
        self._is_draining: bool = False
        self._transport_type: str = "stdio"

    @property
    def active_connections(self) -> Dict[str, Connection]:
        """Currently active connections."""
        return dict(self._connections)

    @property
    def active_count(self) -> int:
        """Number of active connections."""
        return len(self._connections)

    @property
    def is_draining(self) -> bool:
        """Whether transport is in drain mode."""
        return self._is_draining

    @property
    def transport_type(self) -> str:
        """Current transport type."""
        return self._transport_type

    def register_connection(self, conn: Connection) -> None:
        """Register a new connection.

        Args:
            conn: The connection to register.
        """
        if self._is_draining:
            return  # Don't accept new connections during drain
        self._connections[conn.connection_id] = conn

    def unregister_connection(self, connection_id: str) -> None:
        """Remove a connection.

        Args:
            connection_id: ID of the connection to remove.
        """
        self._connections.pop(connection_id, None)

    def get_connection(self, connection_id: str) -> Optional[Connection]:
        """Get a connection by ID.

        Args:
            connection_id: The connection to look up.

        Returns:
            Connection if found, None otherwise.
        """
        return self._connections.get(connection_id)

    def start_stdio(
        self,
        on_request: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> StdioTransport:
        """Start stdio transport.

        Args:
            on_request: Request handler callback.

        Returns:
            The started StdioTransport.
        """
        self._transport_type = "stdio"
        self._stdio = StdioTransport(on_request=on_request)
        self._stdio.start()
        return self._stdio

    def start_drain(self) -> None:
        """Enter drain mode — stop accepting new connections.

        Existing connections continue until they complete.
        """
        self._is_draining = True
        if self._stdio:
            self._stdio.stop()

    def stop(self) -> None:
        """Stop all transports."""
        self._is_draining = True
        if self._stdio:
            self._stdio.stop()
        self._connections.clear()
