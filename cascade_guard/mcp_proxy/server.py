"""MCP Server core — handles tools/list, tools/call JSON-RPC methods.

Wires the full processing pipeline:
  auth → envelope parse → velocity check → CascadeEngine → forward/block

This is the main integration point that connects all building blocks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cascade_guard.engine import CascadeEngine
from cascade_guard.models import DelegationAction, DelegationVerdict, FlowState

from .auth import AgentIdentity, AuthManager, AuthMode, AuthzResult
from .config import ConfigManager, ProxyConfig
from .decision_log import DecisionLog
from .envelope import Envelope, EnvelopeParser, ParseResult
from .metrics import MetricsEmitter
from .registry import ToolRegistry
from .shutdown import ShutdownManager
from .velocity import AgentVelocityController, VelocityCheck


@dataclass
class MCPRequest:
    """Parsed MCP JSON-RPC request."""

    method: str
    params: Dict[str, Any] = field(default_factory=dict)
    id: Any = None


@dataclass
class MCPResponse:
    """MCP JSON-RPC response."""

    result: Any = None
    error: Optional[Dict[str, Any]] = None
    id: Any = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-RPC response dict."""
        response: Dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            response["error"] = self.error
        else:
            response["result"] = self.result
        return response


@dataclass
class ToolCallResult:
    """Internal result of processing a tool call through the full pipeline."""

    allowed: bool
    response: Any = None
    error_code: int = 0
    error_message: str = ""
    reason: str = ""
    impedance: float = 0.0
    flow_state: str = "nominal"
    latency_us: int = 0
    tokens_consumed: float = 0.0


class MCPProxyServer:
    """MCP Proxy Server — the main integration component.

    Processes MCP JSON-RPC requests through the full safety pipeline:
    1. Authenticate (mTLS/IAM)
    2. Authorize (namespace check)
    3. Parse envelope (zero semantic overhead)
    4. Check per-agent velocity
    5. Evaluate through CascadeEngine
    6. Log decision
    7. Forward to target server OR return error

    Also exposes CascadeGuard meta-tools for status/config/audit.
    """

    # CascadeGuard meta-tool names
    META_TOOLS = {
        "cascadeguard/status",
        "cascadeguard/budget_status",
        "cascadeguard/decision_log",
        "cascadeguard/configure",
        "cascadeguard/schema_versions",
        "cascadeguard/reload_status",
    }

    def __init__(
        self,
        engine: CascadeEngine,
        auth_manager: AuthManager,
        envelope_parser: EnvelopeParser,
        velocity_controller: AgentVelocityController,
        tool_registry: ToolRegistry,
        decision_log: DecisionLog,
        metrics: MetricsEmitter,
        config_manager: ConfigManager,
        shutdown_manager: ShutdownManager,
    ):
        """Initialize the MCP Proxy Server.

        Args:
            engine: CascadeEngine instance for safety checks.
            auth_manager: Authentication/authorization manager.
            envelope_parser: Zero-overhead envelope parser.
            velocity_controller: Per-agent velocity throttling.
            tool_registry: Target server tool registry.
            decision_log: Audit trail.
            metrics: Metrics emitter.
            config_manager: Configuration manager.
            shutdown_manager: Graceful shutdown manager.
        """
        self._engine = engine
        self._auth = auth_manager
        self._parser = envelope_parser
        self._velocity = velocity_controller
        self._registry = tool_registry
        self._log = decision_log
        self._metrics = metrics
        self._config = config_manager
        self._shutdown = shutdown_manager
        self._registered_agents: set = set()

    def handle_request(
        self,
        request: MCPRequest,
        identity: AgentIdentity,
    ) -> MCPResponse:
        """Handle an MCP JSON-RPC request.

        Routes to the appropriate handler based on method name.

        Args:
            request: Parsed MCP request.
            identity: Authenticated agent identity.

        Returns:
            MCPResponse to send back to the client.
        """
        # Check drain state
        if self._shutdown.is_draining:
            return MCPResponse(
                error={"code": -32000, "message": "service_shutting_down"},
                id=request.id,
            )

        # Track request for shutdown drain
        self._shutdown.track_request_start()

        try:
            if request.method == "tools/list":
                return self._handle_tools_list(request, identity)
            elif request.method == "tools/call":
                return self._handle_tools_call(request, identity)
            else:
                return MCPResponse(
                    error={"code": -32601, "message": f"Method not found: {request.method}"},
                    id=request.id,
                )
        finally:
            self._shutdown.track_request_end()

    def _handle_tools_list(
        self,
        request: MCPRequest,
        identity: AgentIdentity,
    ) -> MCPResponse:
        """Handle tools/list — return aggregated tool definitions.

        Includes both target server tools and CascadeGuard meta-tools.
        """
        # Get available tools from registry
        tools = self._registry.list_tools()

        # Build tool list response
        tool_list = []
        for tool_def in tools:
            # Only include tools the agent is authorized for
            authz = self._auth.authorize(identity.agent_id, tool_def.name)
            if authz.allowed:
                tool_list.append({
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "inputSchema": tool_def.input_schema,
                })

        # Add CascadeGuard meta-tools
        meta_tools = self._get_meta_tool_definitions(identity)
        tool_list.extend(meta_tools)

        return MCPResponse(result={"tools": tool_list}, id=request.id)

    def _handle_tools_call(
        self,
        request: MCPRequest,
        identity: AgentIdentity,
    ) -> MCPResponse:
        """Handle tools/call — full safety pipeline.

        Pipeline:
        1. Extract tool name
        2. Authorize namespace access
        3. Parse envelope (zero overhead)
        4. Check per-agent velocity
        5. CascadeEngine attempt_delegation
        6. Log decision
        7. Forward or block
        """
        params = request.params
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        # Register agent if first time
        self._ensure_agent_registered(identity)

        # Check if this is a meta-tool
        if tool_name in self.META_TOOLS:
            return self._handle_meta_tool(tool_name, arguments, identity, request.id)

        # Step 2: Authorize
        authz = self._auth.authorize(identity.agent_id, tool_name)
        if not authz.allowed:
            self._log_and_emit("blocked", identity.agent_id, tool_name, authz.reason, 0)
            return MCPResponse(
                error={
                    "code": -32600,
                    "message": authz.reason,
                    "data": {"reason": "unauthorized"},
                },
                id=request.id,
            )

        # Step 3: Parse envelope (zero semantic overhead)
        start_us = time.perf_counter_ns()
        parse_result = self._parser.parse(params)

        if not parse_result.success:
            self._log_and_emit("blocked", identity.agent_id, tool_name, parse_result.error or "parse_error", 0)
            return MCPResponse(
                error={
                    "code": -32602,
                    "message": parse_result.error,
                    "data": {"reason": "envelope_parse_error"},
                },
                id=request.id,
            )

        envelope = parse_result.envelope

        # Step 4: Check per-agent velocity
        velocity_check = self._velocity.check_velocity(identity.agent_id)
        if not velocity_check.allowed:
            latency_us = (time.perf_counter_ns() - start_us) // 1000
            self._log_and_emit(
                "blocked", identity.agent_id, tool_name,
                velocity_check.reason, latency_us,
            )
            self._metrics.emit_agent_throttle(
                identity.agent_id, True, velocity_check.current_rate, velocity_check.threshold
            )
            return MCPResponse(
                error={
                    "code": -32000,
                    "message": velocity_check.reason,
                    "data": {
                        "reason": "agent_velocity_exceeded",
                        "current_rate": velocity_check.current_rate,
                        "threshold": velocity_check.threshold,
                    },
                },
                id=request.id,
            )

        # Step 5: CascadeEngine evaluation
        verdict = self._engine.attempt_delegation(
            source_id=identity.agent_id,
            target_id=tool_name,
            action=DelegationAction.DELEGATE,
            model_id=identity.metadata.get("model_id", "unknown"),
            tokens_used=float(envelope.token_budget),
        )

        latency_us = (time.perf_counter_ns() - start_us) // 1000

        # Step 6: Log decision
        verdict_str = "allowed" if verdict.allowed else "blocked"
        self._log.record(
            agent_id=identity.agent_id,
            tool_name=tool_name,
            verdict=verdict_str,
            reason=verdict.reason,
            impedance=verdict.impedance,
            flow_state=verdict.flow_state.value,
            tokens_consumed=verdict.tokens_consumed,
            schema_version=envelope.schema_version,
            latency_us=latency_us,
        )
        self._metrics.emit_invocation(tool_name, verdict_str)
        self._metrics.emit_latency(latency_us)

        # Track flow state changes
        if verdict.flow_state.value != self._metrics._flow_state:
            self._metrics.emit_flow_state_change(
                self._metrics._flow_state,
                verdict.flow_state.value,
                verdict.reason,
                1.0 - verdict.impedance,
            )

        # Step 7: Forward or block
        if not verdict.allowed:
            return MCPResponse(
                error={
                    "code": -32000,
                    "message": verdict.reason,
                    "data": {
                        "reason": verdict.reason,
                        "impedance": verdict.impedance,
                        "flow_state": verdict.flow_state.value,
                        "retry_delay": self._compute_retry_delay(verdict),
                    },
                },
                id=request.id,
            )

        # Record invocation in velocity counter
        self._velocity.record_invocation(identity.agent_id)

        # Forward to target server (arguments passed unmodified)
        invoke_result = self._registry.invoke_tool(tool_name, arguments)

        if not invoke_result.success:
            return MCPResponse(
                error={
                    "code": -32000,
                    "message": invoke_result.error,
                    "data": {"reason": "target_server_error", "server": invoke_result.server_name},
                },
                id=request.id,
            )

        # Record actual token consumption (if reported)
        # The response from target may include token usage
        actual_tokens = 0.0
        if isinstance(invoke_result.result, dict):
            actual_tokens = float(invoke_result.result.get("_tokens_used", 0.0))
        if actual_tokens > 0:
            self._engine.record_tokens(identity.agent_id, actual_tokens)

        # Return response unmodified
        return MCPResponse(result=invoke_result.result, id=request.id)

    def _handle_meta_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        identity: AgentIdentity,
        request_id: Any,
    ) -> MCPResponse:
        """Handle CascadeGuard meta-tools."""
        # Authorize meta-tool access
        authz = self._auth.authorize(identity.agent_id, tool_name)
        if not authz.allowed:
            return MCPResponse(
                error={"code": -32600, "message": authz.reason},
                id=request_id,
            )

        if tool_name == "cascadeguard/status":
            status = self._engine.get_status()
            return MCPResponse(result=status.model_dump(), id=request_id)

        elif tool_name == "cascadeguard/budget_status":
            usage = self._engine.get_agent_token_usage(identity.agent_id)
            return MCPResponse(result=usage, id=request_id)

        elif tool_name == "cascadeguard/decision_log":
            n = int(arguments.get("n", 50))
            entries = self._log.query(n)
            return MCPResponse(
                result=[e.to_dict() for e in entries],
                id=request_id,
            )

        elif tool_name == "cascadeguard/configure":
            result = self._config.apply_runtime_update(arguments)
            if result.valid:
                return MCPResponse(result={"success": True}, id=request_id)
            else:
                errors = [{"field": e.field, "message": e.message} for e in result.errors]
                return MCPResponse(
                    error={"code": -32602, "message": "Validation failed", "data": errors},
                    id=request_id,
                )

        elif tool_name == "cascadeguard/schema_versions":
            versions = self._parser.supported_versions
            return MCPResponse(result={"versions": versions}, id=request_id)

        elif tool_name == "cascadeguard/reload_status":
            status = self._config.get_reload_status()
            return MCPResponse(result=status, id=request_id)

        return MCPResponse(
            error={"code": -32601, "message": f"Unknown meta-tool: {tool_name}"},
            id=request_id,
        )

    def _ensure_agent_registered(self, identity: AgentIdentity) -> None:
        """Register agent with CascadeEngine on first connection."""
        if identity.agent_id in self._registered_agents:
            return

        policy = self._auth.get_policy(identity.agent_id)
        token_budget = policy.token_budget if policy else None

        self._engine.register_agent(
            agent_id=identity.agent_id,
            model_id=identity.metadata.get("model_id", "unknown"),
            parent_id=None,  # MCP clients are root agents
            token_budget=token_budget,
        )
        self._registered_agents.add(identity.agent_id)
        self._metrics.update_active_agents(len(self._registered_agents))

    def _log_and_emit(
        self,
        verdict: str,
        agent_id: str,
        tool_name: str,
        reason: str,
        latency_us: int,
    ) -> None:
        """Log decision and emit metrics (helper for early rejections)."""
        self._log.record(
            agent_id=agent_id,
            tool_name=tool_name,
            verdict=verdict,
            reason=reason,
            latency_us=latency_us,
        )
        self._metrics.emit_invocation(tool_name, verdict)
        self._metrics.emit_latency(latency_us)

    def _compute_retry_delay(self, verdict: DelegationVerdict) -> float:
        """Compute suggested retry delay based on verdict.

        Args:
            verdict: The blocking verdict.

        Returns:
            Suggested retry delay in seconds.
        """
        if verdict.flow_state == FlowState.PRESERVATION:
            return 60.0  # Full minute in preservation
        elif verdict.flow_state == FlowState.THROTTLED:
            return 5.0 + (verdict.impedance * 10.0)
        elif verdict.cycle_detected:
            return 0.0  # No retry for cycles (permanent failure)
        return 2.0  # Default backoff

    def _get_meta_tool_definitions(self, identity: AgentIdentity) -> List[Dict[str, Any]]:
        """Get CascadeGuard meta-tool definitions (filtered by auth)."""
        meta_tools = [
            {
                "name": "cascadeguard/status",
                "description": "Get CascadeEngine system status (flow state, impedance, agents)",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "cascadeguard/budget_status",
                "description": "Get token budget utilization for the requesting agent",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "cascadeguard/decision_log",
                "description": "Get recent decision log entries",
                "inputSchema": {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "default": 50}},
                },
            },
            {
                "name": "cascadeguard/schema_versions",
                "description": "List supported envelope schema versions",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "cascadeguard/reload_status",
                "description": "Get configuration reload status",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "cascadeguard/configure",
                "description": "Update CascadeGuard safety parameters at runtime (admin only)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_velocity": {"type": "number"},
                        "depth_limit": {"type": "integer"},
                        "fanout_limit": {"type": "integer"},
                        "preservation_threshold": {"type": "number"},
                    },
                },
            },
        ]

        # Filter by authorization
        return [
            t for t in meta_tools
            if self._auth.authorize(identity.agent_id, t["name"]).allowed
        ]
