//! Benchmark for CascadeGuard safety check latency.
//!
//! Target: <100μs per attempt_delegation call.
//! Run with: `cargo bench`

use criterion::{black_box, criterion_group, criterion_main, Criterion};

use cascade_guard_rs::engine::CascadeEngine;
use cascade_guard_rs::envelope::EnvelopeParser;
use cascade_guard_rs::models::EngineConfig;

fn bench_attempt_delegation(c: &mut Criterion) {
    let mut group = c.benchmark_group("safety_check");

    group.bench_function("attempt_delegation_single", |b| {
        b.iter_batched(
            || {
                let mut engine = CascadeEngine::new(EngineConfig::default());
                engine.register_agent("source", "gpt-4", None);
                engine
            },
            |mut engine| {
                black_box(engine.attempt_delegation("source", "target-1", 0.0));
            },
            criterion::BatchSize::SmallInput,
        );
    });

    group.bench_function("attempt_delegation_10_agents", |b| {
        b.iter_batched(
            || {
                let mut engine = CascadeEngine::new(EngineConfig::default());
                for i in 0..10 {
                    engine.register_agent(&format!("agent-{}", i), "gpt-4", None);
                }
                engine
            },
            |mut engine| {
                black_box(engine.attempt_delegation("agent-0", "new-target", 100.0));
            },
            criterion::BatchSize::SmallInput,
        );
    });

    group.bench_function("attempt_delegation_100_agents", |b| {
        b.iter_batched(
            || {
                let mut engine = CascadeEngine::new(EngineConfig {
                    depth_limit: 200,
                    fanout_limit: 200,
                    ..Default::default()
                });
                engine.register_agent("root", "gpt-4", None);
                for i in 0..100 {
                    engine.attempt_delegation("root", &format!("child-{}", i), 0.0);
                }
                engine
            },
            |mut engine| {
                black_box(engine.attempt_delegation("root", "new-target", 50.0));
            },
            criterion::BatchSize::SmallInput,
        );
    });

    group.finish();
}

fn bench_envelope_parse(c: &mut Criterion) {
    let mut group = c.benchmark_group("envelope_parse");

    let tool_call = serde_json::json!({
        "name": "datadog/list_monitors",
        "arguments": {"filter": "env:prod", "limit": 100, "extra": "x".repeat(1000)},
        "_cascadeguard": {
            "schema_version": "1.0",
            "agent_id": "12345",
            "caller_id": "67890",
            "token_budget": 50000,
            "execution_seconds": 30
        }
    });

    group.bench_function("parse_with_payload", |b| {
        let parser = EnvelopeParser::new();
        b.iter(|| {
            black_box(parser.parse(&tool_call));
        });
    });

    group.finish();
}

fn bench_cycle_detection(c: &mut Criterion) {
    let mut group = c.benchmark_group("cycle_detection");

    group.bench_function("detect_cycle_in_3_nodes", |b| {
        b.iter_batched(
            || {
                let mut engine = CascadeEngine::new(EngineConfig::default());
                engine.register_agent("a", "gpt-4", None);
                engine.attempt_delegation("a", "b", 0.0);
                engine.attempt_delegation("b", "c", 0.0);
                engine
            },
            |mut engine| {
                // c → a would be a cycle
                black_box(engine.attempt_delegation("c", "a", 0.0));
            },
            criterion::BatchSize::SmallInput,
        );
    });

    group.finish();
}

criterion_group!(
    benches,
    bench_attempt_delegation,
    bench_envelope_parse,
    bench_cycle_detection,
);
criterion_main!(benches);
