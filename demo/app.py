"""
CascadeGuard Interactive Demo — Streamlit Visual Dashboard

Run: streamlit run demo/app.py

Demonstrates:
- Real-time cycle detection (Union-Find)
- κ_effective impedance monitoring
- Flow state transitions (Green → Yellow → Red)
- Circuit breaker behavior
- Delegation graph visualization
"""

import sys
import time
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px

# Add cascade_guard to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from cascade_guard.engine import CascadeEngine
from cascade_guard.models import DelegationAction, FlowState


# ─── Page Config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="CascadeGuard Demo",
    page_icon="🛡️",
    layout="wide",
)

st.title("🛡️ CascadeGuard — Real-Time Cascade Prevention")
st.markdown("**O(α(N)) cycle detection + distribution-based impedance control**")
st.markdown("---")


# ─── Session State ────────────────────────────────────────────────────────────

if "engine" not in st.session_state:
    st.session_state.engine = CascadeEngine(
        max_velocity=20.0,
        depth_limit=6,
        fanout_limit=5,
        preservation_threshold=0.3,
    )
    st.session_state.history = []
    st.session_state.kappa_history = []
    st.session_state.events = []

engine = st.session_state.engine


# ─── Sidebar Controls ─────────────────────────────────────────────────────────

st.sidebar.header("⚙️ Configuration")
st.sidebar.markdown("Adjust CascadeGuard parameters:")

max_velocity = st.sidebar.slider("Max Velocity (delegations/sec)", 5, 100, 20)
depth_limit = st.sidebar.slider("Depth Limit", 3, 20, 6)
fanout_limit = st.sidebar.slider("Fanout Limit", 2, 20, 5)
preservation_threshold = st.sidebar.slider("Preservation Threshold (κ)", 0.1, 0.5, 0.3)

if st.sidebar.button("🔄 Reset Engine"):
    st.session_state.engine = CascadeEngine(
        max_velocity=float(max_velocity),
        depth_limit=depth_limit,
        fanout_limit=fanout_limit,
        preservation_threshold=preservation_threshold,
    )
    st.session_state.history = []
    st.session_state.kappa_history = []
    st.session_state.events = []
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.markdown("### 📊 Current Status")
status = engine.get_status()
flow_state = status.flow_state.value

state_colors = {
    "nominal": "🟢",
    "elevated": "🟡",
    "throttled": "🟠",
    "preservation": "🔴",
}
st.sidebar.markdown(f"**Flow State:** {state_colors.get(flow_state, '⚪')} {flow_state.upper()}")
st.sidebar.markdown(f"**κ_effective:** {status.kappa_effective:.3f}")
st.sidebar.markdown(f"**Agents:** {status.total_agents}")
st.sidebar.markdown(f"**Delegations:** {status.total_delegations}")
st.sidebar.markdown(f"**Cycles Detected:** {status.cycles_detected}")
st.sidebar.markdown(f"**Blocked:** {status.delegations_blocked}")


# ─── Main Content ─────────────────────────────────────────────────────────────

col1, col2 = st.columns(2)

with col1:
    st.subheader("🔗 Register Agent")
    agent_id = st.text_input("Agent ID", value=f"agent-{engine.num_agents + 1}")
    model_id = st.selectbox("Model", ["gpt-4o", "claude-3.5", "gemini-2", "llama-3", "tool-agent"])

    agents_list = list(engine._agents.keys())
    parent_options = ["(Root — no parent)"] + agents_list
    parent_selection = st.selectbox("Parent Agent", parent_options)
    parent_id = None if parent_selection == "(Root — no parent)" else parent_selection

    if st.button("➕ Register Agent", type="primary"):
        result = engine.register_agent(agent_id, model_id=model_id, parent_id=parent_id)
        event = {
            "time": time.time(),
            "action": "register",
            "agent": agent_id,
            "parent": parent_id,
            "allowed": result.allowed,
            "reason": result.reason,
            "kappa": engine.get_status().kappa_effective,
        }
        st.session_state.events.append(event)
        st.session_state.kappa_history.append(engine.get_status().kappa_effective)

        if result.allowed:
            st.success(f"✓ Agent '{agent_id}' registered | depth={result.depth} | κ={engine.get_status().kappa_effective:.3f}")
        else:
            st.error(f"✗ BLOCKED: {result.reason}")
        st.rerun()

with col2:
    st.subheader("🔄 Attempt Delegation")
    if len(agents_list) >= 2:
        source = st.selectbox("Source Agent", agents_list, key="del_source")
        target = st.selectbox("Target Agent", agents_list, key="del_target")

        if st.button("⚡ Attempt Delegation", type="secondary"):
            result = engine.attempt_delegation(source, target, DelegationAction.DELEGATE)
            event = {
                "time": time.time(),
                "action": "delegate",
                "source": source,
                "target": target,
                "allowed": result.allowed,
                "cycle": result.cycle_detected,
                "reason": result.reason,
                "kappa": engine.get_status().kappa_effective,
            }
            st.session_state.events.append(event)
            st.session_state.kappa_history.append(engine.get_status().kappa_effective)

            if result.allowed:
                st.success(f"✓ Delegation {source} → {target} allowed | κ={engine.get_status().kappa_effective:.3f}")
            else:
                if result.cycle_detected:
                    st.error(f"🔄 CYCLE DETECTED: {source} → {target} would create circular dependency")
                else:
                    st.error(f"✗ BLOCKED: {result.reason}")
            st.rerun()
    else:
        st.info("Register at least 2 agents to attempt delegations.")


# ─── Metrics Dashboard ────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("📈 Real-Time Metrics")

metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)

with metric_col1:
    st.metric("κ_effective", f"{status.kappa_effective:.3f}",
              delta=f"{status.kappa_effective - 1.0:.3f}" if status.kappa_effective < 1.0 else None)

with metric_col2:
    st.metric("Flow State", flow_state.upper())

with metric_col3:
    st.metric("Cycles Detected", status.cycles_detected)

with metric_col4:
    st.metric("Delegations Blocked", status.delegations_blocked)


# ─── κ History Chart ──────────────────────────────────────────────────────────

if st.session_state.kappa_history:
    st.subheader("📉 κ_effective Over Time")

    fig = go.Figure()

    # κ line
    fig.add_trace(go.Scatter(
        y=st.session_state.kappa_history,
        mode='lines+markers',
        name='κ_effective',
        line=dict(color='#3498db', width=3),
        marker=dict(size=6),
    ))

    # Threshold zones
    fig.add_hline(y=0.7, line_dash="dash", line_color="green",
                  annotation_text="Green (κ ≥ 0.7)")
    fig.add_hline(y=0.3, line_dash="dash", line_color="red",
                  annotation_text="Red (κ < 0.3)")

    # Shaded zones
    fig.add_hrect(y0=0.7, y1=1.0, fillcolor="green", opacity=0.05)
    fig.add_hrect(y0=0.3, y1=0.7, fillcolor="yellow", opacity=0.05)
    fig.add_hrect(y0=0.0, y1=0.3, fillcolor="red", opacity=0.05)

    fig.update_layout(
        yaxis_range=[0, 1.05],
        yaxis_title="κ_effective",
        xaxis_title="Delegation Event",
        height=300,
        margin=dict(l=40, r=40, t=20, b=40),
    )

    st.plotly_chart(fig, use_container_width=True)


# ─── Event Log ────────────────────────────────────────────────────────────────

if st.session_state.events:
    st.subheader("📋 Decision Log")

    for event in reversed(st.session_state.events[-20:]):
        if event["allowed"]:
            icon = "✅"
        elif event.get("cycle"):
            icon = "🔄"
        else:
            icon = "🚫"

        if event["action"] == "register":
            st.markdown(f"{icon} **Register** `{event['agent']}` (parent: `{event.get('parent', 'root')}`) — {event['reason']}")
        else:
            st.markdown(f"{icon} **Delegate** `{event.get('source')}` → `{event.get('target')}` — {event['reason']}")


# ─── Delegation Graph ─────────────────────────────────────────────────────────

if engine.num_agents > 0:
    st.markdown("---")
    st.subheader("🌐 Delegation Graph")

    # Build graph data
    nodes = list(engine._agents.keys())
    edges = []
    for agent_id, agent in engine._agents.items():
        if agent.parent_id and agent.parent_id in engine._agents:
            edges.append((agent.parent_id, agent_id))

    if nodes:
        # Simple force-directed layout approximation
        import math
        n = len(nodes)
        node_x = [math.cos(2 * math.pi * i / max(n, 1)) for i in range(n)]
        node_y = [math.sin(2 * math.pi * i / max(n, 1)) for i in range(n)]
        node_map = {name: i for i, name in enumerate(nodes)}

        fig_graph = go.Figure()

        # Edges
        for src, tgt in edges:
            if src in node_map and tgt in node_map:
                x0, y0 = node_x[node_map[src]], node_y[node_map[src]]
                x1, y1 = node_x[node_map[tgt]], node_y[node_map[tgt]]
                fig_graph.add_trace(go.Scatter(
                    x=[x0, x1, None], y=[y0, y1, None],
                    mode='lines',
                    line=dict(width=2, color='#888'),
                    hoverinfo='none',
                    showlegend=False,
                ))

        # Nodes
        depths = [engine._agents[n].depth for n in nodes]
        fig_graph.add_trace(go.Scatter(
            x=node_x, y=node_y,
            mode='markers+text',
            marker=dict(size=20, color=depths, colorscale='Viridis', showscale=True,
                        colorbar=dict(title="Depth")),
            text=nodes,
            textposition="top center",
            hoverinfo='text',
            hovertext=[f"{n} (depth={engine._agents[n].depth}, model={engine._agents[n].model_id})" for n in nodes],
        ))

        fig_graph.update_layout(
            showlegend=False,
            height=400,
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        )

        st.plotly_chart(fig_graph, use_container_width=True)


# ─── Cost Calculator ──────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("💰 Recursive Cascade Cost Calculator")
st.markdown("See how runaway costs compound — and where CascadeGuard stops the bleeding.")

calc_col1, calc_col2 = st.columns(2)

with calc_col1:
    token_rate = st.number_input("Token cost ($/1K tokens)", value=0.03, step=0.005, format="%.3f")
    tokens_per_iteration = st.number_input("Tokens per iteration", value=5000, step=1000)
    concurrent_agents = st.slider("Concurrent agents in loop", 1, 20, 5)
    fuse_iteration = st.slider("CascadeGuard trips at iteration", 3, 50, 20)

with calc_col2:
    max_iterations = 500
    iterations = list(range(1, max_iterations + 1))
    costs = [(i * tokens_per_iteration * token_rate / 1000) * concurrent_agents for i in iterations]

    # Cost with CascadeGuard (capped at fuse_iteration)
    costs_guarded = [(min(i, fuse_iteration) * tokens_per_iteration * token_rate / 1000) * concurrent_agents for i in iterations]

    fig_cost = go.Figure()

    fig_cost.add_trace(go.Scatter(
        x=iterations, y=costs,
        mode='lines', name='Without CascadeGuard',
        line=dict(color='#e63946', width=3),
    ))

    fig_cost.add_trace(go.Scatter(
        x=iterations, y=costs_guarded,
        mode='lines', name='With CascadeGuard',
        line=dict(color='#2ecc71', width=3),
    ))

    fig_cost.add_vline(x=fuse_iteration, line_dash="dash", line_color="orange",
                       annotation_text=f"💥 Fuse trips (iteration {fuse_iteration})")

    saved = costs[-1] - costs_guarded[-1]
    fig_cost.update_layout(
        title=f"Saved: ${saved:,.2f} per runaway event",
        yaxis_title="Cumulative Cost ($)",
        xaxis_title="Recursion Depth (iterations)",
        height=350,
        margin=dict(l=40, r=40, t=60, b=40),
    )

    st.plotly_chart(fig_cost, use_container_width=True)

st.markdown(f"""
**At your settings:** {concurrent_agents} agents × {tokens_per_iteration:,} tokens/iteration × ${token_rate}/1K tokens

| Without CascadeGuard (500 iterations) | With CascadeGuard (trips at {fuse_iteration}) | **Saved** |
|---|---|---|
| **${costs[-1]:,.2f}** | **${costs_guarded[-1]:,.2f}** | **${saved:,.2f}** |
""")


# ─── Footer ───────────────────────────────────────────────────────────────────

st.markdown("---")
st.markdown(
    "**CascadeGuard** — Real-time cascade prevention for multi-agent systems. "
    "[GitHub](https://github.com/brinklmi/cascade-guard) | "
    "[Theory](https://github.com/brinklmi/cascade-guard/blob/main/THEORY.md) | "
    "[Proofs](https://github.com/brinklmi/cascade-guard/blob/main/docs/PROOFS.md)"
)
