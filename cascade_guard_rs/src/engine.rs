//! CascadeEngine — Union-Find cycle detection + FlowMonitor impedance.
//!
//! Target: <100μs per safety check (attempt_delegation).
//! Uses path compression with union by rank for O(α(N)) amortized.

use std::collections::{HashMap, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::models::*;

/// Union-Find (Disjoint Set) with path compression and union by rank.
/// Provides O(α(N)) cycle detection — effectively constant time.
struct UnionFind {
    parent: HashMap<String, String>,
    rank: HashMap<String, u32>,
}

impl UnionFind {
    fn new() -> Self {
        Self {
            parent: HashMap::new(),
            rank: HashMap::new(),
        }
    }

    fn make_set(&mut self, id: &str) {
        if !self.parent.contains_key(id) {
            self.parent.insert(id.to_string(), id.to_string());
            self.rank.insert(id.to_string(), 0);
        }
    }

    fn find(&mut self, id: &str) -> String {
        let parent = match self.parent.get(id) {
            Some(p) => p.clone(),
            None => return id.to_string(),
        };

        if parent != id {
            // Path compression
            let root = self.find(&parent);
            self.parent.insert(id.to_string(), root.clone());
            root
        } else {
            id.to_string()
        }
    }

    fn connected(&mut self, a: &str, b: &str) -> bool {
        self.find(a) == self.find(b)
    }

    fn union(&mut self, a: &str, b: &str) -> bool {
        let root_a = self.find(a);
        let root_b = self.find(b);

        if root_a == root_b {
            return false; // Already in same set — would create cycle
        }

        // Union by rank
        let rank_a = *self.rank.get(&root_a).unwrap_or(&0);
        let rank_b = *self.rank.get(&root_b).unwrap_or(&0);

        if rank_a < rank_b {
            self.parent.insert(root_a, root_b);
        } else if rank_a > rank_b {
            self.parent.insert(root_b, root_a);
        } else {
            self.parent.insert(root_b, root_a.clone());
            self.rank.insert(root_a, rank_a + 1);
        }

        true
    }

    fn num_nodes(&self) -> usize {
        self.parent.len()
    }
}

/// Flow monitor — tracks velocity, computes impedance.
struct FlowMonitor {
    max_velocity: f64,
    depth_limit: u32,
    fanout_limit: u32,
    window_seconds: f64,
    preservation_threshold: f64,
    token_budget: Option<f64>,
    cost_per_1k_tokens: f64,

    // Rolling window of delegation timestamps
    timestamps: VecDeque<f64>,

    // Depth tracking
    max_depth: u32,

    // Token tracking
    total_tokens_consumed: f64,

    // Delegation counts per source (for concentration)
    source_counts: HashMap<String, u32>,

    // Children count per parent (for fanout)
    children_count: HashMap<String, u32>,
}

impl FlowMonitor {
    fn new(config: &EngineConfig) -> Self {
        Self {
            max_velocity: config.max_velocity,
            depth_limit: config.depth_limit,
            fanout_limit: config.fanout_limit,
            window_seconds: config.window_seconds,
            preservation_threshold: config.preservation_threshold,
            token_budget: config.token_budget,
            cost_per_1k_tokens: config.cost_per_1k_tokens,
            timestamps: VecDeque::new(),
            max_depth: 0,
            total_tokens_consumed: 0.0,
            source_counts: HashMap::new(),
            children_count: HashMap::new(),
        }
    }

    fn now_secs() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64()
    }

    fn prune_window(&mut self) {
        let cutoff = Self::now_secs() - self.window_seconds;
        while let Some(&front) = self.timestamps.front() {
            if front < cutoff {
                self.timestamps.pop_front();
            } else {
                break;
            }
        }
    }

    fn velocity(&mut self) -> f64 {
        self.prune_window();
        if self.timestamps.is_empty() {
            return 0.0;
        }
        let elapsed = Self::now_secs() - self.timestamps.front().unwrap_or(&0.0);
        if elapsed < 0.001 {
            return self.timestamps.len() as f64;
        }
        self.timestamps.len() as f64 / elapsed
    }

    fn record_delegation(&mut self, source_id: &str, depth: u32) {
        self.timestamps.push_back(Self::now_secs());
        if depth > self.max_depth {
            self.max_depth = depth;
        }
        *self.source_counts.entry(source_id.to_string()).or_insert(0) += 1;
        *self.children_count.entry(source_id.to_string()).or_insert(0) += 1;
    }

    fn record_tokens(&mut self, tokens: f64) {
        self.total_tokens_consumed += tokens;
    }

    fn token_pressure(&self) -> f64 {
        match self.token_budget {
            Some(budget) if budget > 0.0 => {
                (self.total_tokens_consumed / budget).min(1.0)
            }
            _ => 0.0,
        }
    }

    fn compute_impedance(&mut self) -> ImpedanceReport {
        let velocity = self.velocity();

        // Velocity component (0 to 1)
        let v_norm = (velocity / self.max_velocity).min(1.0);

        // Depth component
        let d_norm = (self.max_depth as f64 / self.depth_limit as f64).min(1.0);

        // Fanout component
        let max_fanout = self.children_count.values().max().copied().unwrap_or(0);
        let f_norm = (max_fanout as f64 / self.fanout_limit as f64).min(1.0);

        // Token pressure
        let t_norm = self.token_pressure();

        // Weighted impedance (matches Python: 0.35, 0.15, 0.15, 0.15, 0.20)
        let impedance = 0.35 * v_norm + 0.15 * d_norm + 0.15 * f_norm + 0.15 * 0.0 + 0.20 * t_norm;

        let flow_state = self.compute_flow_state(impedance);

        ImpedanceReport {
            impedance,
            velocity,
            mean_depth: self.max_depth as f64, // simplified
            max_depth: self.max_depth,
            fan_out: max_fanout as f64,
            max_fan_out: max_fanout,
            concentration: 0.0,
            token_pressure: t_norm,
            flow_state,
        }
    }

    fn compute_flow_state(&self, impedance: f64) -> FlowState {
        let kappa = 1.0 - impedance;
        if kappa < self.preservation_threshold {
            FlowState::Preservation
        } else if kappa < 0.5 {
            FlowState::Throttled
        } else if kappa < 0.8 {
            FlowState::Elevated
        } else {
            FlowState::Nominal
        }
    }

    fn flow_state(&mut self) -> FlowState {
        let report = self.compute_impedance();
        report.flow_state
    }
}

/// The core CascadeGuard engine — Rust implementation.
///
/// Provides identical safety guarantees to the Python implementation:
/// - O(α(N)) cycle detection via Union-Find with path compression
/// - Distribution-based impedance computation
/// - Per-agent and global budget enforcement
/// - Velocity-based flow state management
pub struct CascadeEngine {
    uf: UnionFind,
    flow: FlowMonitor,
    agents: HashMap<String, AgentNode>,
    config: EngineConfig,

    // Counters
    total_delegations: u64,
    cycles_detected: u64,
    delegations_blocked: u64,
}

impl CascadeEngine {
    /// Create a new CascadeEngine with the given configuration.
    pub fn new(config: EngineConfig) -> Self {
        let flow = FlowMonitor::new(&config);
        Self {
            uf: UnionFind::new(),
            flow,
            agents: HashMap::new(),
            config,
            total_delegations: 0,
            cycles_detected: 0,
            delegations_blocked: 0,
        }
    }

    /// Create with default configuration.
    pub fn default() -> Self {
        Self::new(EngineConfig::default())
    }

    /// Register a new root agent.
    pub fn register_agent(
        &mut self,
        agent_id: &str,
        model_id: &str,
        token_budget: Option<f64>,
    ) -> DelegationVerdict {
        if self.agents.contains_key(agent_id) {
            return DelegationVerdict {
                allowed: false,
                source_id: String::new(),
                target_id: Some(agent_id.to_string()),
                reason: format!("Agent '{}' already registered", agent_id),
                flow_state: self.flow.flow_state(),
                ..Default::default()
            };
        }

        self.uf.make_set(agent_id);
        let mut agent = AgentNode::new(
            agent_id.to_string(),
            model_id.to_string(),
            None,
            0,
        );
        agent.token_budget = token_budget;
        self.agents.insert(agent_id.to_string(), agent);

        DelegationVerdict {
            allowed: true,
            source_id: String::new(),
            target_id: Some(agent_id.to_string()),
            reason: "Root agent registered".to_string(),
            flow_state: self.flow.flow_state(),
            ..Default::default()
        }
    }

    /// Attempt a delegation from source to target.
    /// This is the primary safety check — target <100μs.
    pub fn attempt_delegation(
        &mut self,
        source_id: &str,
        target_id: &str,
        tokens_used: f64,
    ) -> DelegationVerdict {
        self.total_delegations += 1;

        // Ensure both nodes exist in Union-Find
        self.uf.make_set(source_id);
        self.uf.make_set(target_id);

        // 1. Cycle detection — O(α(N))
        if self.uf.connected(source_id, target_id) {
            self.cycles_detected += 1;
            self.delegations_blocked += 1;
            return DelegationVerdict {
                allowed: false,
                source_id: source_id.to_string(),
                target_id: Some(target_id.to_string()),
                reason: "cycle_detected".to_string(),
                cycle_detected: true,
                flow_state: self.flow.flow_state(),
                impedance: self.flow.compute_impedance().impedance,
                ..Default::default()
            };
        }

        // 2. Flow impedance check
        let report = self.flow.compute_impedance();
        if report.flow_state == FlowState::Preservation {
            self.delegations_blocked += 1;
            return DelegationVerdict {
                allowed: false,
                source_id: source_id.to_string(),
                target_id: Some(target_id.to_string()),
                reason: "preservation_mode".to_string(),
                impedance: report.impedance,
                flow_state: report.flow_state,
                velocity: report.velocity,
                ..Default::default()
            };
        }

        // 3. Depth limit check
        let source_depth = self.agents.get(source_id).map(|a| a.depth).unwrap_or(0);
        let new_depth = source_depth + 1;
        if new_depth > self.config.depth_limit {
            self.delegations_blocked += 1;
            return DelegationVerdict {
                allowed: false,
                source_id: source_id.to_string(),
                target_id: Some(target_id.to_string()),
                reason: format!("Depth limit exceeded: {} > {}", new_depth, self.config.depth_limit),
                depth: new_depth,
                flow_state: report.flow_state,
                impedance: report.impedance,
                ..Default::default()
            };
        }

        // 4. Fanout limit check
        let fanout = self.agents.get(source_id)
            .map(|a| a.children.len() as u32)
            .unwrap_or(0);
        if fanout >= self.config.fanout_limit {
            self.delegations_blocked += 1;
            return DelegationVerdict {
                allowed: false,
                source_id: source_id.to_string(),
                target_id: Some(target_id.to_string()),
                reason: format!("Fanout limit exceeded: {} >= {}", fanout, self.config.fanout_limit),
                flow_state: report.flow_state,
                impedance: report.impedance,
                ..Default::default()
            };
        }

        // 5. Per-agent budget check
        if let Some(agent) = self.agents.get(source_id) {
            if agent.is_over_budget() {
                self.delegations_blocked += 1;
                return DelegationVerdict {
                    allowed: false,
                    source_id: source_id.to_string(),
                    target_id: Some(target_id.to_string()),
                    reason: format!(
                        "Agent budget exceeded: {:.0} > {:.0}",
                        agent.tokens_consumed,
                        agent.token_budget.unwrap_or(0.0)
                    ),
                    tokens_consumed: agent.tokens_consumed,
                    token_budget_remaining: agent.budget_remaining(),
                    flow_state: report.flow_state,
                    impedance: report.impedance,
                    ..Default::default()
                };
            }
        }

        // 6. Global budget check
        if let Some(budget) = self.config.token_budget {
            if self.flow.total_tokens_consumed >= budget {
                self.delegations_blocked += 1;
                return DelegationVerdict {
                    allowed: false,
                    source_id: source_id.to_string(),
                    target_id: Some(target_id.to_string()),
                    reason: format!(
                        "System budget exhausted: {:.0} / {:.0}",
                        self.flow.total_tokens_consumed, budget
                    ),
                    flow_state: report.flow_state,
                    impedance: report.impedance,
                    ..Default::default()
                };
            }
        }

        // All checks passed — execute delegation
        self.uf.union(source_id, target_id);
        self.flow.record_delegation(source_id, new_depth);

        // Record tokens
        if tokens_used > 0.0 {
            self.flow.record_tokens(tokens_used);
            if let Some(agent) = self.agents.get_mut(source_id) {
                agent.tokens_consumed += tokens_used;
            }
        }

        // Register target as agent if not exists
        if !self.agents.contains_key(target_id) {
            let agent = AgentNode::new(
                target_id.to_string(),
                "unknown".to_string(),
                Some(source_id.to_string()),
                new_depth,
            );
            self.agents.insert(target_id.to_string(), agent);
        }

        // Update source children
        if let Some(source) = self.agents.get_mut(source_id) {
            source.children.push(target_id.to_string());
        }

        let tokens_consumed = self.agents.get(source_id)
            .map(|a| a.tokens_consumed)
            .unwrap_or(0.0);

        DelegationVerdict {
            allowed: true,
            source_id: source_id.to_string(),
            target_id: Some(target_id.to_string()),
            reason: "Delegation allowed".to_string(),
            impedance: report.impedance,
            flow_state: report.flow_state,
            depth: new_depth,
            velocity: report.velocity,
            tokens_consumed,
            token_budget_remaining: self.agents.get(source_id)
                .and_then(|a| a.budget_remaining()),
            ..Default::default()
        }
    }

    /// Record token consumption for an agent.
    pub fn record_tokens(&mut self, agent_id: &str, tokens: f64) -> DelegationVerdict {
        self.flow.record_tokens(tokens);

        if let Some(agent) = self.agents.get_mut(agent_id) {
            agent.tokens_consumed += tokens;

            let over_budget = agent.is_over_budget();
            let reason = if over_budget {
                format!("Agent budget exceeded: {:.0} > {:.0}",
                    agent.tokens_consumed, agent.token_budget.unwrap_or(0.0))
            } else {
                "Tokens recorded".to_string()
            };

            DelegationVerdict {
                allowed: !over_budget,
                source_id: agent_id.to_string(),
                reason,
                tokens_consumed: agent.tokens_consumed,
                token_budget_remaining: agent.budget_remaining(),
                flow_state: self.flow.flow_state(),
                ..Default::default()
            }
        } else {
            DelegationVerdict {
                allowed: false,
                source_id: agent_id.to_string(),
                reason: format!("Agent '{}' not found", agent_id),
                flow_state: self.flow.flow_state(),
                ..Default::default()
            }
        }
    }

    /// Get number of registered agents.
    pub fn num_agents(&self) -> usize {
        self.agents.len()
    }

    /// Get total delegations attempted.
    pub fn total_delegations(&self) -> u64 {
        self.total_delegations
    }

    /// Get cycles detected count.
    pub fn cycles_detected(&self) -> u64 {
        self.cycles_detected
    }

    /// Get delegations blocked count.
    pub fn delegations_blocked(&self) -> u64 {
        self.delegations_blocked
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_register_agent() {
        let mut engine = CascadeEngine::default();
        let verdict = engine.register_agent("agent-a", "gpt-4", None);
        assert!(verdict.allowed);
        assert_eq!(engine.num_agents(), 1);
    }

    #[test]
    fn test_duplicate_registration_rejected() {
        let mut engine = CascadeEngine::default();
        engine.register_agent("agent-a", "gpt-4", None);
        let verdict = engine.register_agent("agent-a", "gpt-4", None);
        assert!(!verdict.allowed);
    }

    #[test]
    fn test_delegation_allowed() {
        let mut engine = CascadeEngine::default();
        engine.register_agent("source", "gpt-4", None);
        let verdict = engine.attempt_delegation("source", "target", 0.0);
        assert!(verdict.allowed);
        assert_eq!(verdict.depth, 1);
    }

    #[test]
    fn test_cycle_detected() {
        let mut engine = CascadeEngine::default();
        engine.register_agent("a", "gpt-4", None);
        engine.attempt_delegation("a", "b", 0.0);
        // a and b are now connected — b delegating to a would be a cycle
        let verdict = engine.attempt_delegation("b", "a", 0.0);
        assert!(!verdict.allowed);
        assert!(verdict.cycle_detected);
        assert_eq!(verdict.reason, "cycle_detected");
    }

    #[test]
    fn test_depth_limit() {
        let config = EngineConfig {
            depth_limit: 2,
            ..Default::default()
        };
        let mut engine = CascadeEngine::new(config);
        engine.register_agent("root", "gpt-4", None);
        engine.attempt_delegation("root", "child1", 0.0);
        engine.attempt_delegation("child1", "child2", 0.0);
        let verdict = engine.attempt_delegation("child2", "child3", 0.0);
        assert!(!verdict.allowed);
        assert!(verdict.reason.contains("Depth limit"));
    }

    #[test]
    fn test_agent_budget() {
        let mut engine = CascadeEngine::default();
        engine.register_agent("agent-a", "gpt-4", Some(100.0));
        engine.record_tokens("agent-a", 150.0);
        let verdict = engine.attempt_delegation("agent-a", "target", 0.0);
        assert!(!verdict.allowed);
        assert!(verdict.reason.contains("budget exceeded"));
    }

    #[test]
    fn test_global_budget() {
        let config = EngineConfig {
            token_budget: Some(100.0),
            ..Default::default()
        };
        let mut engine = CascadeEngine::new(config);
        engine.register_agent("agent-a", "gpt-4", None);
        engine.record_tokens("agent-a", 150.0);
        let verdict = engine.attempt_delegation("agent-a", "target", 0.0);
        assert!(!verdict.allowed);
        assert!(verdict.reason.contains("System budget"));
    }
}
