"""Tests for Configuration Manager with Hot-Reload.

Verifies:
1. Default config is valid
2. JSON config file loading
3. Environment variable overrides
4. Validation rejects invalid parameters
5. Invalid reload retains previous config
6. Runtime updates for engine params
7. Reload status tracking
"""

import json
import os
import tempfile
import time

import pytest

from cascade_guard.mcp_proxy.config import (
    ConfigManager,
    ConfigValidator,
    EngineConfig,
    ProxyConfig,
    ShutdownConfig,
    TargetServerConfig,
    TransportConfig,
    ValidationResult,
    VelocityConfig,
)


class TestDefaultConfig:
    """Default configuration is valid and has correct values."""

    def test_default_config_is_valid(self):
        """Default ProxyConfig passes validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        result = validator.validate(config)
        assert result.valid is True
        assert result.errors == []

    def test_default_engine_values(self):
        """Default engine parameters match expected values."""
        config = ProxyConfig()
        assert config.engine.max_velocity == 50.0
        assert config.engine.depth_limit == 10
        assert config.engine.fanout_limit == 20
        assert config.engine.preservation_threshold == 0.3
        assert config.engine.window_seconds == 60.0

    def test_default_transport_values(self):
        """Default transport is SSE on port 8080."""
        config = ProxyConfig()
        assert config.transport.type == "sse"
        assert config.transport.port == 8080

    def test_default_shutdown_values(self):
        """Default drain timeout is 30 seconds."""
        config = ProxyConfig()
        assert config.shutdown.drain_timeout_seconds == 30
        assert config.shutdown.persist_state is True


class TestConfigFileLoading:
    """Loading configuration from JSON files."""

    def test_load_from_json_file(self):
        """Loads config from a JSON file."""
        config_data = {
            "engine": {
                "max_velocity": 100.0,
                "depth_limit": 15,
            },
            "transport": {
                "port": 9090,
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(config_data, f)
            f.flush()
            config_path = f.name

        try:
            mgr = ConfigManager(config_path=config_path)
            config = mgr.load()

            assert config.engine.max_velocity == 100.0
            assert config.engine.depth_limit == 15
            assert config.transport.port == 9090
            # Unspecified values keep defaults
            assert config.engine.fanout_limit == 20
        finally:
            os.unlink(config_path)

    def test_load_with_targets(self):
        """Loads target server configuration."""
        config_data = {
            "targets": [
                {
                    "name": "datadog",
                    "transport": "http",
                    "endpoint": "https://datadog-mcp.internal:9090",
                    "auth": {"type": "bearer", "token_env": "DD_TOKEN"},
                },
                {
                    "name": "splunk",
                    "transport": "stdio",
                    "command": ["splunk-mcp-server"],
                },
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(config_data, f)
            f.flush()
            config_path = f.name

        try:
            mgr = ConfigManager(config_path=config_path)
            config = mgr.load()

            assert len(config.targets) == 2
            assert config.targets[0].name == "datadog"
            assert config.targets[0].auth_type == "bearer"
            assert config.targets[0].auth_token_env == "DD_TOKEN"
            assert config.targets[1].name == "splunk"
            assert config.targets[1].command == ["splunk-mcp-server"]
        finally:
            os.unlink(config_path)

    def test_missing_file_uses_defaults(self):
        """Non-existent config file uses defaults."""
        mgr = ConfigManager(config_path="/nonexistent/path.json")
        config = mgr.load()

        assert config.engine.max_velocity == 50.0
        assert config.transport.port == 8080


class TestEnvVarOverrides:
    """Environment variable overrides."""

    def test_env_var_overrides_engine(self):
        """CASCADEGUARD_ENGINE_* env vars override config."""
        os.environ["CASCADEGUARD_ENGINE_MAX_VELOCITY"] = "200.0"
        os.environ["CASCADEGUARD_ENGINE_DEPTH_LIMIT"] = "25"

        try:
            mgr = ConfigManager()
            config = mgr.load()

            assert config.engine.max_velocity == 200.0
            assert config.engine.depth_limit == 25
        finally:
            del os.environ["CASCADEGUARD_ENGINE_MAX_VELOCITY"]
            del os.environ["CASCADEGUARD_ENGINE_DEPTH_LIMIT"]

    def test_env_var_overrides_transport(self):
        """CASCADEGUARD_TRANSPORT_PORT overrides port."""
        os.environ["CASCADEGUARD_TRANSPORT_PORT"] = "3000"

        try:
            mgr = ConfigManager()
            config = mgr.load()
            assert config.transport.port == 3000
        finally:
            del os.environ["CASCADEGUARD_TRANSPORT_PORT"]

    def test_invalid_env_var_ignored(self):
        """Invalid env var value is ignored (warning logged)."""
        os.environ["CASCADEGUARD_ENGINE_MAX_VELOCITY"] = "not_a_number"

        try:
            mgr = ConfigManager()
            config = mgr.load()
            # Should retain default, not crash
            assert config.engine.max_velocity == 50.0
        finally:
            del os.environ["CASCADEGUARD_ENGINE_MAX_VELOCITY"]


class TestValidation:
    """Configuration validation."""

    def test_valid_config_passes(self):
        """Valid config passes validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        result = validator.validate(config)
        assert result.valid is True

    def test_negative_max_velocity_rejected(self):
        """Negative max_velocity fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.engine.max_velocity = -1.0
        result = validator.validate(config)
        assert result.valid is False
        assert any("max_velocity" in e.field for e in result.errors)

    def test_zero_depth_limit_rejected(self):
        """Zero depth_limit fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.engine.depth_limit = 0
        result = validator.validate(config)
        assert result.valid is False

    def test_invalid_transport_type_rejected(self):
        """Invalid transport type fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.transport.type = "websocket"
        result = validator.validate(config)
        assert result.valid is False
        assert any("transport.type" in e.field for e in result.errors)

    def test_invalid_auth_mode_rejected(self):
        """Invalid auth mode fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.auth.mode = "oauth"
        result = validator.validate(config)
        assert result.valid is False

    def test_negative_token_budget_rejected(self):
        """Negative token_budget fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.engine.token_budget = -100.0
        result = validator.validate(config)
        assert result.valid is False

    def test_target_without_name_rejected(self):
        """Target server without name fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.targets = [TargetServerConfig(name="", transport="http")]
        result = validator.validate(config)
        assert result.valid is False

    def test_preservation_threshold_out_of_range(self):
        """preservation_threshold > 1.0 fails validation."""
        validator = ConfigValidator()
        config = ProxyConfig()
        config.engine.preservation_threshold = 1.5
        result = validator.validate(config)
        assert result.valid is False


class TestInvalidReloadRetainsPrevious:
    """Invalid config reload retains previous valid config."""

    def test_invalid_reload_keeps_old_config(self):
        """Failed reload retains previous configuration."""
        # Start with valid config
        valid_data = {"engine": {"max_velocity": 75.0}}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(valid_data, f)
            f.flush()
            config_path = f.name

        try:
            mgr = ConfigManager(config_path=config_path)
            config = mgr.load()
            assert config.engine.max_velocity == 75.0

            # Write invalid config
            with open(config_path, "w") as f:
                json.dump({"engine": {"max_velocity": -999}}, f)

            # Force mtime change detection
            mgr._last_file_mtime = 0

            # Attempt reload
            success = mgr.check_for_changes()
            assert success is False

            # Previous config retained
            assert mgr.config.engine.max_velocity == 75.0

            # Reload status shows failure
            status = mgr.get_reload_status()
            assert status["success"] is False
            assert len(status["errors"]) > 0
        finally:
            os.unlink(config_path)


class TestRuntimeUpdates:
    """Runtime parameter updates via MCP tool."""

    def test_update_max_velocity(self):
        """Can update max_velocity at runtime."""
        mgr = ConfigManager()
        mgr.load()

        result = mgr.apply_runtime_update({"max_velocity": 100.0})
        assert result.valid is True
        assert mgr.config.engine.max_velocity == 100.0

    def test_update_multiple_params(self):
        """Can update multiple engine params at once."""
        mgr = ConfigManager()
        mgr.load()

        result = mgr.apply_runtime_update({
            "max_velocity": 80.0,
            "depth_limit": 20,
            "fanout_limit": 30,
        })
        assert result.valid is True
        assert mgr.config.engine.max_velocity == 80.0
        assert mgr.config.engine.depth_limit == 20
        assert mgr.config.engine.fanout_limit == 30

    def test_invalid_runtime_update_rejected(self):
        """Invalid runtime value is rejected."""
        mgr = ConfigManager()
        mgr.load()
        original_velocity = mgr.config.engine.max_velocity

        result = mgr.apply_runtime_update({"max_velocity": -5.0})
        assert result.valid is False

        # Original value retained
        assert mgr.config.engine.max_velocity == original_velocity

    def test_non_runtime_param_rejected(self):
        """Non-runtime params (e.g. port) cannot be updated at runtime."""
        mgr = ConfigManager()
        mgr.load()

        result = mgr.apply_runtime_update({"port": 9090})
        assert result.valid is False
        assert "cannot be updated at runtime" in result.errors[0].message

    def test_on_reload_callback_invoked(self):
        """on_reload callback is called on successful runtime update."""
        received_configs = []
        mgr = ConfigManager(on_reload=lambda c: received_configs.append(c))
        mgr.load()

        mgr.apply_runtime_update({"max_velocity": 120.0})
        assert len(received_configs) == 1
        assert received_configs[0].engine.max_velocity == 120.0


class TestReloadStatus:
    """Reload status tracking."""

    def test_initial_status_after_load(self):
        """After successful load, status shows success."""
        mgr = ConfigManager()
        mgr.load()

        status = mgr.get_reload_status()
        assert status["success"] is True
        assert status["last_successful_reload"] is not None
        assert status["errors"] == []

    def test_status_after_failed_reload(self):
        """After failed reload, status shows failure with errors."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"engine": {"max_velocity": 50.0}}, f)
            f.flush()
            config_path = f.name

        try:
            mgr = ConfigManager(config_path=config_path)
            mgr.load()

            # Write invalid config
            with open(config_path, "w") as f:
                f.write("not valid json {{{")

            mgr._last_file_mtime = 0
            mgr.check_for_changes()

            status = mgr.get_reload_status()
            assert status["success"] is False
            assert len(status["errors"]) > 0
        finally:
            os.unlink(config_path)
