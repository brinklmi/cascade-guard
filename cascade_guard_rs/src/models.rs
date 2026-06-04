//! Domain models for CascadeGuard Rust implementation.
//!
//! Mirrors the Python models exactly to ensure cross-implementation equivalence.

use serde::{Deserialize, Serialize};

/// Actions that can be delegated between agents.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DelegationAction {
    Spawn,
    Delegate,
    Escalate,
    Revoke,
}

/// System flow states based on impedance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FlowState {
    Nominal,
    Elevated,
    Throttled,
    Preservation,
}

impl FlowState {
    pub fn as_str(&self) -> &'static str {
        match self {
            FlowState::Nominal => "nominal",
            FlowState::Elevated => "elevated",
            FlowState::Throttled => "throttled",
            FlowState::Preservation => "preservation",
        }
    }
}

/// Envelope extracted from MCP tool call metadata.
/// Contains only integer routing fields — zero semantic overhead.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Envelope {
    pub schema_version: String,
    pub agent_id: String,
    pub caller_id: String,
    pub token_budget: i64,
    pub execution_seconds: i64,
}

impl Default for Envelope {
    fn default() -> Self {
        Self {
            schema_version: "1.0".to_string(),
            agent_id: String::new(),
            caller_id: String::new(),
            token_budget: 0,
            execution_seconds: 0,
        }
    }
}

/// Result of evaluating a delegation attempt.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DelegationVerdict {
    pub allowed: bool,
    pub source_id: String,
    pub target_id: Option<String>,
    pub reason: String,
    pub impedance: f64,
    pub flow_state: FlowState,
    pub depth: u32,
    pub cycle_detected: bool,
    pub velocity: f64,
    pub tokens_consumed: f64,
    pub token_budget_remaining: Option<f64>,
    pub cost_estimate: f64,
}

impl Default for DelegationVerdict {
    fn default() -> Self {
        Self {
            allowed: false,
            source_id: String::new(),
            target_id: None,
            reason: String::new(),
            impedance: 0.0,
            flow_state: FlowState::Nominal,
            depth: 0,
            cycle_detected: false,
            velocity: 0.0,
            tokens_consumed: 0.0,
            token_budget_remaining: None,
            cost_estimate: 0.0,
        }
    }
}

/// Distribution-based impedance metrics.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ImpedanceReport {
    pub impedance: f64,
    pub velocity: f64,
    pub mean_depth: f64,
    pub max_depth: u32,
    pub fan_out: f64,
    pub max_fan_out: u32,
    pub concentration: f64,
    pub token_pressure: f64,
    pub flow_state: FlowState,
}

impl Default for FlowState {
    fn default() -> Self {
        FlowState::Nominal
    }
}

/// An agent node in the delegation graph.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentNode {
    pub id: String,
    pub model_id: String,
    pub parent_id: Option<String>,
    pub depth: u32,
    pub children: Vec<String>,
    pub token_budget: Option<f64>,
    pub tokens_consumed: f64,
    pub cost_per_1k_tokens: f64,
}

impl AgentNode {
    pub fn new(id: String, model_id: String, parent_id: Option<String>, depth: u32) -> Self {
        Self {
            id,
            model_id,
            parent_id,
            depth,
            children: Vec::new(),
            token_budget: None,
            tokens_consumed: 0.0,
            cost_per_1k_tokens: 0.03,
        }
    }

    pub fn is_over_budget(&self) -> bool {
        match self.token_budget {
            Some(budget) => self.tokens_consumed > budget,
            None => false,
        }
    }

    pub fn budget_remaining(&self) -> Option<f64> {
        self.token_budget.map(|b| (b - self.tokens_consumed).max(0.0))
    }
}

/// Engine configuration parameters.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EngineConfig {
    pub max_velocity: f64,
    pub depth_limit: u32,
    pub fanout_limit: u32,
    pub preservation_threshold: f64,
    pub window_seconds: f64,
    pub token_budget: Option<f64>,
    pub dollar_budget: Option<f64>,
    pub cost_per_1k_tokens: f64,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            max_velocity: 50.0,
            depth_limit: 10,
            fanout_limit: 20,
            preservation_threshold: 0.3,
            window_seconds: 60.0,
            token_budget: None,
            dollar_budget: None,
            cost_per_1k_tokens: 0.03,
        }
    }
}
