//! PyO3 Python bindings for cross-implementation testing.
//!
//! Exposes the Rust CascadeEngine to Python so that Hypothesis-generated
//! test sequences can verify equivalence between implementations.
//!
//! Build with: `maturin develop --features python`

#[cfg(feature = "python")]
use pyo3::prelude::*;

#[cfg(feature = "python")]
use crate::engine::CascadeEngine;
#[cfg(feature = "python")]
use crate::envelope::EnvelopeParser;
#[cfg(feature = "python")]
use crate::models::EngineConfig;

/// Python-accessible CascadeEngine wrapper.
#[cfg(feature = "python")]
#[pyclass]
pub struct PyCascadeEngine {
    engine: CascadeEngine,
}

#[cfg(feature = "python")]
#[pymethods]
impl PyCascadeEngine {
    #[new]
    #[pyo3(signature = (
        max_velocity=50.0,
        depth_limit=10,
        fanout_limit=20,
        preservation_threshold=0.3,
        window_seconds=60.0,
        token_budget=None,
    ))]
    fn new(
        max_velocity: f64,
        depth_limit: u32,
        fanout_limit: u32,
        preservation_threshold: f64,
        window_seconds: f64,
        token_budget: Option<f64>,
    ) -> Self {
        let config = EngineConfig {
            max_velocity,
            depth_limit,
            fanout_limit,
            preservation_threshold,
            window_seconds,
            token_budget,
            ..Default::default()
        };
        Self {
            engine: CascadeEngine::new(config),
        }
    }

    /// Register a root agent. Returns (allowed, reason).
    fn register_agent(
        &mut self,
        agent_id: &str,
        model_id: &str,
        token_budget: Option<f64>,
    ) -> (bool, String) {
        let verdict = self.engine.register_agent(agent_id, model_id, token_budget);
        (verdict.allowed, verdict.reason)
    }

    /// Attempt delegation. Returns (allowed, reason, cycle_detected).
    fn attempt_delegation(
        &mut self,
        source_id: &str,
        target_id: &str,
        tokens_used: f64,
    ) -> (bool, String, bool) {
        let verdict = self.engine.attempt_delegation(source_id, target_id, tokens_used);
        (verdict.allowed, verdict.reason, verdict.cycle_detected)
    }

    /// Record tokens for an agent. Returns (allowed, reason).
    fn record_tokens(&mut self, agent_id: &str, tokens: f64) -> (bool, String) {
        let verdict = self.engine.record_tokens(agent_id, tokens);
        (verdict.allowed, verdict.reason)
    }

    /// Get number of agents.
    fn num_agents(&self) -> usize {
        self.engine.num_agents()
    }

    /// Get total delegations.
    fn total_delegations(&self) -> u64 {
        self.engine.total_delegations()
    }

    /// Get cycles detected.
    fn cycles_detected(&self) -> u64 {
        self.engine.cycles_detected()
    }
}

/// Python-accessible envelope parser wrapper.
#[cfg(feature = "python")]
#[pyclass]
pub struct PyEnvelopeParser {
    parser: EnvelopeParser,
}

#[cfg(feature = "python")]
#[pymethods]
impl PyEnvelopeParser {
    #[new]
    fn new() -> Self {
        Self {
            parser: EnvelopeParser::new(),
        }
    }

    /// Parse envelope from a JSON string. Returns (success, agent_id, caller_id, token_budget, exec_seconds, error).
    fn parse_json(
        &self,
        json_str: &str,
    ) -> (bool, String, String, i64, i64, Option<String>) {
        match serde_json::from_str(json_str) {
            Ok(value) => {
                let result = self.parser.parse(&value);
                (
                    result.success,
                    result.envelope.agent_id,
                    result.envelope.caller_id,
                    result.envelope.token_budget,
                    result.envelope.execution_seconds,
                    result.error,
                )
            }
            Err(e) => (false, String::new(), String::new(), 0, 0, Some(e.to_string())),
        }
    }

    /// Get supported schema versions.
    fn supported_versions(&self) -> Vec<String> {
        self.parser.supported_versions().to_vec()
    }
}

/// Python module initialization.
#[cfg(feature = "python")]
#[pymodule]
fn cascade_guard_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyCascadeEngine>()?;
    m.add_class::<PyEnvelopeParser>()?;
    Ok(())
}
