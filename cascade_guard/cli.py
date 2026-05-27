"""CLI demo for CascadeGuard.

Demonstrates:
1. Normal delegation chain (allowed)
2. Cycle detection (blocked)
3. Depth limit enforcement (blocked)
4. Fanout limit enforcement (blocked)
5. Velocity-based impedance (throttled → preservation)
6. Chain tracing and status reporting
"""

from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .engine import CascadeEngine
from .models import DelegationAction


def main() -> int:
    """Run the CascadeGuard demo scenario."""
    console = Console()

    console.print(Panel.fit(
        "[bold cyan]CascadeGuard — Cascade Detection & Prevention[/bold cyan]\n"
        "Union-Find Cycle Detection + Distribution-Based Impedance",
        border_style="cyan",
    ))

    # Initialize with tight limits for demo visibility
    engine = CascadeEngine(
        max_velocity=10.0,
        depth_limit=5,
        fanout_limit=4,
        preservation_threshold=0.3,
        window_seconds=60.0,
    )

    # ─── Scenario 1: Normal Delegation Chain ─────────────────────────────────
    console.print("\n[bold]━━━ Scenario 1: Normal Delegation Chain ━━━[/bold]")

    console.print("\n  Registering orchestrator (root agent)...")
    r = engine.register_agent("orch-1", model_id="gpt-4o", metadata={"role": "orchestrator"})
    _print_verdict(console, r)

    console.print("\n  Orchestrator spawns planner...")
    r = engine.register_agent("planner-1", model_id="claude-3.5", parent_id="orch-1")
    _print_verdict(console, r)

    console.print("\n  Planner spawns executor...")
    r = engine.register_agent("exec-1", model_id="gpt-4o-mini", parent_id="planner-1")
    _print_verdict(console, r)

    console.print("\n  Executor spawns tool-agent...")
    r = engine.register_agent("tool-1", model_id="tool-use-v1", parent_id="exec-1")
    _print_verdict(console, r)

    # Show chain
    chain = engine.get_chain("tool-1")
    console.print(f"\n  [dim]Chain: {' → '.join(chain)}[/dim]")

    # ─── Scenario 2: Cycle Detection ─────────────────────────────────────────
    console.print("\n[bold]━━━ Scenario 2: Cycle Detection ━━━[/bold]")

    console.print("\n  Tool-agent attempts to delegate BACK to orchestrator...")
    r = engine.attempt_delegation("tool-1", "orch-1", DelegationAction.DELEGATE)
    _print_verdict(console, r)

    console.print("\n  Executor attempts to delegate to planner (ancestor)...")
    r = engine.attempt_delegation("exec-1", "planner-1", DelegationAction.DELEGATE)
    _print_verdict(console, r)

    # ─── Scenario 3: Depth Limit ─────────────────────────────────────────────
    console.print("\n[bold]━━━ Scenario 3: Depth Limit Enforcement ━━━[/bold]")

    console.print("\n  Tool-agent spawns sub-tool (depth=4)...")
    r = engine.register_agent("sub-tool-1", model_id="tool-v2", parent_id="tool-1")
    _print_verdict(console, r)

    console.print("\n  Sub-tool spawns sub-sub-tool (depth=5 — at limit)...")
    r = engine.register_agent("sub-sub-tool-1", model_id="tool-v3", parent_id="sub-tool-1")
    _print_verdict(console, r)

    console.print("\n  Sub-sub-tool tries to spawn (depth=6 — EXCEEDS limit)...")
    r = engine.register_agent("too-deep-1", model_id="tool-v4", parent_id="sub-sub-tool-1")
    _print_verdict(console, r)

    # ─── Scenario 4: Fanout Limit ────────────────────────────────────────────
    console.print("\n[bold]━━━ Scenario 4: Fanout Limit Enforcement ━━━[/bold]")

    console.print("\n  Orchestrator spawns workers (fanout limit = 4)...")
    for i in range(2, 5):  # Already has planner-1, so 3 more to hit limit
        r = engine.register_agent(f"worker-{i}", model_id="worker-v1", parent_id="orch-1")
        console.print(f"    worker-{i}: {'✓' if r.allowed else '✗'} (fanout={len(engine._agents['orch-1'].children)})")

    console.print("\n  Orchestrator tries 5th child (EXCEEDS fanout limit)...")
    r = engine.register_agent("worker-5", model_id="worker-v1", parent_id="orch-1")
    _print_verdict(console, r)

    # ─── Scenario 5: Velocity Impedance ──────────────────────────────────────
    console.print("\n[bold]━━━ Scenario 5: Velocity-Based Impedance ━━━[/bold]")

    # Create a fresh engine with very low velocity limit for demo
    engine2 = CascadeEngine(
        max_velocity=5.0,
        depth_limit=20,
        fanout_limit=100,
        preservation_threshold=0.3,
        window_seconds=2.0,  # Short window for demo
    )

    engine2.register_agent("root", model_id="gpt-4o")
    console.print("\n  Rapid-fire delegations (max_velocity=5/sec)...")

    for i in range(1, 12):
        r = engine2.register_agent(f"rapid-{i}", model_id="fast-v1", parent_id="root")
        state_icon = "🟢" if r.flow_state.value == "nominal" else (
            "🟡" if r.flow_state.value == "elevated" else (
                "🟠" if r.flow_state.value == "throttled" else "🔴"
            )
        )
        status = f"{state_icon} {r.flow_state.value}"
        if not r.allowed:
            console.print(f"    rapid-{i}: [red]BLOCKED[/red] | {status} | Z={r.impedance:.3f}")
            break
        else:
            console.print(f"    rapid-{i}: [green]OK[/green] | {status} | Z={r.impedance:.3f} | v={r.velocity:.1f}/s")

    # ─── Final Status ────────────────────────────────────────────────────────
    console.print("\n[bold]━━━ System Status ━━━[/bold]")
    status = engine.get_status()
    _print_status(console, status)

    console.print("\n[bold green]Demo complete.[/bold green]\n")
    return 0


def _print_verdict(console: Console, v) -> None:
    """Print a delegation verdict."""
    if v.allowed:
        console.print(f"    [green]✓ ALLOWED[/green] | {v.reason} | depth={v.depth} | Z={v.impedance:.3f}")
    else:
        icon = "🔄" if v.cycle_detected else "✗"
        console.print(f"    [red]{icon} BLOCKED[/red] | {v.reason}")


def _print_status(console: Console, status) -> None:
    """Print system status as a table."""
    table = Table(title="CascadeGuard System Status")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total Agents", str(status.total_agents))
    table.add_row("Total Delegations", str(status.total_delegations))
    table.add_row("Active Roots", str(status.active_roots))
    table.add_row("Max Depth", str(status.max_depth))
    table.add_row("Max Fan-Out", str(status.max_fan_out))
    table.add_row("Cycles Detected", f"[red]{status.cycles_detected}[/red]")
    table.add_row("Delegations Blocked", f"[yellow]{status.delegations_blocked}[/yellow]")
    table.add_row("Flow State", status.flow_state.value)
    table.add_row("κ_effective", f"{status.kappa_effective:.4f}")
    table.add_row("Impedance (Z)", f"{status.impedance.impedance:.4f}")
    table.add_row("Velocity", f"{status.impedance.velocity:.2f}/s")
    table.add_row("Concentration", f"{status.impedance.concentration:.4f}")

    console.print(table)


if __name__ == "__main__":
    sys.exit(main())
