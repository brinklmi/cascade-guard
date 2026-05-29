# Edge Cases & Production Failure Surface

A systematic analysis of every hidden failure vector across CascadeGuard's six core theorems. This document maps the complete attack surface for production hardening.

The POC implementation handles the common cases. The edge cases documented here define the scope of a production-grade deployment engagement.

---

## Theorem 1 — Inverse Ackermann Bound (O(α(N)))

### Edge Case 1.1: Concurrency Race Conditions

**The Hazard:** If Union-Find operations execute concurrently across asynchronous worker threads without thread-safe synchronization, path compression can create structural memory race conditions — two threads compressing the same path simultaneously corrupt the parent pointer array.

**Current POC Status:** Single-threaded only. Python GIL provides incidental protection but is not a guarantee for multi-process deployments.

**Production Mitigation:** Thread-safe explicit isolation via per-component read-write locks, or a lock-free concurrent Union-Find (Jayanti & Tarjan 2016). For the ETRM use case (50-100 delegations/sec), a simple mutex is sufficient — contention is negligible at this rate.

### Edge Case 1.2: Rank Overflow

**The Hazard:** In compiled languages (Rust, C++), millions of union operations could overflow a fixed-width integer rank parameter, corrupting tree balance.

**Current POC Status:** Python uses arbitrary-precision integers — overflow is impossible. Noted for future compiled-language ports.

**Production Mitigation:** Cap rank at 5 (since α(N) ≤ 4 for all physical values of N, no tree needs rank > 5). Assert on rank increment.

### Edge Case 1.3: Pathological Alternation Interleaving

**The Hazard:** An adversary deliberately designs a sequence alternating between unrelated Union and Find actions across single-node branches, temporarily driving execution time to maximum raw overhead before path compression amortizes.

**Current POC Status:** Not mitigated. The amortized bound holds over any sequence, but individual operations can be slower than average.

**Production Mitigation:** Acceptable. The amortized guarantee means total cost is bounded regardless of adversarial ordering. Worst-case single operation is O(log N), which is still sub-microsecond for practical N.

### Edge Case 1.4: Memory Footprint Saturation

**The Hazard:** While time complexity remains O(α(N)), the spatial overhead of tracking separate parent pointers and rank arrays can trigger out-of-memory at extreme scale (millions of agents).

**Current POC Status:** Python dict overhead is ~100 bytes per agent. 1M agents ≈ 100MB. Acceptable for most deployments.

**Production Mitigation:** Pre-allocated fixed-size arrays for known maximum agent counts. Memory-mapped backing store for persistence. Configurable agent registry cap with graceful rejection.

---

## Theorem 2 — Impedance Monotonicity (Z increases with load)

### Edge Case 2.1: Floating-Point Catastrophic Cancellation

**The Hazard:** At extreme scale or zero-load boundaries, calculating Z using standard IEEE 754 doubles can cause catastrophic cancellation or rounding errors, forcing Z to erroneously decrease when it should increase.

**Current POC Status:** Uses Python floats (64-bit double). Clamped to [0.0, 1.0] via `min(1.0, max(0.0, ...))`. Sufficient for normal operation.

**Production Mitigation:** Fixed-point integer scaling (multiply all ratios by 10,000, compute in integer arithmetic, divide at output). Eliminates floating-point non-determinism entirely.

### Edge Case 2.2: Network Partition Latency Cascades

**The Hazard:** If load is calculated using real-world asynchronous telemetry, network delays can cause timestamps to arrive out of chronological order, mimicking a false drop in impedance.

**Current POC Status:** Assumes local, synchronous operation. Timestamps are generated at point of delegation, not at point of receipt.

**Production Mitigation:** Monotonic clock enforcement (time.monotonic(), not time.time()). Out-of-order timestamp rejection. Causal ordering via vector clocks in distributed deployments.

### Edge Case 2.3: Negative Telemetry Injection

**The Hazard:** A compromised agent reporting invalid negative metrics can corrupt the impedance calculation, driving Z below zero and κ above 1.0.

**Current POC Status:** Clamped. `min(1.0, max(0.0, impedance))` prevents Z from leaving [0, 1].

**Production Mitigation:** Input validation at the recording boundary. Reject any delegation event with negative depth, negative fanout, or negative timestamp delta. Log and alert on injection attempts.

---

## Theorem 3 — Z Computation Complexity (O(K))

### Edge Case 3.1: Dynamic Source Collision

**The Hazard:** If unique sources (K) change dynamically during active execution, allocating a static hash map will cause costly memory reallocation overhead, breaking the strict linear time budget.

**Current POC Status:** Python dict handles dynamic growth transparently. Amortized O(1) insertion.

**Production Mitigation:** Pre-allocated source pool with configurable maximum K. If K exceeds pool size, oldest sources are evicted (LRU). Guarantees bounded memory and bounded computation.

### Edge Case 3.2: Memory Allocator Contention

**The Hazard:** In multi-tenant systems, setting up fresh data structures for large numbers of unique sources can lock up the system memory allocator, shifting execution from in-memory to OS paging.

**Current POC Status:** Not applicable in single-process Python deployment.

**Production Mitigation:** Arena allocation for source tracking structures. Pre-warm the allocator at startup. Monitor RSS and trigger graceful degradation before OOM.

### Edge Case 3.3: CPU Cache Miss Overhead

**The Hazard:** If K grows to extreme numbers (100K+ unique sources), traversing the source dictionary can trigger L1/L2 cache misses, making computation significantly slower than the theoretical O(K) bound suggests.

**Current POC Status:** Python dict is hash-based with good cache locality for moderate K. Performance degrades gracefully.

**Production Mitigation:** Cache-friendly data layout (struct-of-arrays instead of array-of-structs). Keep hot path data (source counts) in a contiguous array. Profile cache behavior under load.

---

## Theorems 4 & 5 — Deterministic Zero-Error Guarantee

### Edge Case 4.1: Physical Memory Bit Flips

**The Hazard:** Bit flips inside physical RAM (cosmic rays, hardware degradation) can silently change parent pointers, turning a mathematically flawless deterministic guarantee into an unpredictable failure state.

**Current POC Status:** Not mitigated. Relies on hardware ECC memory (standard in server environments).

**Production Mitigation:** ECC memory requirement in deployment spec. Periodic integrity checksums on the Union-Find parent array. If checksum fails, trigger state reconstruction from decision log.

### Edge Case 4.2: Clock Distortion Across Distributed Nodes

**The Hazard:** If the system reads telemetry across multiple machines with unsynchronized system times, chronological ordering breaks, producing incorrect temporal assessments.

**Current POC Status:** Single-machine deployment assumed. All timestamps from local monotonic clock.

**Production Mitigation:** NTP synchronization requirement (< 10ms drift). Logical timestamps (Lamport clocks) for ordering guarantees independent of wall clock. Decision log uses sequence numbers, not timestamps, for ordering.

### Edge Case 4.3: Floating-Point Equality in Derived Computations

**The Hazard:** Any downstream system that checks CascadeGuard outputs for exact floating-point equality (==) will experience non-deterministic behavior due to IEEE 754 rounding.

**Current POC Status:** CascadeGuard's core cycle detection uses string comparison (node IDs), not floating-point. κ_effective is a float but is only compared against thresholds using `<` and `>=`, never `==`.

**Production Mitigation:** Document that κ_effective should never be compared with `==`. Provide epsilon-based comparison utilities if downstream systems require equality checks.

---

## Theorem 6 — Self-Healing Convergence (κ Recovery)

### Edge Case 6.1: Window Over-Saturation Paradox

**The Hazard:** If the system generates new delegation events faster than the configured recovery window can expire old ones, the rolling window grows unboundedly, trapping the system in perpetual PRESERVATION mode.

**Current POC Status:** The deque is pruned on every access (timestamps older than window_seconds are removed). But if events arrive faster than pruning, the deque grows.

**Production Mitigation:** Hard cap on deque size (e.g., max 10,000 entries). If cap is reached, oldest entries are force-evicted regardless of age. This bounds memory and ensures recovery is always possible.

### Edge Case 6.2: Boundary Oscillation (κ at Threshold)

**The Hazard:** If κ_effective sits exactly on the preservation threshold (0.3), minor telemetry noise causes rapid oscillation between THROTTLED and PRESERVATION states — the system flaps.

**Current POC Status:** No hysteresis. Transitions are instantaneous based on current κ value.

**Production Mitigation:** Dead-band / hysteresis: enter PRESERVATION at κ < 0.3, but only exit PRESERVATION when κ > 0.4 (configurable recovery margin). Prevents oscillation at the boundary.

### Edge Case 6.3: Telemetry Starvation Lockup

**The Hazard:** If an agent stops reporting metrics completely during a critical window, the self-healing engine lacks data to evaluate recovery, locking up the convergence path indefinitely.

**Current POC Status:** The flow monitor computes velocity from timestamps in the deque. If no new timestamps arrive, velocity naturally drops to 0 as old timestamps age out. Recovery proceeds normally.

**Production Mitigation:** Explicit starvation timeout: if no delegation events are recorded for 2× the window period, force κ_effective to 1.0 (assume healthy). Log the starvation event for investigation.

---

## Production Hardening Matrix

| Theorem | Primary Failure Vector | Mitigation | Production Asset |
|---|---|---|---|
| Theorem 1 (α(N)) | Concurrency race conditions | Thread-safe explicit isolation | `DISJOINT_SET_LOCK` |
| Theorem 2 (Z monotonicity) | Float catastrophic cancellation | Fixed-point integer scaling | `Z_SCALE_INT64` |
| Theorem 3 (O(K)) | Dynamic memory allocation | Pre-allocated fixed buffer arrays | `K_SOURCE_POOL` |
| Theorems 4 & 5 (0-error) | Physical bit flips | ECC + periodic checksums | `INTEGRITY_CHECKSUM` |
| Theorem 6 (κ recovery) | Window over-saturation | Sliding window size cap + hysteresis | `RECOVERY_LIMIT_CAP` |
| 10K-hop stress | Call-stack exhaustion | Iterative loop (already implemented) | `CIRCUIT_BREAKER_MAX` |

---

## Engagement Scope

The POC handles the common operational cases. The edge cases documented above define the scope of a **Production Implementation** engagement:

- **POC (open source):** Handles single-threaded, single-machine, moderate-scale deployments. Sufficient for development environments and proof-of-concept demonstrations.
- **Production ($75K-$120K):** Adds thread safety, hysteresis, starvation detection, memory caps, and monitoring integration. Calibrated to customer's specific agent infrastructure.
- **Enterprise ($150K-$250K):** Adds distributed deployment support, ECC verification, vector clocks, arena allocation, and full observability stack integration.

Each tier builds on the previous. The mathematical guarantees (Theorems 1-6) hold at all tiers. The edge case mitigations determine operational resilience under adversarial or extreme conditions.
