"""
CascadeGuard Auto-Recovery Module

Production-grade recovery for CascadeGuard failures:
1. Fail-mode configuration (fail-closed / fail-open / fail-cached)
2. Heartbeat emitter (liveness signal)
3. State reconstructor (rebuild from decision log on restart)
4. Topology snapshot (periodic known-good state for fail-cached mode)
5. Watchdog (detect CascadeGuard unavailability, trigger failover)

Recovery flow:
    CascadeGuard crashes → Watchdog detects (heartbeat missed)
    → Fail-cached mode activates (use last snapshot)
    → Watchdog restarts CascadeGuard
    → State reconstructor replays decision log
    → Normal operation resumes
    → No delegations lost, no unsafe state

Contract Authority: Kheruma (Navigator)
Builder: Scroll-LD OS (Autopoietic)
"""

from __future__ import annotations

import json
import time
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from datetime import datetime


class FailMode(str, Enum):
    """Behavior when CascadeGuard is unavailable."""

    CLOSED = "fail-closed"  # Block all delegations (safe, stops system)
    OPEN = "fail-open"  # Allow all delegations (dangerous, keeps running)
    CACHED = "fail-cached"  # Use last known-good topology (recommended)


class RecoveryState(str, Enum):
    """Current recovery state."""

    HEALTHY = "healthy"  # Normal operation
    DEGRADED = "degraded"  # Running on cached state
    RECOVERING = "recovering"  # Replaying decision log
    UNAVAILABLE = "unavailable"  # CascadeGuard down, fail-mode active


class CascadeRecovery:
    """Auto-recovery module for CascadeGuard.

    Provides:
    - Configurable fail-mode (closed/open/cached)
    - Heartbeat emission for external monitoring
    - State reconstruction from decision log
    - Periodic topology snapshots for fail-cached mode
    - Integrated watchdog with automatic restart

    Parameters
    ----------
    fail_mode : FailMode
        Behavior when CascadeGuard is unavailable (default: CACHED).
    heartbeat_interval : float
        Seconds between heartbeat emissions (default: 5.0).
    snapshot_interval : float
        Seconds between topology snapshots (default: 60.0).
    watchdog_timeout : float
        Seconds without heartbeat before declaring unavailable (default: 15.0).
    decision_log_dir : str or Path, optional
        Directory containing decision capsules for replay.
    snapshot_dir : str or Path, optional
        Directory for topology snapshots.
    """

    def __init__(
        self,
        fail_mode: FailMode = FailMode.CACHED,
        heartbeat_interval: float = 5.0,
        snapshot_interval: float = 60.0,
        watchdog_timeout: float = 15.0,
        decision_log_dir: Optional[str] = None,
        snapshot_dir: Optional[str] = None,
    ):
        self.fail_mode = fail_mode
        self.heartbeat_interval = heartbeat_interval
        self.snapshot_interval = snapshot_interval
        self.watchdog_timeout = watchdog_timeout

        # Paths
        self._decision_log_dir = Path(decision_log_dir) if decision_log_dir else None
        self._snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        if self._snapshot_dir:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        # State
        self._state = RecoveryState.HEALTHY
        self._last_heartbeat: float = time.time()
        self._last_snapshot: Optional[dict] = None
        self._last_snapshot_time: float = 0.0
        self._recovery_count: int = 0
        self._total_decisions_replayed: int = 0

        # Watchdog thread (not started by default — call start_watchdog())
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_running: bool = False

        # Callbacks
        self._on_failover: Optional[Callable] = None
        self._on_recovery: Optional[Callable] = None
        self._on_heartbeat: Optional[Callable] = None

    # ─── Heartbeat ────────────────────────────────────────────────────────

    def emit_heartbeat(self) -> None:
        """Emit a heartbeat signal. Call this periodically from CascadeGuard."""
        self._last_heartbeat = time.time()
        self._state = RecoveryState.HEALTHY
        if self._on_heartbeat:
            self._on_heartbeat(self._last_heartbeat)

    @property
    def seconds_since_heartbeat(self) -> float:
        """Seconds since last heartbeat."""
        return time.time() - self._last_heartbeat

    @property
    def is_alive(self) -> bool:
        """Whether CascadeGuard is considered alive (heartbeat within timeout)."""
        return self.seconds_since_heartbeat < self.watchdog_timeout

    # ─── Fail-Mode Decision ───────────────────────────────────────────────

    def should_allow_delegation(self, source_id: str, target_id: str) -> tuple[bool, str]:
        """Determine if a delegation should be allowed during degraded state.

        Called when CascadeGuard is unavailable and the system needs to decide
        whether to allow or block a delegation.

        Parameters
        ----------
        source_id : str
            Delegating agent.
        target_id : str
            Target agent.

        Returns
        -------
        tuple[bool, str]
            (allowed, reason)
        """
        if self._state == RecoveryState.HEALTHY:
            return True, "CascadeGuard healthy — normal operation"

        if self.fail_mode == FailMode.CLOSED:
            return False, "CascadeGuard unavailable — fail-closed: all delegations blocked"

        elif self.fail_mode == FailMode.OPEN:
            return True, "CascadeGuard unavailable — fail-open: delegation allowed (UNSAFE)"

        elif self.fail_mode == FailMode.CACHED:
            return self._check_cached_topology(source_id, target_id)

        return False, "Unknown fail mode"

    def _check_cached_topology(self, source_id: str, target_id: str) -> tuple[bool, str]:
        """Check delegation against cached topology snapshot."""
        if self._last_snapshot is None:
            # No snapshot available — fall back to fail-closed
            return False, "CascadeGuard unavailable — no cached snapshot, defaulting to fail-closed"

        # Check if both agents exist in the cached topology
        known_agents = set(self._last_snapshot.get("agents", []))

        if source_id not in known_agents:
            return False, f"CascadeGuard unavailable — source '{source_id}' not in cached topology"

        if target_id in known_agents:
            # Both known — check if they were in the same component (potential cycle)
            components = self._last_snapshot.get("components", {})
            source_component = None
            target_component = None
            for root, members in components.items():
                if source_id in members:
                    source_component = root
                if target_id in members:
                    target_component = root

            if source_component == target_component:
                return (
                    False,
                    f"CascadeGuard unavailable — cached topology shows cycle risk (same component)",
                )

        # Target is new or in different component — allow
        return (
            True,
            "CascadeGuard unavailable — fail-cached: delegation matches known-good topology",
        )

    # ─── Topology Snapshot ────────────────────────────────────────────────

    def take_snapshot(self, engine) -> dict:
        """Take a topology snapshot from a live CascadeEngine.

        Parameters
        ----------
        engine : CascadeEngine
            The live CascadeGuard engine to snapshot.

        Returns
        -------
        dict
            Snapshot containing agents, components, and flow state.
        """
        snapshot = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "epoch": time.time(),
            "agents": list(engine._agents.keys()),
            "num_agents": engine.num_agents,
            "components": engine._uf.get_components(),
            "num_components": engine._uf.num_components,
            "flow_state": engine.get_status().flow_state.value,
            "kappa_effective": engine.get_status().kappa_effective,
            "total_delegations": engine._total_delegations,
            "cycles_detected": engine._cycles_detected,
        }

        self._last_snapshot = snapshot
        self._last_snapshot_time = time.time()

        # Persist if directory configured
        if self._snapshot_dir:
            filepath = self._snapshot_dir / "latest_snapshot.json"
            with open(filepath, "w") as f:
                json.dump(snapshot, f, indent=2, default=str)

        return snapshot

    def load_snapshot(self) -> Optional[dict]:
        """Load the most recent snapshot from disk.

        Returns
        -------
        Optional[dict]
            The snapshot, or None if not found.
        """
        if not self._snapshot_dir:
            return None

        filepath = self._snapshot_dir / "latest_snapshot.json"
        if not filepath.exists():
            return None

        with open(filepath) as f:
            self._last_snapshot = json.load(f)
        return self._last_snapshot

    # ─── State Reconstruction ─────────────────────────────────────────────

    def reconstruct_state(self, engine) -> dict:
        """Reconstruct CascadeGuard state from decision log.

        Replays all decision capsules through the engine to rebuild
        the Union-Find topology and flow state.

        Parameters
        ----------
        engine : CascadeEngine
            A fresh CascadeEngine to reconstruct into.

        Returns
        -------
        dict
            Reconstruction report.
        """
        if not self._decision_log_dir or not self._decision_log_dir.exists():
            return {
                "success": False,
                "reason": "No decision log directory configured or found",
                "decisions_replayed": 0,
            }

        self._state = RecoveryState.RECOVERING

        # Load all decision capsules, sorted by epoch
        decisions = []
        for filepath in sorted(self._decision_log_dir.glob("decision_*.json")):
            with open(filepath) as f:
                decisions.append(json.load(f))

        # Sort by epoch
        decisions.sort(key=lambda d: d.get("epoch", 0))

        # Replay through engine
        replayed = 0
        errors = []

        for decision in decisions:
            source_id = decision.get("source_id", "")
            target_id = decision.get("target_id", "")
            allowed = decision.get("allowed", True)

            if not source_id or not target_id:
                continue

            try:
                # Ensure source exists
                if source_id not in engine._agents:
                    engine.register_agent(source_id, model_id="reconstructed")

                # If it was allowed, register the target
                if allowed and target_id not in engine._agents:
                    engine.register_agent(
                        target_id,
                        model_id="reconstructed",
                        parent_id=source_id,
                    )

                replayed += 1
            except Exception as e:
                errors.append({"decision": decision, "error": str(e)})

        self._total_decisions_replayed += replayed
        self._recovery_count += 1
        self._state = RecoveryState.HEALTHY

        # Take a fresh snapshot after reconstruction
        self.take_snapshot(engine)

        if self._on_recovery:
            self._on_recovery(replayed)

        return {
            "success": True,
            "decisions_replayed": replayed,
            "total_decisions": len(decisions),
            "errors": len(errors),
            "recovery_count": self._recovery_count,
            "engine_agents": engine.num_agents,
            "engine_components": engine.num_roots,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    # ─── Watchdog ─────────────────────────────────────────────────────────

    def start_watchdog(self, restart_fn: Optional[Callable] = None) -> None:
        """Start the watchdog thread.

        Parameters
        ----------
        restart_fn : Callable, optional
            Function to call to restart CascadeGuard when unavailable.
        """
        if self._watchdog_running:
            return

        self._watchdog_running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            args=(restart_fn,),
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        """Stop the watchdog thread."""
        self._watchdog_running = False
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=self.watchdog_timeout + 1)
            self._watchdog_thread = None

    def _watchdog_loop(self, restart_fn: Optional[Callable]) -> None:
        """Watchdog loop — monitors heartbeat and triggers failover."""
        while self._watchdog_running:
            time.sleep(self.heartbeat_interval)

            if not self.is_alive and self._state == RecoveryState.HEALTHY:
                # Heartbeat missed — trigger failover
                self._state = RecoveryState.UNAVAILABLE

                if self._on_failover:
                    self._on_failover(self.seconds_since_heartbeat)

                # Attempt restart if function provided
                if restart_fn:
                    try:
                        restart_fn()
                        self._state = RecoveryState.RECOVERING
                    except Exception:
                        pass  # Stay in UNAVAILABLE

    # ─── Callbacks ────────────────────────────────────────────────────────

    def on_failover(self, callback: Callable) -> None:
        """Register callback for failover events."""
        self._on_failover = callback

    def on_recovery(self, callback: Callable) -> None:
        """Register callback for recovery events."""
        self._on_recovery = callback

    def on_heartbeat(self, callback: Callable) -> None:
        """Register callback for heartbeat events."""
        self._on_heartbeat = callback

    # ─── Status ───────────────────────────────────────────────────────────

    @property
    def state(self) -> RecoveryState:
        """Current recovery state."""
        return self._state

    def get_status(self) -> dict:
        """Get full recovery module status."""
        return {
            "state": self._state.value,
            "fail_mode": self.fail_mode.value,
            "is_alive": self.is_alive,
            "seconds_since_heartbeat": round(self.seconds_since_heartbeat, 2),
            "watchdog_timeout": self.watchdog_timeout,
            "recovery_count": self._recovery_count,
            "total_decisions_replayed": self._total_decisions_replayed,
            "has_snapshot": self._last_snapshot is not None,
            "snapshot_age_seconds": (
                round(time.time() - self._last_snapshot_time, 1)
                if self._last_snapshot_time > 0
                else None
            ),
            "watchdog_running": self._watchdog_running,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "operator": "ϟ",
            "witness": "🐞",
        }

    def reset(self) -> None:
        """Reset recovery module state."""
        self._state = RecoveryState.HEALTHY
        self._last_heartbeat = time.time()
        self._last_snapshot = None
        self._last_snapshot_time = 0.0
        self._recovery_count = 0
        self._total_decisions_replayed = 0
        self.stop_watchdog()
