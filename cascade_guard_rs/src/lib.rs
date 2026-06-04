//! CascadeGuard MCP Proxy — High-Performance Rust Implementation
//!
//! Provides sub-100μs safety check latency for production deployments
//! with >10K invocations/second throughput.
//!
//! Core components:
//! - Union-Find with path compression for O(α(N)) cycle detection
//! - FlowMonitor for impedance computation and velocity tracking
//! - Envelope parser for zero-overhead metadata extraction
//!
//! Cross-implementation equivalence:
//! For any sequence of tool invocations with identical config,
//! the Rust implementation produces identical verdicts to Python.

pub mod engine;
pub mod envelope;
pub mod models;

#[cfg(feature = "python")]
pub mod python;

pub use engine::CascadeEngine;
pub use envelope::EnvelopeParser;
pub use models::{
    DelegationAction, DelegationVerdict, Envelope, FlowState, ImpedanceReport,
};
