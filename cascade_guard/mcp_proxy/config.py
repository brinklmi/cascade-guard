"""Configuration & Hot-Reload — env vars + YAML/JSON + runtime MCP tool updates.

Watches config file (5s poll), validates parameters, applies changes
without dropping in-flight requests. Retains last valid config on failure.

Configuration precedence (highest to lowest):
1. Runtime updates via cascadeguard/configure MCP tool
2. Environment variables (CASCADEGUARD_* prefix)
3. Configuration file (YAML or JSON)
4. Built-in defaults
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("cascadeguard.config")


@dataclass
class TLSConfig:
    """TLS certificate configuration."""

    cert_path: str = ""
    key_path: str = ""
    ca_path: str = ""
    enabled: bool = False


@dataclass
class TransportConfig:
    """Transport layer configuration."""

    type: str = "sse"  # "stdio" | "sse"
    port: int = 8080
    host: str = "0.0.0.0"
    tls: TLSConfig = field(default_factory=TLSConfig)


@dataclass
class EngineConfig:
    """CascadeEngine configuration parameters."""

    max_velocity: float = 50.0
    depth_limit: int = 10
    fanout_limit: int = 20
    preservation_threshold: float = 0.3
    window_seconds: float = 60.0
    token_budget: Optional[float] = None
    dollar_budget: Optional[float] = None
    cost_per_1k_tokens: float = 0.03


@dataclass
class AuthConfig:
    """Authentication configuration."""

    mode: str = "none"  # "mtls" | "iam" | "none"
    policy_file: str = ""


@dataclass
class VelocityConfig:
    """Per-agent velocity configuration."""

    default_per_agent: Optional[float] = None  # None = 2x engine.max_velocity
    overrides: Dict[str, float] = field(default_factory=dict)


@dataclass
class TargetServerConfig:
    """Single target server configuration."""

    name: str = ""
    transport: str = "http"  # "stdio" | "http" | "sse"
    endpoint: str = ""
    command: List[str] = field(default_factory=list)
    auth_type: str = "none"  # "none" | "bearer" | "iam"
    auth_token_env: str = ""


@dataclass
class ShutdownConfig:
    """Graceful shutdown configuration."""

    drain_timeout_seconds: int = 30
    persist_state: bool = True
    state_path: str = "/var/lib/cascadeguard/engine_state.json"


@dataclass
class ProxyConfig:
    """Complete MCP proxy configuration.

    Holds all configurable parameters for the proxy server.
    Immutable after construction — new config replaces old atomically.
    """

    transport: TransportConfig = field(default_factory=TransportConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    velocity: VelocityConfig = field(default_factory=VelocityConfig)
    targets: List[TargetServerConfig] = field(default_factory=list)
    shutdown: ShutdownConfig = field(default_factory=ShutdownConfig)


@dataclass(frozen=True)
class ValidationError:
    """A single configuration validation error."""

    field: str
    message: str
    value: Any = None


@dataclass
class ValidationResult:
    """Result of validating a configuration."""

    valid: bool
    errors: List[ValidationError] = field(default_factory=list)


@dataclass
class ReloadStatus:
    """Status of the last configuration reload attempt."""

    last_successful_reload: Optional[float] = None  # Unix timestamp
    last_attempt: Optional[float] = None
    success: bool = True
    errors: List[str] = field(default_factory=list)
    config_path: str = ""


class ConfigValidator:
    """Validates proxy configuration parameters against acceptable ranges."""

    # Parameter validation ranges
    RANGES = {
        "engine.max_velocity": (0.1, 100000.0),
        "engine.depth_limit": (1, 1000),
        "engine.fanout_limit": (1, 10000),
        "engine.preservation_threshold": (0.0, 1.0),
        "engine.window_seconds": (1.0, 86400.0),
        "engine.cost_per_1k_tokens": (0.0, 1.0),
        "transport.port": (1, 65535),
        "shutdown.drain_timeout_seconds": (1, 600),
        "velocity.default_per_agent": (0.1, 100000.0),
    }

    VALID_TRANSPORT_TYPES = {"stdio", "sse", "http"}
    VALID_AUTH_MODES = {"none", "mtls", "iam"}

    def validate(self, config: ProxyConfig) -> ValidationResult:
        """Validate a ProxyConfig against acceptable ranges.

        Args:
            config: The configuration to validate.

        Returns:
            ValidationResult with valid flag and any errors.
        """
        errors: List[ValidationError] = []

        # Engine parameter validation
        self._validate_range(errors, "engine.max_velocity", config.engine.max_velocity)
        self._validate_range(errors, "engine.depth_limit", config.engine.depth_limit)
        self._validate_range(errors, "engine.fanout_limit", config.engine.fanout_limit)
        self._validate_range(errors, "engine.preservation_threshold", config.engine.preservation_threshold)
        self._validate_range(errors, "engine.window_seconds", config.engine.window_seconds)
        self._validate_range(errors, "engine.cost_per_1k_tokens", config.engine.cost_per_1k_tokens)

        # Token/dollar budget (if set, must be positive)
        if config.engine.token_budget is not None and config.engine.token_budget <= 0:
            errors.append(ValidationError(
                field="engine.token_budget",
                message="token_budget must be positive if set",
                value=config.engine.token_budget,
            ))
        if config.engine.dollar_budget is not None and config.engine.dollar_budget <= 0:
            errors.append(ValidationError(
                field="engine.dollar_budget",
                message="dollar_budget must be positive if set",
                value=config.engine.dollar_budget,
            ))

        # Transport validation
        self._validate_range(errors, "transport.port", config.transport.port)
        if config.transport.type not in self.VALID_TRANSPORT_TYPES:
            errors.append(ValidationError(
                field="transport.type",
                message=f"Must be one of {self.VALID_TRANSPORT_TYPES}",
                value=config.transport.type,
            ))

        # Auth validation
        if config.auth.mode not in self.VALID_AUTH_MODES:
            errors.append(ValidationError(
                field="auth.mode",
                message=f"Must be one of {self.VALID_AUTH_MODES}",
                value=config.auth.mode,
            ))

        # Shutdown validation
        self._validate_range(errors, "shutdown.drain_timeout_seconds", config.shutdown.drain_timeout_seconds)

        # Velocity validation
        if config.velocity.default_per_agent is not None:
            self._validate_range(errors, "velocity.default_per_agent", config.velocity.default_per_agent)

        # Per-agent overrides must be positive
        for agent_id, threshold in config.velocity.overrides.items():
            if threshold <= 0:
                errors.append(ValidationError(
                    field=f"velocity.overrides.{agent_id}",
                    message="Per-agent velocity threshold must be positive",
                    value=threshold,
                ))

        # Target servers must have names
        for i, target in enumerate(config.targets):
            if not target.name:
                errors.append(ValidationError(
                    field=f"targets[{i}].name",
                    message="Target server must have a name",
                ))
            if target.transport not in self.VALID_TRANSPORT_TYPES:
                errors.append(ValidationError(
                    field=f"targets[{i}].transport",
                    message=f"Must be one of {self.VALID_TRANSPORT_TYPES}",
                    value=target.transport,
                ))

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def _validate_range(
        self,
        errors: List[ValidationError],
        field_path: str,
        value: Any,
    ) -> None:
        """Check if a value is within its configured range."""
        if field_path not in self.RANGES:
            return

        min_val, max_val = self.RANGES[field_path]
        if value < min_val or value > max_val:
            errors.append(ValidationError(
                field=field_path,
                message=f"Must be between {min_val} and {max_val}",
                value=value,
            ))


class ConfigManager:
    """Configuration manager with hot-reload support.

    Loads configuration from env vars and config files.
    Watches for file changes (5s poll interval).
    Validates before applying. Retains last valid on failure.
    """

    ENV_PREFIX = "CASCADEGUARD_"
    POLL_INTERVAL = 5.0  # seconds

    def __init__(
        self,
        config_path: Optional[str] = None,
        on_reload: Optional[Callable[[ProxyConfig], None]] = None,
    ):
        """Initialize configuration manager.

        Args:
            config_path: Path to YAML/JSON config file. If None, uses env var
                         CASCADEGUARD_CONFIG_PATH or defaults to no file.
            on_reload: Callback invoked with new config after successful reload.
        """
        self._config_path = config_path or os.environ.get("CASCADEGUARD_CONFIG_PATH", "")
        self._on_reload = on_reload
        self._validator = ConfigValidator()
        self._current_config: ProxyConfig = ProxyConfig()
        self._reload_status = ReloadStatus(config_path=self._config_path)
        self._last_file_mtime: float = 0.0

    @property
    def config(self) -> ProxyConfig:
        """Current active configuration."""
        return self._current_config

    @property
    def reload_status(self) -> ReloadStatus:
        """Status of the last reload attempt."""
        return self._reload_status

    def load(self) -> ProxyConfig:
        """Load configuration from file and env vars.

        Precedence: env vars override file values override defaults.

        Returns:
            The loaded and validated ProxyConfig.

        Raises:
            ValueError: If the loaded configuration fails validation.
        """
        config = ProxyConfig()

        # Load from file if path exists
        if self._config_path:
            file_config = self._load_from_file(self._config_path)
            if file_config is not None:
                config = file_config

        # Apply environment variable overrides
        config = self._apply_env_overrides(config)

        # Validate
        result = self._validator.validate(config)
        if not result.valid:
            error_msgs = [f"{e.field}: {e.message}" for e in result.errors]
            raise ValueError(
                f"Configuration validation failed: {'; '.join(error_msgs)}"
            )

        self._current_config = config
        self._reload_status = ReloadStatus(
            last_successful_reload=time.time(),
            last_attempt=time.time(),
            success=True,
            config_path=self._config_path,
        )

        return config

    def check_for_changes(self) -> bool:
        """Check if the config file has been modified since last load.

        Returns:
            True if the file was modified and reload was attempted.
        """
        if not self._config_path:
            return False

        path = Path(self._config_path)
        if not path.exists():
            return False

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False

        if mtime <= self._last_file_mtime:
            return False

        # File changed — attempt reload
        self._last_file_mtime = mtime
        return self._attempt_reload()

    def validate(self, config_dict: Dict[str, Any]) -> ValidationResult:
        """Validate a configuration dictionary.

        Args:
            config_dict: Raw configuration dictionary to validate.

        Returns:
            ValidationResult with valid flag and errors.
        """
        config = self._dict_to_config(config_dict)
        return self._validator.validate(config)

    def apply_runtime_update(self, updates: Dict[str, Any]) -> ValidationResult:
        """Apply runtime parameter updates (from cascadeguard/configure tool).

        Only applies engine parameters that are safe to update at runtime:
        max_velocity, depth_limit, fanout_limit, preservation_threshold.

        Args:
            updates: Dict of parameter names → new values.

        Returns:
            ValidationResult indicating success or listing errors.
        """
        # Build a new config with the updates applied
        import copy
        new_config = copy.deepcopy(self._current_config)

        allowed_runtime_params = {
            "max_velocity", "depth_limit", "fanout_limit",
            "preservation_threshold", "window_seconds",
        }

        for key, value in updates.items():
            if key not in allowed_runtime_params:
                return ValidationResult(
                    valid=False,
                    errors=[ValidationError(
                        field=key,
                        message=f"Parameter '{key}' cannot be updated at runtime. "
                                f"Allowed: {allowed_runtime_params}",
                        value=value,
                    )],
                )
            setattr(new_config.engine, key, value)

        # Validate the updated config
        result = self._validator.validate(new_config)
        if result.valid:
            self._current_config = new_config
            if self._on_reload:
                self._on_reload(new_config)

        return result

    def get_reload_status(self) -> Dict[str, Any]:
        """Get reload status as a dict (for MCP tool response).

        Returns:
            Dict with last reload timestamp and any errors.
        """
        return {
            "last_successful_reload": self._reload_status.last_successful_reload,
            "last_attempt": self._reload_status.last_attempt,
            "success": self._reload_status.success,
            "errors": self._reload_status.errors,
            "config_path": self._reload_status.config_path,
        }

    def _attempt_reload(self) -> bool:
        """Attempt to reload configuration from file.

        On success: replaces current config, invokes callback.
        On failure: retains previous config, records error.

        Returns:
            True if reload succeeded.
        """
        self._reload_status.last_attempt = time.time()

        try:
            new_config = self._load_from_file(self._config_path)
            if new_config is None:
                self._reload_status.success = False
                self._reload_status.errors = ["Failed to parse config file"]
                return False

            # Apply env overrides
            new_config = self._apply_env_overrides(new_config)

            # Validate
            result = self._validator.validate(new_config)
            if not result.valid:
                error_msgs = [f"{e.field}: {e.message}" for e in result.errors]
                self._reload_status.success = False
                self._reload_status.errors = error_msgs
                logger.warning(
                    f"Config reload validation failed: {error_msgs}. "
                    "Retaining previous configuration."
                )
                return False

            # Success — apply
            self._current_config = new_config
            self._reload_status.success = True
            self._reload_status.errors = []
            self._reload_status.last_successful_reload = time.time()

            if self._on_reload:
                self._on_reload(new_config)

            logger.info("Configuration reloaded successfully")
            return True

        except Exception as e:
            self._reload_status.success = False
            self._reload_status.errors = [str(e)]
            logger.error(f"Config reload failed: {e}. Retaining previous configuration.")
            return False

    def _load_from_file(self, path: str) -> Optional[ProxyConfig]:
        """Load configuration from a JSON or YAML file.

        Args:
            path: Path to the configuration file.

        Returns:
            ProxyConfig if loaded successfully, None otherwise.
        """
        file_path = Path(path)
        if not file_path.exists():
            return None

        try:
            with open(file_path) as f:
                content = f.read()

            # Detect format by extension
            if file_path.suffix in (".yaml", ".yml"):
                try:
                    import yaml
                    data = yaml.safe_load(content)
                except ImportError:
                    logger.warning("YAML support requires pyyaml. Falling back to JSON parse.")
                    data = json.loads(content)
            else:
                data = json.loads(content)

            return self._dict_to_config(data)

        except Exception as e:
            logger.error(f"Failed to load config from {path}: {e}")
            return None

    def _dict_to_config(self, data: Dict[str, Any]) -> ProxyConfig:
        """Convert a raw dict to a ProxyConfig dataclass.

        Args:
            data: Raw configuration dictionary.

        Returns:
            ProxyConfig instance.
        """
        config = ProxyConfig()

        # Transport
        if "transport" in data:
            t = data["transport"]
            config.transport = TransportConfig(
                type=t.get("type", "sse"),
                port=int(t.get("port", 8080)),
                host=t.get("host", "0.0.0.0"),
                tls=TLSConfig(
                    cert_path=t.get("tls", {}).get("cert", ""),
                    key_path=t.get("tls", {}).get("key", ""),
                    ca_path=t.get("tls", {}).get("ca", ""),
                    enabled=bool(t.get("tls", {}).get("cert", "")),
                ),
            )

        # Engine
        if "engine" in data:
            e = data["engine"]
            config.engine = EngineConfig(
                max_velocity=float(e.get("max_velocity", 50.0)),
                depth_limit=int(e.get("depth_limit", 10)),
                fanout_limit=int(e.get("fanout_limit", 20)),
                preservation_threshold=float(e.get("preservation_threshold", 0.3)),
                window_seconds=float(e.get("window_seconds", 60.0)),
                token_budget=e.get("token_budget"),
                dollar_budget=e.get("dollar_budget"),
                cost_per_1k_tokens=float(e.get("cost_per_1k_tokens", 0.03)),
            )

        # Auth
        if "auth" in data:
            a = data["auth"]
            config.auth = AuthConfig(
                mode=a.get("mode", "none"),
                policy_file=a.get("policy_file", ""),
            )

        # Velocity
        if "velocity" in data:
            v = data["velocity"]
            config.velocity = VelocityConfig(
                default_per_agent=v.get("default_per_agent"),
                overrides=v.get("overrides", {}),
            )

        # Targets
        if "targets" in data:
            config.targets = [
                TargetServerConfig(
                    name=t.get("name", ""),
                    transport=t.get("transport", "http"),
                    endpoint=t.get("endpoint", ""),
                    command=t.get("command", []),
                    auth_type=t.get("auth", {}).get("type", "none") if isinstance(t.get("auth"), dict) else "none",
                    auth_token_env=t.get("auth", {}).get("token_env", "") if isinstance(t.get("auth"), dict) else "",
                )
                for t in data["targets"]
            ]

        # Shutdown
        if "shutdown" in data:
            s = data["shutdown"]
            config.shutdown = ShutdownConfig(
                drain_timeout_seconds=int(s.get("drain_timeout_seconds", 30)),
                persist_state=bool(s.get("persist_state", True)),
                state_path=s.get("state_path", "/var/lib/cascadeguard/engine_state.json"),
            )

        return config

    def _apply_env_overrides(self, config: ProxyConfig) -> ProxyConfig:
        """Apply environment variable overrides to config.

        Env var format: CASCADEGUARD_ENGINE_MAX_VELOCITY=100.0

        Args:
            config: Base configuration to override.

        Returns:
            Configuration with env var overrides applied.
        """
        import copy
        config = copy.deepcopy(config)

        # Engine overrides
        env_map = {
            "CASCADEGUARD_ENGINE_MAX_VELOCITY": ("engine", "max_velocity", float),
            "CASCADEGUARD_ENGINE_DEPTH_LIMIT": ("engine", "depth_limit", int),
            "CASCADEGUARD_ENGINE_FANOUT_LIMIT": ("engine", "fanout_limit", int),
            "CASCADEGUARD_ENGINE_PRESERVATION_THRESHOLD": ("engine", "preservation_threshold", float),
            "CASCADEGUARD_ENGINE_WINDOW_SECONDS": ("engine", "window_seconds", float),
            "CASCADEGUARD_ENGINE_TOKEN_BUDGET": ("engine", "token_budget", float),
            "CASCADEGUARD_ENGINE_DOLLAR_BUDGET": ("engine", "dollar_budget", float),
            "CASCADEGUARD_TRANSPORT_PORT": ("transport", "port", int),
            "CASCADEGUARD_TRANSPORT_TYPE": ("transport", "type", str),
            "CASCADEGUARD_AUTH_MODE": ("auth", "mode", str),
            "CASCADEGUARD_SHUTDOWN_DRAIN_TIMEOUT": ("shutdown", "drain_timeout_seconds", int),
        }

        for env_var, (section, field_name, cast_fn) in env_map.items():
            value = os.environ.get(env_var)
            if value is not None:
                try:
                    typed_value = cast_fn(value)
                    section_obj = getattr(config, section)
                    setattr(section_obj, field_name, typed_value)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid env var {env_var}={value}: {e}")

        return config
