"""Graceful Shutdown & State Persistence.

SIGTERM → stop accepting → drain in-flight → persist engine state → exit.
Restores delegation graph, budgets, and flow state on restart.

Sequence:
1. SIGTERM received → enter drain state
2. /ready returns 503 → LB stops routing
3. Wait for in-flight requests (configurable timeout)
4. If timeout: return "service_shutting_down" to remaining
5. Persist CascadeEngine state to JSON
6. Exit cleanly
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("cascadeguard.shutdown")


@dataclass
class ShutdownState:
    """Current shutdown/drain state.

    Attributes:
        is_draining: Whether the proxy is in drain mode.
        drain_started_at: Unix timestamp when drain began (0 if not draining).
        drain_timeout: Maximum seconds to wait for in-flight requests.
        in_flight_count: Current number of in-flight requests.
        abandoned_count: Requests abandoned due to timeout.
    """

    is_draining: bool = False
    drain_started_at: float = 0.0
    drain_timeout: float = 30.0
    in_flight_count: int = 0
    abandoned_count: int = 0

    @property
    def drain_elapsed(self) -> float:
        """Seconds elapsed since drain started (0 if not draining)."""
        if not self.is_draining or self.drain_started_at == 0:
            return 0.0
        return time.time() - self.drain_started_at

    @property
    def drain_timeout_reached(self) -> bool:
        """Whether the drain timeout has been exceeded."""
        if not self.is_draining:
            return False
        return self.drain_elapsed >= self.drain_timeout

    @property
    def drain_complete(self) -> bool:
        """Whether drain is complete (no in-flight or timeout reached)."""
        if not self.is_draining:
            return False
        return self.in_flight_count == 0 or self.drain_timeout_reached

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for status reporting."""
        return {
            "is_draining": self.is_draining,
            "drain_started_at": self.drain_started_at,
            "drain_elapsed_seconds": round(self.drain_elapsed, 1),
            "drain_timeout": self.drain_timeout,
            "in_flight_count": self.in_flight_count,
            "abandoned_count": self.abandoned_count,
            "drain_complete": self.drain_complete,
        }

    def track_request_start(self) -> bool:
        """Track a new in-flight request at the state level.

        Returns:
            True if the request is accepted (not draining).
            False if draining (request should be rejected).
        """
        if self.is_draining:
            return False
        self.in_flight_count += 1
        return True


@dataclass
class PersistedEngineState:
    """Serializable snapshot of CascadeEngine state for recovery.

    Contains the minimal state needed to restore budget accounting
    and delegation topology across restarts.
    """

    agents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    total_tokens_consumed: float = 0.0
    total_delegations: int = 0
    cycles_detected: int = 0
    delegations_blocked: int = 0
    flow_state: str = "nominal"
    kappa_effective: float = 1.0
    persisted_at: float = 0.0
    version: str = "1.0"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON storage."""
        return {
            "version": self.version,
            "persisted_at": self.persisted_at,
            "agents": self.agents,
            "total_tokens_consumed": self.total_tokens_consumed,
            "total_delegations": self.total_delegations,
            "cycles_detected": self.cycles_detected,
            "delegations_blocked": self.delegations_blocked,
            "flow_state": self.flow_state,
            "kappa_effective": self.kappa_effective,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersistedEngineState":
        """Reconstruct from stored dict."""
        return cls(
            agents=data.get("agents", {}),
            total_tokens_consumed=float(data.get("total_tokens_consumed", 0.0)),
            total_delegations=int(data.get("total_delegations", 0)),
            cycles_detected=int(data.get("cycles_detected", 0)),
            delegations_blocked=int(data.get("delegations_blocked", 0)),
            flow_state=str(data.get("flow_state", "nominal")),
            kappa_effective=float(data.get("kappa_effective", 1.0)),
            persisted_at=float(data.get("persisted_at", 0.0)),
            version=str(data.get("version", "1.0")),
        )


class ShutdownManager:
    """Manages graceful shutdown and state persistence.

    Handles:
    - Drain state management (stop accepting, wait for in-flight)
    - Timeout handling (abandon remaining after timeout)
    - Engine state persistence to JSON file
    - State restoration on startup

    Thread-safety: Not thread-safe. Use in asyncio single-threaded context.
    """

    def __init__(
        self,
        drain_timeout: float = 30.0,
        state_path: str = "/var/lib/cascadeguard/engine_state.json",
        persist_state: bool = True,
        on_drain_start: Optional[Callable[[], None]] = None,
        on_drain_complete: Optional[Callable[[], None]] = None,
    ):
        """Initialize shutdown manager.

        Args:
            drain_timeout: Max seconds to wait for in-flight requests.
            state_path: File path for persisting engine state.
            persist_state: Whether to persist state on shutdown.
            on_drain_start: Callback when drain starts.
            on_drain_complete: Callback when drain completes.
        """
        self._state = ShutdownState(drain_timeout=drain_timeout)
        self._state_path = state_path
        self._persist_state = persist_state
        self._on_drain_start = on_drain_start
        self._on_drain_complete = on_drain_complete

    @property
    def is_draining(self) -> bool:
        """Whether the proxy is in drain mode."""
        return self._state.is_draining

    @property
    def state(self) -> ShutdownState:
        """Current shutdown state."""
        return self._state

    @property
    def state_path(self) -> str:
        """Path where engine state is persisted."""
        return self._state_path

    def start_drain(self) -> None:
        """Enter drain state — stop accepting new connections.

        Called when SIGTERM is received.
        """
        if self._state.is_draining:
            return  # Already draining

        self._state.is_draining = True
        self._state.drain_started_at = time.time()
        logger.info(
            f"Drain started. Timeout: {self._state.drain_timeout}s. "
            f"In-flight: {self._state.in_flight_count}"
        )

        if self._on_drain_start:
            self._on_drain_start()

    def track_request_start(self) -> bool:
        """Track a new in-flight request.

        Returns:
            True if the request is accepted (not draining).
            False if draining (request should be rejected).
        """
        if self._state.is_draining:
            return False

        self._state.in_flight_count += 1
        return True

    def track_request_end(self) -> None:
        """Track completion of an in-flight request."""
        self._state.in_flight_count = max(0, self._state.in_flight_count - 1)

        # Check if drain is now complete
        if self._state.is_draining and self._state.in_flight_count == 0:
            self._complete_drain()

    def check_drain_timeout(self) -> bool:
        """Check if drain timeout has been reached.

        Returns:
            True if timeout reached and requests were abandoned.
        """
        if not self._state.is_draining:
            return False

        if not self._state.drain_timeout_reached:
            return False

        # Timeout reached — abandon remaining
        if self._state.in_flight_count > 0:
            self._state.abandoned_count = self._state.in_flight_count
            logger.warning(
                f"Drain timeout reached. Abandoning {self._state.in_flight_count} "
                f"in-flight requests."
            )
            self._state.in_flight_count = 0

        self._complete_drain()
        return True

    def persist_engine_state(self, engine_state: PersistedEngineState) -> bool:
        """Persist CascadeEngine state to disk.

        Args:
            engine_state: The engine state to persist.

        Returns:
            True if persistence succeeded.
        """
        if not self._persist_state:
            return False

        engine_state.persisted_at = time.time()

        try:
            path = Path(self._state_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w") as f:
                json.dump(engine_state.to_dict(), f, indent=2)

            logger.info(f"Engine state persisted to {self._state_path}")
            return True

        except (OSError, IOError) as e:
            logger.error(f"Failed to persist engine state: {e}")
            return False

    def load_persisted_state(self) -> Optional[PersistedEngineState]:
        """Load persisted engine state from disk (for startup recovery).

        Returns:
            PersistedEngineState if found and valid, None otherwise.
        """
        path = Path(self._state_path)
        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)

            state = PersistedEngineState.from_dict(data)
            logger.info(
                f"Loaded persisted engine state from {self._state_path} "
                f"(persisted at {state.persisted_at})"
            )
            return state

        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load persisted state: {e}")
            return None

    def clear_persisted_state(self) -> None:
        """Remove persisted state file (after successful restore)."""
        path = Path(self._state_path)
        if path.exists():
            try:
                path.unlink()
                logger.info(f"Cleared persisted state at {self._state_path}")
            except OSError as e:
                logger.warning(f"Failed to clear persisted state: {e}")

    def _complete_drain(self) -> None:
        """Finalize drain completion."""
        logger.info(
            f"Drain complete. Abandoned: {self._state.abandoned_count}. "
            f"Elapsed: {self._state.drain_elapsed:.1f}s"
        )
        if self._on_drain_complete:
            self._on_drain_complete()
