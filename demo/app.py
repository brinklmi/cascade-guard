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
import random
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
        dollar_budget=150.0,
        cost_per_1k_tokens=0.03,
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
dollar_budget = st.sidebar.number_input("💰 Monthly Budget ($)", value=150.0, step=25.0, format="%.2f")
cost_per_1k = st.sidebar.number_input("Cost per 1K tokens ($)", value=0.03, step=0.005, format="%.3f")

# Show the implied token budget
implied_tokens = (dollar_budget / cost_per_1k) * 1000 if cost_per_1k > 0 else 0
st.sidebar.caption(f"≈ {implied_tokens:,.0f} tokens at ${cost_per_1k}/1K")

if st.sidebar.button("🔄 Reset Engine"):
    st.session_state.engine = CascadeEngine(
        max_velocity=float(max_velocity),
        depth_limit=depth_limit,
        fanout_limit=fanout_limit,
        preservation_threshold=preservation_threshold,
        dollar_budget=dollar_budget,
        cost_per_1k_tokens=cost_per_1k,
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
st.sidebar.markdown("---")
st.sidebar.markdown("### 💰 Token Budget")
st.sidebar.markdown(f"**Consumed:** {status.total_tokens_consumed:,.0f} tokens")
if status.total_token_budget:
    st.sidebar.progress(status.token_budget_utilization, text=f"{status.token_budget_utilization*100:.1f}% of budget")
    remaining_tokens = status.total_token_budget - status.total_tokens_consumed
    cost_spent = status.total_tokens_consumed * cost_per_1k / 1000
    cost_remaining = remaining_tokens * cost_per_1k / 1000
    st.sidebar.markdown(f"**Spent:** ${cost_spent:.2f} of ${dollar_budget:.2f}")
    st.sidebar.markdown(f"**Remaining:** ${max(0, cost_remaining):.2f}")
else:
    st.sidebar.markdown("**Budget:** Unlimited")


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

    agent_token_budget = st.number_input("Agent Token Budget (0 = unlimited)", value=0, step=10000, format="%d")
    effective_budget = float(agent_token_budget) if agent_token_budget > 0 else None

    if st.button("➕ Register Agent", type="primary"):
        result = engine.register_agent(agent_id, model_id=model_id, parent_id=parent_id, token_budget=effective_budget)
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

        # Token consumption is randomized based on realistic agentic workloads
        agent_complexity = st.selectbox("Task Complexity", [
            "Simple tool call (5K–15K)",
            "Multi-step agent (50K–200K)",
            "Complex multi-agent (200K–1M)",
            "Agentic coding (1M–3.5M)",
        ], key="task_complexity")

        # Map complexity to token ranges
        complexity_ranges = {
            "Simple tool call (5K–15K)": (5000, 15000),
            "Multi-step agent (50K–200K)": (50000, 200000),
            "Complex multi-agent (200K–1M)": (200000, 1000000),
            "Agentic coding (1M–3.5M)": (1000000, 3500000),
        }

        if st.button("⚡ Attempt Delegation", type="secondary"):
            # Randomize token consumption within the selected range
            token_min, token_max = complexity_ranges[agent_complexity]
            tokens_for_delegation = random.randint(token_min, token_max)

            result = engine.attempt_delegation(
                source, target, DelegationAction.DELEGATE,
                tokens_used=float(tokens_for_delegation),
            )
            event = {
                "time": time.time(),
                "action": "delegate",
                "source": source,
                "target": target,
                "allowed": result.allowed,
                "cycle": result.cycle_detected,
                "reason": result.reason,
                "kappa": engine.get_status().kappa_effective,
                "tokens": tokens_for_delegation,
                "cost": result.cost_estimate,
            }
            st.session_state.events.append(event)
            st.session_state.kappa_history.append(engine.get_status().kappa_effective)

            if result.allowed:
                st.success(f"✓ Delegation {source} → {target} | {tokens_for_delegation:,} tokens | κ={engine.get_status().kappa_effective:.3f} | cost=${result.cost_estimate:.2f}")
            else:
                if result.cycle_detected:
                    st.error(f"🔄 CYCLE DETECTED: {source} → {target} would create circular dependency")
                else:
                    st.error(f"✗ BLOCKED: {result.reason}")
            st.rerun()
    else:
        st.info("Register at least 2 agents to attempt delegations.")

    # Token recording section
    st.markdown("---")
    st.subheader("📊 Simulate Task Execution")
    st.markdown("*Tokens consumed per task are randomized based on real agentic workload data.*")
    if agents_list:
        token_agent = st.selectbox("Agent", agents_list, key="token_agent")
        task_type = st.selectbox("Task Type", [
            "Simple tool call (5K–15K)",
            "Multi-step agent (50K–200K)",
            "Complex multi-agent (200K–1M)",
            "Agentic coding (1M–3.5M)",
        ], key="task_type")

        if st.button("🎲 Run Task (random tokens)"):
            task_min, task_max = complexity_ranges[task_type]
            tokens_to_record = random.randint(task_min, task_max)
            result = engine.record_tokens(token_agent, float(tokens_to_record))
            if result.allowed:
                remaining_str = f"{result.token_budget_remaining:,.0f}" if result.token_budget_remaining is not None else "∞"
                st.success(f"✓ Task consumed {tokens_to_record:,} tokens for '{token_agent}' | remaining: {remaining_str}")
            else:
                st.warning(f"⚠️ {token_agent} over budget! Consumed: {result.tokens_consumed:,.0f}")
            st.rerun()
    else:
        st.info("Register agents first.")


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

# Token metrics row
token_col1, token_col2, token_col3, token_col4 = st.columns(4)

with token_col1:
    st.metric("Tokens Consumed", f"{status.total_tokens_consumed:,.0f}")

with token_col2:
    budget_str = f"{status.total_token_budget:,.0f}" if status.total_token_budget else "∞"
    st.metric("Token Budget", budget_str)

with token_col3:
    st.metric("Budget Used", f"{status.token_budget_utilization*100:.1f}%")

with token_col4:
    cost = status.total_tokens_consumed * 0.03 / 1000
    st.metric("Total Cost", f"${cost:.2f}")


# ─── κ History Chart ──────────────────────────────────────────────────────────

if st.session_state.kappa_history:
    st.subheader("📉 κ_effective Over Time")

    # Ensure chart ends at current engine state
    chart_data = st.session_state.kappa_history.copy()
    current_kappa = engine.get_status().kappa_effective
    if chart_data and abs(chart_data[-1] - current_kappa) > 0.01:
        chart_data.append(current_kappa)

    fig = go.Figure()

    # κ line
    fig.add_trace(go.Scatter(
        y=chart_data,
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

# Per-agent token usage table
if engine.num_agents > 0:
    st.markdown("---")
    st.subheader("📋 Per-Agent Token Usage")

    agent_data = []
    for aid, agent in engine._agents.items():
        usage = engine.get_agent_token_usage(aid)
        agent_data.append({
            "Agent": aid,
            "Model": agent.model_id,
            "Tokens Used": f"{usage['tokens_consumed']:,.0f}",
            "Budget": f"{usage['token_budget']:,.0f}" if usage['token_budget'] else "∞",
            "Remaining": f"{usage['token_budget_remaining']:,.0f}" if usage['token_budget_remaining'] is not None else "∞",
            "% Used": f"{usage['budget_ratio']*100:.1f}%" if usage['token_budget'] else "—",
            "Cost": f"${usage['estimated_cost']:.2f}",
            "Status": "🔴 OVER" if usage['over_budget'] else "🟢 OK",
        })

    if agent_data:
        import pandas as pd
        df = pd.DataFrame(agent_data)
        st.dataframe(df, use_container_width=True, hide_index=True)

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
