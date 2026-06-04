//! Zero-overhead envelope parser — Rust implementation.
//!
//! Extracts only integer routing fields from MCP tool call metadata.
//! Never touches tool call arguments or payload content.
//!
//! Performance target: <100μs per parse (typically <10μs).

use serde_json::Value;
use std::time::Instant;

use crate::models::Envelope;

/// Result of parsing an envelope from an MCP tool call.
#[derive(Debug, Clone)]
pub struct ParseResult {
    pub envelope: Envelope,
    pub success: bool,
    pub error: Option<String>,
    pub parse_time_ns: u64,
}

/// Zero-overhead envelope parser for MCP tool calls.
///
/// Extracts only integer routing fields from tool call metadata.
/// Never reads, parses, tokenizes, or evaluates tool call arguments.
pub struct EnvelopeParser {
    /// Key in the tool call where envelope fields are expected
    envelope_key: String,
    /// Supported schema versions
    supported_versions: Vec<String>,
}

impl EnvelopeParser {
    /// Create a new parser with default settings.
    pub fn new() -> Self {
        Self {
            envelope_key: "_cascadeguard".to_string(),
            supported_versions: vec!["1.0".to_string()],
        }
    }

    /// Get supported schema versions.
    pub fn supported_versions(&self) -> &[String] {
        &self.supported_versions
    }

    /// Check if a schema version is supported.
    pub fn is_supported(&self, version: &str) -> bool {
        self.supported_versions.iter().any(|v| v == version)
    }

    /// Parse envelope fields from an MCP tool call.
    ///
    /// Extracts ONLY the integer routing metadata.
    /// Never touches the 'arguments' field or any payload content.
    pub fn parse(&self, tool_call: &Value) -> ParseResult {
        let start = Instant::now();

        // Extract envelope metadata block — never touch 'arguments'
        let envelope_data = match tool_call.get(&self.envelope_key) {
            Some(data) => data,
            None => {
                // No envelope metadata — return defaults (backward compat)
                return ParseResult {
                    envelope: Envelope::default(),
                    success: true,
                    error: None,
                    parse_time_ns: start.elapsed().as_nanos() as u64,
                };
            }
        };

        // Must be an object
        if !envelope_data.is_object() {
            return ParseResult {
                envelope: Envelope::default(),
                success: false,
                error: Some(format!(
                    "Envelope metadata must be an object, got {}",
                    envelope_data
                )),
                parse_time_ns: start.elapsed().as_nanos() as u64,
            };
        }

        // Detect schema version
        let schema_version = envelope_data
            .get("schema_version")
            .and_then(|v| v.as_str())
            .unwrap_or("1.0")
            .to_string();

        // Validate schema version
        if !self.is_supported(&schema_version) {
            return ParseResult {
                envelope: Envelope::default(),
                success: false,
                error: Some(format!(
                    "unsupported_schema_version: '{}'. Supported: {:?}",
                    schema_version, self.supported_versions
                )),
                parse_time_ns: start.elapsed().as_nanos() as u64,
            };
        }

        // Extract fields (v1.0 schema)
        let agent_id = envelope_data
            .get("agent_id")
            .map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            })
            .unwrap_or_default();

        let caller_id = envelope_data
            .get("caller_id")
            .map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            })
            .unwrap_or_default();

        let token_budget = envelope_data
            .get("token_budget")
            .and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.parse().ok(),
                _ => None,
            })
            .unwrap_or(0);

        let execution_seconds = envelope_data
            .get("execution_seconds")
            .and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.parse().ok(),
                _ => None,
            })
            .unwrap_or(0);

        let envelope = Envelope {
            schema_version,
            agent_id,
            caller_id,
            token_budget,
            execution_seconds,
        };

        ParseResult {
            envelope,
            success: true,
            error: None,
            parse_time_ns: start.elapsed().as_nanos() as u64,
        }
    }

    /// Reconstruct envelope fields as a JSON value.
    /// Used for round-trip verification.
    pub fn reconstruct(envelope: &Envelope) -> Value {
        serde_json::json!({
            "schema_version": envelope.schema_version,
            "agent_id": envelope.agent_id,
            "caller_id": envelope.caller_id,
            "token_budget": envelope.token_budget,
            "execution_seconds": envelope.execution_seconds,
        })
    }
}

impl Default for EnvelopeParser {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_parse_valid_envelope() {
        let parser = EnvelopeParser::new();
        let tool_call = json!({
            "name": "datadog/list_monitors",
            "arguments": {"filter": "env:prod"},
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "12345",
                "caller_id": "67890",
                "token_budget": 50000,
                "execution_seconds": 30
            }
        });

        let result = parser.parse(&tool_call);
        assert!(result.success);
        assert_eq!(result.envelope.agent_id, "12345");
        assert_eq!(result.envelope.caller_id, "67890");
        assert_eq!(result.envelope.token_budget, 50000);
        assert_eq!(result.envelope.execution_seconds, 30);
    }

    #[test]
    fn test_parse_no_envelope_returns_defaults() {
        let parser = EnvelopeParser::new();
        let tool_call = json!({
            "name": "tool",
            "arguments": {"x": 1}
        });

        let result = parser.parse(&tool_call);
        assert!(result.success);
        assert_eq!(result.envelope, Envelope::default());
    }

    #[test]
    fn test_unsupported_schema_rejected() {
        let parser = EnvelopeParser::new();
        let tool_call = json!({
            "_cascadeguard": {
                "schema_version": "99.0",
                "agent_id": "x"
            }
        });

        let result = parser.parse(&tool_call);
        assert!(!result.success);
        assert!(result.error.unwrap().contains("unsupported_schema_version"));
    }

    #[test]
    fn test_roundtrip_property() {
        let parser = EnvelopeParser::new();
        let tool_call = json!({
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "abc",
                "caller_id": "def",
                "token_budget": 1000,
                "execution_seconds": 60
            }
        });

        let result = parser.parse(&tool_call);
        assert!(result.success);

        let reconstructed = EnvelopeParser::reconstruct(&result.envelope);
        assert_eq!(reconstructed["agent_id"], "abc");
        assert_eq!(reconstructed["token_budget"], 1000);
    }

    #[test]
    fn test_parse_performance() {
        let parser = EnvelopeParser::new();
        let tool_call = json!({
            "name": "test/tool",
            "arguments": {"large_payload": "x".repeat(10000)},
            "_cascadeguard": {
                "schema_version": "1.0",
                "agent_id": "perf-test",
                "caller_id": "root",
                "token_budget": 50000,
                "execution_seconds": 30
            }
        });

        // Parse 1000 times — should be well under 100μs average
        let start = Instant::now();
        for _ in 0..1000 {
            let _ = parser.parse(&tool_call);
        }
        let elapsed_us = start.elapsed().as_micros();
        let avg_ns = (elapsed_us * 1000) / 1000;

        // Should be under 100,000ns (100μs) per parse
        assert!(
            avg_ns < 100_000,
            "Average parse time {}ns exceeds 100μs target",
            avg_ns
        );
    }
}
