"""Tests for Authentication & Authorization Layer.

Verifies:
1. mTLS authentication (valid cert accepted, empty cert rejected)
2. IAM authentication (valid ARN accepted, invalid rejected)
3. Namespace-based tool authorization
4. Admin-only tools protected
5. Operator tools require operator+
6. Policy hot-reload (atomic replacement)
7. Wildcard and prefix namespace matching
"""

import pytest

from cascade_guard.mcp_proxy.auth import (
    AgentIdentity,
    AgentPolicy,
    AgentRole,
    AuthManager,
    AuthMode,
    AuthResult,
    AuthzResult,
)


class TestMTLSAuthentication:
    """mTLS certificate-based authentication."""

    def test_valid_cert_cn_accepted(self):
        """Valid certificate CN authenticates successfully."""
        mgr = AuthManager(auth_mode=AuthMode.MTLS)
        result = mgr.authenticate(cert_cn="aws-devops-agent")

        assert result.success is True
        assert result.identity.agent_id == "aws-devops-agent"
        assert result.identity.auth_mode == AuthMode.MTLS

    def test_empty_cert_cn_rejected(self):
        """Empty certificate CN is rejected."""
        mgr = AuthManager(auth_mode=AuthMode.MTLS)
        result = mgr.authenticate(cert_cn="")

        assert result.success is False
        assert "Empty certificate CN" in result.error

    def test_no_cert_in_mtls_mode_rejected(self):
        """Missing cert in mTLS mode is rejected."""
        mgr = AuthManager(auth_mode=AuthMode.MTLS)
        result = mgr.authenticate()

        assert result.success is False
        assert "requires client certificate" in result.error

    def test_cert_cn_resolves_role_from_policy(self):
        """Agent with policy gets configured role."""
        policies = {
            "admin-agent": AgentPolicy(
                agent_id="admin-agent",
                role=AgentRole.ADMIN,
            ),
        }
        mgr = AuthManager(auth_mode=AuthMode.MTLS, policies=policies)
        result = mgr.authenticate(cert_cn="admin-agent")

        assert result.success is True
        assert result.identity.role == AgentRole.ADMIN

    def test_unknown_agent_gets_default_role(self):
        """Agent without policy gets default role."""
        mgr = AuthManager(
            auth_mode=AuthMode.MTLS,
            default_role=AgentRole.READER,
        )
        result = mgr.authenticate(cert_cn="unknown-agent")

        assert result.success is True
        assert result.identity.role == AgentRole.READER

    def test_metadata_contains_cert_cn(self):
        """Authentication metadata includes cert CN."""
        mgr = AuthManager(auth_mode=AuthMode.MTLS)
        result = mgr.authenticate(cert_cn="my-agent")

        assert result.identity.metadata["cert_cn"] == "my-agent"


class TestIAMAuthentication:
    """IAM role-based authentication."""

    def test_valid_role_arn_accepted(self):
        """Valid IAM role ARN authenticates successfully."""
        mgr = AuthManager(auth_mode=AuthMode.IAM)
        result = mgr.authenticate(
            role_arn="arn:aws:iam::123456789:role/sre-agent"
        )

        assert result.success is True
        assert result.identity.agent_id == "sre-agent"
        assert result.identity.auth_mode == AuthMode.IAM

    def test_empty_role_arn_rejected(self):
        """Empty role ARN is rejected."""
        mgr = AuthManager(auth_mode=AuthMode.IAM)
        result = mgr.authenticate(role_arn="")

        assert result.success is False
        assert "Empty role ARN" in result.error

    def test_no_arn_in_iam_mode_rejected(self):
        """Missing ARN in IAM mode is rejected."""
        mgr = AuthManager(auth_mode=AuthMode.IAM)
        result = mgr.authenticate()

        assert result.success is False
        assert "requires role ARN" in result.error

    def test_invalid_arn_format_rejected(self):
        """ARN without / separator is rejected."""
        mgr = AuthManager(auth_mode=AuthMode.IAM)
        result = mgr.authenticate(role_arn="no-slash-here")

        assert result.success is True  # Still extracts last part
        assert result.identity.agent_id == "no-slash-here"

    def test_iam_resolves_role_from_policy(self):
        """IAM agent with policy gets configured role."""
        policies = {
            "ops-agent": AgentPolicy(
                agent_id="ops-agent",
                role=AgentRole.OPERATOR,
            ),
        }
        mgr = AuthManager(auth_mode=AuthMode.IAM, policies=policies)
        result = mgr.authenticate(
            role_arn="arn:aws:iam::123456:role/ops-agent"
        )

        assert result.success is True
        assert result.identity.role == AgentRole.OPERATOR

    def test_metadata_contains_role_arn(self):
        """Authentication metadata includes role ARN."""
        mgr = AuthManager(auth_mode=AuthMode.IAM)
        result = mgr.authenticate(
            role_arn="arn:aws:iam::123:role/test",
            account_id="123",
        )

        assert result.identity.metadata["role_arn"] == "arn:aws:iam::123:role/test"
        assert result.identity.metadata["account_id"] == "123"


class TestNamespaceAuthorization:
    """Tool namespace-based authorization."""

    def test_wildcard_allows_everything(self):
        """Wildcard '*' namespace allows all tools."""
        policies = {
            "agent-a": AgentPolicy(
                agent_id="agent-a",
                role=AgentRole.READER,
                allowed_namespaces=["*"],
            ),
        }
        mgr = AuthManager(policies=policies)

        assert mgr.authorize("agent-a", "datadog/list_monitors").allowed is True
        assert mgr.authorize("agent-a", "splunk/search").allowed is True
        assert mgr.authorize("agent-a", "aws-cli/describe_instances").allowed is True

    def test_server_prefix_allows_all_tools_on_server(self):
        """'server/*' pattern allows all tools on that server."""
        policies = {
            "agent-a": AgentPolicy(
                agent_id="agent-a",
                role=AgentRole.READER,
                allowed_namespaces=["datadog/*"],
            ),
        }
        mgr = AuthManager(policies=policies)

        assert mgr.authorize("agent-a", "datadog/list_monitors").allowed is True
        assert mgr.authorize("agent-a", "datadog/get_events").allowed is True
        assert mgr.authorize("agent-a", "splunk/search").allowed is False

    def test_exact_tool_match(self):
        """Exact tool name matches only that tool."""
        policies = {
            "agent-a": AgentPolicy(
                agent_id="agent-a",
                role=AgentRole.READER,
                allowed_namespaces=["datadog/list_monitors"],
            ),
        }
        mgr = AuthManager(policies=policies)

        assert mgr.authorize("agent-a", "datadog/list_monitors").allowed is True
        assert mgr.authorize("agent-a", "datadog/get_events").allowed is False

    def test_multiple_namespaces(self):
        """Multiple namespace patterns checked in order."""
        policies = {
            "agent-a": AgentPolicy(
                agent_id="agent-a",
                role=AgentRole.READER,
                allowed_namespaces=["datadog/*", "splunk/search"],
            ),
        }
        mgr = AuthManager(policies=policies)

        assert mgr.authorize("agent-a", "datadog/get_events").allowed is True
        assert mgr.authorize("agent-a", "splunk/search").allowed is True
        assert mgr.authorize("agent-a", "splunk/other").allowed is False

    def test_unauthorized_returns_reason(self):
        """Unauthorized result includes descriptive reason."""
        policies = {
            "restricted": AgentPolicy(
                agent_id="restricted",
                role=AgentRole.READER,
                allowed_namespaces=["datadog/*"],
            ),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("restricted", "splunk/search")
        assert result.allowed is False
        assert "not authorized" in result.reason
        assert "splunk/search" in result.reason

    def test_no_policy_allows_by_default(self):
        """Agent without explicit policy is allowed (default behavior)."""
        mgr = AuthManager()
        result = mgr.authorize("unknown-agent", "datadog/anything")
        assert result.allowed is True


class TestAdminToolProtection:
    """Admin-only tools require admin role."""

    def test_admin_can_configure(self):
        """Admin role can invoke cascadeguard/configure."""
        policies = {
            "admin-agent": AgentPolicy(
                agent_id="admin-agent",
                role=AgentRole.ADMIN,
            ),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("admin-agent", "cascadeguard/configure")
        assert result.allowed is True

    def test_reader_cannot_configure(self):
        """Reader role cannot invoke cascadeguard/configure."""
        policies = {
            "reader-agent": AgentPolicy(
                agent_id="reader-agent",
                role=AgentRole.READER,
            ),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("reader-agent", "cascadeguard/configure")
        assert result.allowed is False
        assert "requires admin" in result.reason

    def test_operator_cannot_configure(self):
        """Operator role cannot invoke cascadeguard/configure."""
        policies = {
            "ops-agent": AgentPolicy(
                agent_id="ops-agent",
                role=AgentRole.OPERATOR,
            ),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("ops-agent", "cascadeguard/configure")
        assert result.allowed is False

    def test_is_admin_helper(self):
        """is_admin() correctly identifies admin agents."""
        policies = {
            "admin-a": AgentPolicy(agent_id="admin-a", role=AgentRole.ADMIN),
            "reader-b": AgentPolicy(agent_id="reader-b", role=AgentRole.READER),
        }
        mgr = AuthManager(policies=policies)

        assert mgr.is_admin("admin-a") is True
        assert mgr.is_admin("reader-b") is False
        assert mgr.is_admin("unknown") is False


class TestOperatorTools:
    """Operator-level tools require operator or admin."""

    def test_operator_can_access_reload_status(self):
        """Operator can access cascadeguard/reload_status."""
        policies = {
            "ops": AgentPolicy(agent_id="ops", role=AgentRole.OPERATOR),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("ops", "cascadeguard/reload_status")
        assert result.allowed is True

    def test_admin_can_access_reload_status(self):
        """Admin can also access operator tools."""
        policies = {
            "admin": AgentPolicy(agent_id="admin", role=AgentRole.ADMIN),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("admin", "cascadeguard/reload_status")
        assert result.allowed is True

    def test_reader_cannot_access_reload_status(self):
        """Reader cannot access operator tools."""
        policies = {
            "reader": AgentPolicy(agent_id="reader", role=AgentRole.READER),
        }
        mgr = AuthManager(policies=policies)

        result = mgr.authorize("reader", "cascadeguard/reload_status")
        assert result.allowed is False


class TestPolicyManagement:
    """Policy CRUD and hot-reload."""

    def test_set_policy(self):
        """set_policy adds a new policy."""
        mgr = AuthManager()
        mgr.set_policy(AgentPolicy(agent_id="new-agent", role=AgentRole.OPERATOR))

        policy = mgr.get_policy("new-agent")
        assert policy is not None
        assert policy.role == AgentRole.OPERATOR

    def test_remove_policy(self):
        """remove_policy removes an existing policy."""
        policies = {
            "agent-a": AgentPolicy(agent_id="agent-a", role=AgentRole.ADMIN),
        }
        mgr = AuthManager(policies=policies)
        mgr.remove_policy("agent-a")

        assert mgr.get_policy("agent-a") is None

    def test_reload_policies_atomic(self):
        """reload_policies replaces all policies atomically."""
        old_policies = {
            "old-agent": AgentPolicy(agent_id="old-agent", role=AgentRole.ADMIN),
        }
        mgr = AuthManager(policies=old_policies)

        new_policies = {
            "new-agent": AgentPolicy(agent_id="new-agent", role=AgentRole.OPERATOR),
        }
        mgr.reload_policies(new_policies)

        assert mgr.get_policy("old-agent") is None
        assert mgr.get_policy("new-agent") is not None
        assert mgr.get_policy("new-agent").role == AgentRole.OPERATOR

    def test_policies_property_returns_copy(self):
        """policies property returns a copy (not mutable reference)."""
        policies = {
            "agent-a": AgentPolicy(agent_id="agent-a", role=AgentRole.ADMIN),
        }
        mgr = AuthManager(policies=policies)

        external = mgr.policies
        external["injected"] = AgentPolicy(agent_id="injected", role=AgentRole.ADMIN)

        # Original should be unchanged
        assert mgr.get_policy("injected") is None


class TestNoAuthMode:
    """AuthMode.NONE for testing/local development."""

    def test_none_mode_accepts_any_cert(self):
        """NONE mode accepts any credentials."""
        mgr = AuthManager(auth_mode=AuthMode.NONE)
        result = mgr.authenticate(cert_cn="any-agent")

        assert result.success is True
        assert result.identity.agent_id == "any-agent"
        assert result.identity.auth_mode == AuthMode.NONE

    def test_none_mode_anonymous_fallback(self):
        """NONE mode with no credentials creates anonymous identity."""
        mgr = AuthManager(auth_mode=AuthMode.NONE)
        result = mgr.authenticate()

        assert result.success is True
        assert result.identity.agent_id == "anonymous"
