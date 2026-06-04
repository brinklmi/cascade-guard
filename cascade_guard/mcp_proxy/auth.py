"""Authentication & Authorization — mTLS + IAM, role-based namespace access.

Authenticates MCP clients via client certificates or IAM roles.
Authorizes tool access based on agent policy (namespace membership).

Security model:
- mTLS: Extract CN from client cert → resolve agent_id
- IAM: Extract role from request signing → resolve agent_id
- Authorization: agent_id → role → allowed_namespaces
- Admin check: only 'admin' role can invoke cascadeguard/configure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AuthMode(str, Enum):
    """Supported authentication modes."""

    MTLS = "mtls"
    IAM = "iam"
    NONE = "none"  # For testing/local development only


class AgentRole(str, Enum):
    """Agent authorization roles (least to most privileged)."""

    READER = "reader"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class AgentIdentity:
    """Authenticated agent identity extracted from connection credentials.

    Attributes:
        agent_id: Unique identifier for the agent.
        role: Authorization role determining access level.
        auth_mode: How the agent was authenticated.
        metadata: Additional identity metadata (cert CN, IAM role ARN, etc.)
    """

    agent_id: str
    role: AgentRole = AgentRole.READER
    auth_mode: AuthMode = AuthMode.NONE
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class AgentPolicy:
    """Authorization policy for a single agent.

    Defines what tool namespaces an agent can access and
    optional resource limits.

    Attributes:
        agent_id: The agent this policy applies to.
        role: The agent's authorization role.
        allowed_namespaces: List of tool namespace patterns (e.g. ["datadog/*", "splunk/*"]).
        per_agent_velocity: Optional per-agent velocity override.
        token_budget: Optional per-agent token budget.
    """

    agent_id: str
    role: AgentRole = AgentRole.READER
    allowed_namespaces: List[str] = field(default_factory=lambda: ["*"])
    per_agent_velocity: Optional[float] = None
    token_budget: Optional[float] = None


@dataclass(frozen=True)
class AuthResult:
    """Result of an authentication attempt.

    Attributes:
        success: Whether authentication succeeded.
        identity: The authenticated identity (None if failed).
        error: Error message if authentication failed.
    """

    success: bool
    identity: Optional[AgentIdentity] = None
    error: str = ""


@dataclass(frozen=True)
class AuthzResult:
    """Result of an authorization check.

    Attributes:
        allowed: Whether the action is authorized.
        reason: Explanation (empty if allowed, error if denied).
    """

    allowed: bool
    reason: str = ""


class AuthManager:
    """Authentication and authorization manager for MCP proxy.

    Handles:
    - mTLS certificate-based authentication (CN → agent_id)
    - IAM role-based authentication (role ARN → agent_id)
    - Namespace-based tool authorization
    - Admin role enforcement for configuration tools

    Thread-safety: Policy is mutable (hot-reload), but operations are
    read-only during request processing. Reload replaces the entire
    policy dict atomically.
    """

    # Tool patterns that require admin role
    ADMIN_TOOLS = frozenset([
        "cascadeguard/configure",
    ])

    # Tool patterns accessible to operator+ roles
    OPERATOR_TOOLS = frozenset([
        "cascadeguard/reload_status",
    ])

    def __init__(
        self,
        auth_mode: AuthMode = AuthMode.NONE,
        policies: Optional[Dict[str, AgentPolicy]] = None,
        default_role: AgentRole = AgentRole.READER,
    ):
        """Initialize the auth manager.

        Args:
            auth_mode: Primary authentication mode.
            policies: Dict of agent_id → AgentPolicy. If None, empty (deny all).
            default_role: Role assigned to agents without explicit policy.
        """
        self._auth_mode = auth_mode
        self._policies: Dict[str, AgentPolicy] = policies or {}
        self._default_role = default_role

    @property
    def auth_mode(self) -> AuthMode:
        """Current authentication mode."""
        return self._auth_mode

    @property
    def policies(self) -> Dict[str, AgentPolicy]:
        """Current agent policies (read-only view)."""
        return dict(self._policies)

    def authenticate_mtls(self, cert_cn: str) -> AuthResult:
        """Authenticate via mTLS client certificate.

        Extracts agent_id from certificate Common Name (CN).

        Args:
            cert_cn: The Common Name from the client certificate.

        Returns:
            AuthResult with success/failure and identity.
        """
        if not cert_cn:
            return AuthResult(
                success=False,
                error="Empty certificate CN — cannot authenticate",
            )

        # CN is the agent_id
        agent_id = cert_cn
        policy = self._policies.get(agent_id)

        if policy is None:
            # Unknown agent — use default role
            identity = AgentIdentity(
                agent_id=agent_id,
                role=self._default_role,
                auth_mode=AuthMode.MTLS,
                metadata={"cert_cn": cert_cn},
            )
        else:
            identity = AgentIdentity(
                agent_id=agent_id,
                role=policy.role,
                auth_mode=AuthMode.MTLS,
                metadata={"cert_cn": cert_cn},
            )

        return AuthResult(success=True, identity=identity)

    def authenticate_iam(self, role_arn: str, account_id: str = "") -> AuthResult:
        """Authenticate via IAM role.

        Extracts agent_id from IAM role ARN.

        Args:
            role_arn: The IAM role ARN (e.g. "arn:aws:iam::123456:role/agent-name").
            account_id: The AWS account ID for validation.

        Returns:
            AuthResult with success/failure and identity.
        """
        if not role_arn:
            return AuthResult(
                success=False,
                error="Empty role ARN — cannot authenticate",
            )

        # Extract role name as agent_id from ARN
        # ARN format: arn:aws:iam::ACCOUNT:role/ROLE_NAME
        # If no "/" present, use the entire string as agent_id (graceful fallback)
        try:
            parts = role_arn.split("/")
            agent_id = parts[-1] if len(parts) >= 2 else role_arn
        except (IndexError, AttributeError):
            return AuthResult(
                success=False,
                error=f"Cannot parse role ARN: {role_arn}",
            )

        policy = self._policies.get(agent_id)

        if policy is None:
            identity = AgentIdentity(
                agent_id=agent_id,
                role=self._default_role,
                auth_mode=AuthMode.IAM,
                metadata={"role_arn": role_arn, "account_id": account_id},
            )
        else:
            identity = AgentIdentity(
                agent_id=agent_id,
                role=policy.role,
                auth_mode=AuthMode.IAM,
                metadata={"role_arn": role_arn, "account_id": account_id},
            )

        return AuthResult(success=True, identity=identity)

    def authenticate(
        self,
        cert_cn: Optional[str] = None,
        role_arn: Optional[str] = None,
        account_id: str = "",
    ) -> AuthResult:
        """Authenticate using the configured auth mode.

        Dispatches to the appropriate method based on auth_mode.

        Args:
            cert_cn: Client certificate CN (for mTLS mode).
            role_arn: IAM role ARN (for IAM mode).
            account_id: AWS account ID (for IAM validation).

        Returns:
            AuthResult with success/failure and identity.
        """
        if self._auth_mode == AuthMode.MTLS:
            if cert_cn is None:
                return AuthResult(
                    success=False,
                    error="mTLS mode requires client certificate",
                )
            return self.authenticate_mtls(cert_cn)

        elif self._auth_mode == AuthMode.IAM:
            if role_arn is None:
                return AuthResult(
                    success=False,
                    error="IAM mode requires role ARN",
                )
            return self.authenticate_iam(role_arn, account_id)

        elif self._auth_mode == AuthMode.NONE:
            # No auth — create identity from whatever is available
            agent_id = cert_cn or (role_arn.split("/")[-1] if role_arn else "anonymous")
            policy = self._policies.get(agent_id)
            role = policy.role if policy else self._default_role

            return AuthResult(
                success=True,
                identity=AgentIdentity(
                    agent_id=agent_id,
                    role=role,
                    auth_mode=AuthMode.NONE,
                ),
            )

        return AuthResult(success=False, error=f"Unknown auth mode: {self._auth_mode}")

    def authorize(self, agent_id: str, tool_name: str) -> AuthzResult:
        """Check if an agent is authorized to invoke a tool.

        Authorization rules:
        1. Admin tools require AgentRole.ADMIN
        2. Operator tools require AgentRole.OPERATOR or higher
        3. All other tools checked against allowed_namespaces

        Args:
            agent_id: The authenticated agent ID.
            tool_name: The fully-qualified tool name (e.g. "datadog/list_monitors").

        Returns:
            AuthzResult with allowed/denied and reason.
        """
        policy = self._policies.get(agent_id)
        role = policy.role if policy else self._default_role

        # Check admin-only tools
        if tool_name in self.ADMIN_TOOLS:
            if role != AgentRole.ADMIN:
                return AuthzResult(
                    allowed=False,
                    reason=f"Tool '{tool_name}' requires admin role, agent has '{role.value}'",
                )
            return AuthzResult(allowed=True)

        # Check operator tools
        if tool_name in self.OPERATOR_TOOLS:
            if role not in (AgentRole.OPERATOR, AgentRole.ADMIN):
                return AuthzResult(
                    allowed=False,
                    reason=f"Tool '{tool_name}' requires operator role, agent has '{role.value}'",
                )
            return AuthzResult(allowed=True)

        # Check namespace authorization
        if policy is None:
            # No policy — use default (allow all for default role)
            return AuthzResult(allowed=True)

        # Check if tool matches any allowed namespace pattern
        if self._matches_namespace(tool_name, policy.allowed_namespaces):
            return AuthzResult(allowed=True)

        return AuthzResult(
            allowed=False,
            reason=f"Agent '{agent_id}' not authorized for tool '{tool_name}'. "
                   f"Allowed namespaces: {policy.allowed_namespaces}",
        )

    def is_admin(self, agent_id: str) -> bool:
        """Check if an agent has admin role.

        Args:
            agent_id: The agent to check.

        Returns:
            True if agent has admin role.
        """
        policy = self._policies.get(agent_id)
        if policy is None:
            return self._default_role == AgentRole.ADMIN
        return policy.role == AgentRole.ADMIN

    def get_policy(self, agent_id: str) -> Optional[AgentPolicy]:
        """Get the policy for a specific agent.

        Args:
            agent_id: The agent to query.

        Returns:
            AgentPolicy if found, None otherwise.
        """
        return self._policies.get(agent_id)

    def set_policy(self, policy: AgentPolicy) -> None:
        """Set or update a policy for an agent.

        Args:
            policy: The agent policy to set.
        """
        self._policies[policy.agent_id] = policy

    def remove_policy(self, agent_id: str) -> None:
        """Remove a policy for an agent.

        Args:
            agent_id: The agent whose policy to remove.
        """
        self._policies.pop(agent_id, None)

    def reload_policies(self, policies: Dict[str, AgentPolicy]) -> None:
        """Atomically replace all policies (hot-reload).

        Args:
            policies: New policy dict to replace existing.
        """
        self._policies = dict(policies)

    def _matches_namespace(self, tool_name: str, namespaces: List[str]) -> bool:
        """Check if a tool name matches any of the allowed namespace patterns.

        Supports patterns:
        - "*" matches everything
        - "server/*" matches all tools on a server
        - "server/tool" matches exact tool
        - "cascadeguard/*" matches all cascadeguard meta-tools

        Args:
            tool_name: Fully-qualified tool name.
            namespaces: List of namespace patterns.

        Returns:
            True if tool matches any pattern.
        """
        for pattern in namespaces:
            if pattern == "*":
                return True
            if pattern.endswith("/*"):
                prefix = pattern[:-2]  # Remove /*
                if tool_name.startswith(prefix + "/") or tool_name == prefix:
                    return True
            elif pattern == tool_name:
                return True

        return False
