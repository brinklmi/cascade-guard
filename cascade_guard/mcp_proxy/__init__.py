"""CascadeGuard MCP Proxy Middleware.

A Model Context Protocol server that intercepts tool calls and evaluates them
through CascadeGuard's deterministic safety checks (cycle detection, budget
enforcement, velocity control, flow state evaluation) before forwarding to
target tool servers.

Zero semantic overhead: only reads integer envelope routing fields.
"""

from .auth import AgentIdentity, AgentPolicy, AgentRole, AuthManager, AuthMode
from .config import ConfigManager, ProxyConfig
from .decision_log import DecisionLog, DecisionEntry
from .envelope import Envelope, EnvelopeParser, ParseResult
from .metrics import MetricsEmitter
from .registry import ToolRegistry, ToolDefinition
from .server import MCPProxyServer, MCPRequest, MCPResponse
from .shutdown import ShutdownManager, PersistedEngineState
from .velocity import AgentVelocityController, VelocityCheck

__version__ = "0.1.0"

__all__ = [
    "MCPProxyServer",
    "MCPRequest",
    "MCPResponse",
    "AuthManager",
    "AgentIdentity",
    "AgentPolicy",
    "AgentRole",
    "AuthMode",
    "ConfigManager",
    "ProxyConfig",
    "DecisionLog",
    "DecisionEntry",
    "EnvelopeParser",
    "Envelope",
    "ParseResult",
    "MetricsEmitter",
    "ToolRegistry",
    "ToolDefinition",
    "ShutdownManager",
    "PersistedEngineState",
    "AgentVelocityController",
    "VelocityCheck",
]
