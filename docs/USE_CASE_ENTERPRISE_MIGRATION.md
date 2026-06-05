# Flagship Enterprise Architecture: The MCP Active Migration Router Pattern

## Zero-Risk ETRM Platform Migration via Concurrent Shadow Routing

---

## The Industry Challenge

Enterprise trading firms running legacy ETRM platforms (RightAngle, Allegro, custom OMS) face a structural trap: their current system works but cannot support autonomous AI agents, modern MCP integrations, or the regulatory demands of algorithmic trading governance.

Migration to modern platforms is the obvious answer. But every ETRM migration today follows the same catastrophic pattern:

1. **6-12 months of parallel configuration** (expensive, invisible to the business)
2. **A weekend cutover** (war room, prayer, fingers crossed)
3. **Monday morning** — live trading on the new system with no safety net

If the new system has a position mismatch, a settlement discrepancy, or a nomination failure on Monday morning, the firm faces:

- Open positions in a system they can't trust
- Manual reconciliation that takes weeks
- FERC/CFTC exposure if trades were misreported during transition
- Trading desk downtime while the issue is diagnosed

**The result:** firms delay migrations for years — sometimes decades — trapped on aging platforms that cannot support the autonomous agent workloads being deployed across the industry today.

---

## The Architectural Innovation: CascadeGuard as an Active Migration Router

CascadeGuard's MCP Proxy Middleware enables a fundamentally different migration model: **concurrent dual-routing with automated reconciliation and zero production risk.**

Instead of a cliff, there's a ramp.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Trading Agents (MCP Clients)                       │
│          Gas Agent    Power Agent    Scheduling Agent                 │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   CascadeGuard MCP Proxy                             │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Migration Router                                             │   │
│  │  ┌─────────────┐  ┌─────────────────┐  ┌────────────────┐   │   │
│  │  │ Shadow Mode │  │  Canary Routing  │  │  Full Cutover  │   │   │
│  │  │ (both)      │  │  (by commodity)  │  │  (flip auth.)  │   │   │
│  │  └─────────────┘  └─────────────────┘  └────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌────────────────┐  ┌────────────────┐  ┌───────────────────┐     │
│  │ Cycle Detection│  │ Budget Control │  │ Velocity Throttle │     │
│  │ O(α(N))       │  │ Per-Agent      │  │ Per-Agent         │     │
│  └────────────────┘  └────────────────┘  └───────────────────┘     │
└─────────────────┬─────────────────────────────────┬─────────────────┘
                  │                                   │
                  ▼                                   ▼
┌─────────────────────────────┐     ┌─────────────────────────────────┐
│     Legacy System            │     │       Target System              │
│     (RightAngle / Allegro)   │     │       (Endur / Modern ETRM)     │
│                              │     │                                  │
│     ► Remains authoritative  │     │       ► Shadow / proving mode    │
│     ► No changes required    │     │       ► Hydrated continuously    │
│     ► Production traffic     │     │       ► Reconciled daily         │
│       continues normally     │     │       ► Flipped when proven      │
└─────────────────────────────┘     └─────────────────────────────────┘
                  │                                   │
                  └─────────────┬─────────────────────┘
                                │
                                ▼
                  ┌─────────────────────────────┐
                  │   Reconciliation Engine      │
                  │                              │
                  │   • Position comparison      │
                  │   • P&L divergence detection │
                  │   • Settlement matching      │
                  │   • Nomination sync check    │
                  │   • Daily confidence report  │
                  └─────────────────────────────┘
```

### How It Works

**Phase 1: Shadow Mode (Prove Equivalence)**

Every trade entering the legacy system is simultaneously routed to the target system via CascadeGuard-governed migration agents. The legacy system remains the book of record. The target system receives an identical copy and processes it independently.

Reconciliation agents compare outputs daily: positions, P&L, settlements, nominations. Divergences are logged, diagnosed, and fixed in configuration — never in production.

**Phase 2: Canary Routing (Progressive Cutover by Commodity)**

Once a single commodity achieves N consecutive days of zero divergence (configurable: 30/60/90 days per the firm's risk tolerance), that commodity's authoritative source flips from legacy to target.

Gas goes first. Power follows. Environmental products next. Each commodity proves itself independently.

**Phase 3: Full Cutover (Mathematically Verified)**

When all commodities have been verified, the legacy system becomes the shadow. Then it's decommissioned at the firm's pace — no urgency, no war room, no cliff.

**Rollback at any point:** If a commodity shows divergence after cutover, flip it back to the legacy system instantly. The proxy route change is a single configuration update.

---

## The Algorithmic Safety Layer

While the migration runs, CascadeGuard's core engine ensures the migration agents themselves cannot cause harm:

| Safety Guarantee | Mechanism | Migration Application |
|---|---|---|
| **Cycle Prevention** | Union-Find O(α(N)) | Migration agents cannot write back to the legacy system. Namespace authorization enforces read-only against source, write-only against target. |
| **Budget Enforcement** | Per-agent token budgets | Backfill agents are budget-capped. A historical data load cannot consume unbounded resources on either system. |
| **Velocity Throttling** | Per-agent rolling window | Hydration rate is controlled. Migration agents cannot overwhelm the target system's API layer during business hours. |
| **Noisy Neighbor Isolation** | Independent per-agent counters | A misbehaving gas migration agent is throttled without affecting the power migration or live trading agents. |
| **Graceful Shutdown** | State persistence + drain | Maintenance windows and deployments don't lose migration state. Budget counters and delegation topology survive restarts. |
| **Deterministic Audit Trail** | Append-only decision log | Every routing decision is logged with reason codes. Auditors can replay the migration history and verify correctness. |

---

## Integration Architecture

CascadeGuard integrates at the **event boundary** of the legacy system — intercepting business events as they occur, not polling databases or parsing log files.

### Source System Integration (Legacy)

| Integration Point | Mechanism | Data Captured |
|---|---|---|
| Business Automation Event Hooks | Event-driven triggers on deal state transitions (created, amended, confirmed, scheduled) | Full deal payload at each lifecycle stage |
| Integration Services API | REST/SOAP query endpoints | Position snapshots, scheduling status, counterparty data |
| Change Data Capture (CDC) | Database-level stream for historical backfill | Complete trade history for reconciliation baseline |

### Target System Integration (Modern)

| Integration Point | Mechanism | Data Written |
|---|---|---|
| JVS / API Layer | Standard platform integration interface | Deal creation, amendment, confirmation |
| Nomination Services | Scheduling API | Gas Day nominations, transport scheduling |
| Position Services | Portfolio management API | Position seeding and verification |

### CascadeGuard's Role

CascadeGuard sits **between** these integration points as the MCP Proxy. It does not modify either system's data. It governs the agents that perform the data movement:

- **Authentication:** Migration agents authenticate via mTLS or IAM role. Only authorized agents can route traffic.
- **Namespace Authorization:** Read-only against legacy (`rightangle/*`). Write-only against target (`endur/*`). The production system physically cannot be corrupted by the migration process.
- **Envelope Parsing:** Zero semantic overhead — CascadeGuard reads only integer routing metadata, never inspects trade payloads. <250μs per check.

---

## Deployment

CascadeGuard runs on minimal hardware inside the client's data center or VPC. No cloud dependency. No data leaves the facility.

| Deployment Option | Hardware | Use Case |
|---|---|---|
| On-premises mini-PC | Intel NUC / equivalent ($500-800) | Single desk, one commodity |
| VM on existing hypervisor | vSphere / Hyper-V (no additional cost) | Most common — leverage existing infra |
| 1U rackmount server | Dell PowerEdge ($3K-5K) | Full trading floor, multiple commodities |
| AWS ECS Fargate (VPC-private) | PrivateLink endpoint, no public exposure | Cloud-first firms |

**Resource footprint:**
- Memory: <50MB working set
- CPU: Single core handles 32,000 checks/second
- Disk: ~1GB/month decision log retention
- Network: Adds <250μs latency per routing decision

CloudFormation and Terraform templates are included for push-button VPC-private deployment.

---

## Performance Characteristics

| Metric | Value |
|---|---|
| Safety check latency | <250μs (Python) / <100μs target (Rust) |
| Throughput | 32,000 delegation checks/second |
| Envelope parse time | ~5μs (zero semantic overhead) |
| False positive rate | 0% on legitimate delegation chains |
| Memory footprint | <50MB for 10K concurrent agents |

---

## Compliance & Audit

The migration itself is auditable from day one:

- **ISO/IEC 42001:** Continuous risk evaluation via κ_effective health coefficient
- **NIST AI RMF:** Quantified risk metrics (velocity, depth, fanout, concentration, token pressure)
- **SOC 2 Processing Integrity:** Deterministic replay — same inputs + same config = same routing decisions
- **FERC/CFTC:** Every routing decision is timestamped, attributed, and reason-coded. Point-in-time reconstruction of exactly how, why, and where any trade was routed during migration.

---

## Why This Pattern Didn't Exist Before

Traditional ETRM migrations require big-bang cutovers because there was no way to:

1. Run both systems simultaneously with transaction-level synchronization
2. Compare outputs automatically at the business level (not just database row counts)
3. Ensure the migration agents themselves couldn't corrupt production data
4. Roll back instantly if divergence appeared post-cutover

CascadeGuard's MCP Proxy pattern solves all four. The proxy sits at the protocol layer — agents don't know which backend they're talking to. The safety engine governs the migration process with the same mathematical guarantees it provides for production trading agents.

**The migration router is not a separate product.** It's CascadeGuard doing what it was built to do: governing agent delegations with constant-time safety checks. The only difference is that the "agents" are migration workers and the "delegations" are trade hydration events.

---

## Getting Started

```bash
git clone https://github.com/brinklmi/cascade-guard.git
cd cascade-guard
pip install -e .
pytest tests/ -v  # 651 tests
```

For the Rust high-performance path:
```bash
cd cascade_guard_rs
cargo build --release
cargo test  # 12 tests
cargo bench  # Latency verification
```

See [README.md](../README.md) for full documentation, [THEORY.md](../THEORY.md) for mathematical proofs, and the `infra/` directory for deployment templates.

---

*CascadeGuard — the structural safety layer that makes autonomous agent systems governable. Including the agents that migrate your trading platform.*
