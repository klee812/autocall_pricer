# Distributed Market Computation Engine — Technical Design

**Status:** Draft v3
**Audience:** Engineering team, future reference
**Last updated:** 2026-02-22

---

## 1. Problem Statement

We need to run approximately 200,000 financial calculation tasks (ETF valuations and option models) throughout the trading day. Each task:

- Is configured by an external process (symbols, model parameters, active time window)
- Reads live market data from a shared cache
- Re-computes its result every second on a strict metronome
- Publishes results to a REST API for downstream consumers
- Runs for a configured time window (anywhere from 2 hours to the full trading day)

The system must be **cost-efficient** (no idle compute), **horizontally scalable**, and **guarantee** a fresh result every second per active task. Peak result throughput is approximately 5,000 results/second.

---

## 2. Architecture Overview

The system is composed of four decoupled tiers. Each tier has a single responsibility and communicates with adjacent tiers via durable queues or shared cache — never direct coupling.

```
┌──────────────────────────────────────────────────────────────────┐
│  TIER 0 — Config Generation (external process, periodic)         │
│                                                                  │
│  Generates task configs throughout the trading day               │
│  → DynamoDB: TaskConfig table  (source of truth, queryable)      │
│  → SQS: TaskQueue              (one message per task_id)         │
└──────────────────────────────────────────────────────────────────┘
                              │ SQS polls
┌──────────────────────────────────────────────────────────────────┐
│  TIER 1 — Ticker Plant (separate, decoupled process)             │
│                                                                  │
│  Fetches live market data for all active symbols                 │
│  → Publishes to Redis (latest price per symbol)                  │
│  Workers read from Redis — Ticker Plant is not aware of workers  │
└──────────────────────────────────────────────────────────────────┘
                              │ Redis reads
┌──────────────────────────────────────────────────────────────────┐
│  TIER 2 — Worker Fleet (EC2 Auto Scaling Group)                  │
│                                                                  │
│  Instances pre-warmed on market open schedule                    │
│  Each instance:                                                  │
│    - Pulls task configs from SQS until at capacity               │
│    - Spawns one worker process per CPU core                      │
│    - Each process owns a fixed slice of task configs             │
│    - Runs a strict 1-second metronome loop                       │
│    - Publishes results to Kinesis                                │
│  Scale-down: processes expire → instance idles → ASG terminates  │
└──────────────────────────────────────────────────────────────────┘
                              │ Kinesis (5k records/sec, 5 shards)
┌──────────────────────────────────────────────────────────────────┐
│  TIER 3 — Result Delivery                                        │
│                                                                  │
│  ├── Lambda (batch consumer, 500 records/invocation)             │
│  │     → Batch POST to REST API                                  │
│  │     → DLQ on failure (Kinesis 24hr retention = short replay)  │
│  │                                                               │
│  └── Firehose (independent consumer, parallel)                   │
│        → S3 archival (full fidelity, indefinite retention)       │
│        → Emergency recovery if REST API down > 24 hours          │
│        → Raw material for batch downsampling (daily/weekly)      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Tier 0 — Config Generation

### Responsibility

An external process (outside the scope of this document) generates task configurations throughout the trading day and publishes them into the system. It is the sole writer to the config store.

### Data flow

```
Config Generator
  1. Writes full config to DynamoDB  ← source of truth, auditable
  2. Publishes task_id to SQS        ← delivery trigger to workers
```

The SQS message contains only the `task_id`. Workers fetch the full config from DynamoDB after receiving the message. This keeps SQS messages tiny and the config queryable independently of the queue.

**Required: publish in ascending `active_until` order.** The Config Generator must sort tasks by `active_until` before publishing to SQS — shortest-lived tasks first, longest-lived last. This is the sole mechanism for end-of-day fleet consolidation (see §5 Scale-down strategy and §10). It is a hard interface requirement on Tier 0, not an optimisation.

### Why SQS and not DynamoDB as the queue?

DynamoDB is an excellent config store but a poor work queue. Using it as a queue would require:

- Custom "claim task" logic using conditional `UpdateItem` expressions
- A polling loop that consumes read capacity units continuously
- Manual implementation of visibility timeout (re-queue if worker crashes)
- A dead-letter equivalent built from scratch

SQS provides all of this natively. The two services play complementary roles: DynamoDB stores the config durably and makes it queryable; SQS guarantees exactly-once delivery to a worker. Neither alone is sufficient.

### DynamoDB: TaskConfig table

```
Partition key:  task_id  (String)   e.g. "etf-SPY-20260222-001"

Fields:
  task_id          String    Unique identifier
  task_type        String    "etf_valuation" | "option_model"
  symbols          List      Market data symbols required
  parameters       Map       Model-specific parameters (weights, strikes, expiry, etc.)
  active_from      String    ISO-8601 — when to start computing
  active_until     String    ISO-8601 — when to stop computing
  publish          Boolean   Whether this task emits to Kinesis
  created_at       String    ISO-8601
  ttl              Number    Unix epoch + 7 days (DynamoDB auto-delete)
```

### DynamoDB: TaskState table

Tracks live execution state, separate from config:

```
Partition key:  task_id  (String)

Fields:
  task_id          String
  state            String    PENDING | RUNNING | COMPLETE | FAILED
  worker_instance  String    EC2 instance ID of owning worker
  worker_pid       Number    Process ID within instance
  started_at       String    ISO-8601
  completed_at     String    ISO-8601 (nullable)
  last_result_at   String    ISO-8601 — timestamp of last published result
  error_message    String    (nullable)
  ttl              Number    Unix epoch + 30 days
```

---

## 4. Tier 1 — Ticker Plant

The Ticker Plant is a **separate, fully decoupled process**. It has no awareness of the worker fleet.

### Responsibility

- Subscribe to or poll a market data source for all active symbols
- Write the latest price (and any other needed fields) to Redis on every tick
- Key structure: `market:{symbol}` → JSON hash of price fields

### Interface contract with workers

Workers read from Redis using the symbol list in their task config:

```python
data = redis.hmget(f"market:{symbol}", "last_price", "bid", "ask", "volume")
```

The Ticker Plant is responsible for keeping these keys fresh. Workers treat Redis as a read-only, always-current source of truth. If a key is stale or missing, the worker skips that tick and records a missed computation.

### Decoupling rationale

- The Ticker Plant can be replaced, restarted, or scaled independently
- Workers are not affected by market data source changes
- Redis acts as the contract boundary between the two tiers

---

## 5. Tier 2 — Worker Fleet

This is the core of the system. Workers pull task configs from SQS, compute results on a 1-second metronome, and publish to Kinesis.

### Instance design

**Instance type:** `c5.4xlarge` (16 vCPU, 32 GB RAM)

A c5.4xlarge is chosen over larger instances to limit blast radius — if an instance fails, it loses at most ~3,000 tasks rather than ~20,000. Multiple instances also provide better fault tolerance and more predictable bin-packing.

Each instance runs:
- 1 coordinator process
- 15 worker processes (leaving 1 core for the coordinator and OS)
- Each worker process owns ~200 task configs

**Capacity per instance:** ~3,000 tasks
**Expected instances at peak:** ~10 (for 30,000 concurrent tasks)

### Process model — why NOT asyncio

The calculations (ETF sum-product, Black-Scholes option models) are **CPU-bound pure Python**. Asyncio is designed for I/O-bound concurrent work. Using it here causes two problems:

1. **The event loop blocks** — CPU work never yields to the scheduler, so other coroutines are delayed. The 1-second cadence guarantee cannot be upheld.
2. **run_in_executor adds IPC overhead** — the only asyncio workaround is offloading to a process pool via `run_in_executor`, which serialises (pickles) every task's data across process boundaries on every single tick. This is needless overhead for work that can simply run in its own process.

**The right model:** Each worker process owns a static slice of task configs and loops independently. There is no shared state, no IPC per calculation, and no event loop. The timing guarantee comes from `time.monotonic()`, not a scheduler.

### The 1-second metronome loop

```python
# worker_process.py

import time
import redis
import boto3
from models import calculate_etf, calculate_option

def run(tasks: list[TaskConfig], instance_id: str, pid: int):
    r = redis.Redis(host=REDIS_HOST, decode_responses=True)
    kinesis = boto3.client("kinesis", region_name=AWS_REGION)
    records_buffer = []

    next_tick = time.monotonic() + 1.0

    while any(t.is_active() for t in tasks):
        tick_start = time.monotonic()

        for task in tasks:
            if not task.is_active():
                continue

            # Read market data from Redis (sub-millisecond, sync is appropriate here)
            data = {sym: r.hgetall(f"market:{sym}") for sym in task.symbols}

            # CPU-bound calculation — pure Python, no GIL release needed
            if task.task_type == "etf_valuation":
                result = calculate_etf(task, data)
            elif task.task_type == "option_model":
                result = calculate_option(task, data)

            if task.should_publish():
                records_buffer.append({
                    "Data": json.dumps(result),
                    "PartitionKey": task.task_id,
                })

        # Batch flush to Kinesis (max 500 records per put_records call)
        if records_buffer:
            flush_to_kinesis(kinesis, records_buffer)
            records_buffer.clear()

        # Sleep precisely until next 1-second boundary
        elapsed = time.monotonic() - tick_start
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            # Calculations took >1s — log warning, skip sleep
            log.warning("tick_overrun", elapsed_ms=round(elapsed * 1000))

        next_tick += 1.0
```

### Timing budget per tick (per process)

| Task type       | Per-task time (est.) | 200 tasks/process |
|-----------------|---------------------|-------------------|
| ETF valuation   | ~0.1ms              | 20ms total        |
| Option model    | ~0.2ms              | 40ms total        |
| Redis reads     | ~0.5ms              | 100ms total       |
| Kinesis flush   | ~5ms (amortised)    | 5ms total         |
| **Total**       |                     | **~165ms**        |
| **Headroom**    |                     | **835ms**         |

The 1-second guarantee is robust. Even 5× slower calculations would fit within the window.

### Coordinator process

Each instance runs a coordinator that manages the worker processes:

```
Coordinator responsibilities:
  1. On startup: register instance protection with ASG
     (prevents ASG from terminating this instance during scale-in)

  2. Pull task configs from SQS until instance is at capacity
     (capacity = 15 processes × 200 tasks = 3,000 tasks)

  3. Fetch full config for each task_id from DynamoDB

  4. Divide tasks into 15 equal slices

  5. Spawn 15 worker processes via multiprocessing.Process,
     passing each its slice of task configs

  6. Monitor worker processes — restart if any crash unexpectedly

  7. Periodically check: are all worker processes done?
     (all tasks expired past their active_until time)

  8. When all workers are done:
     - Remove instance protection from ASG
     - Exit cleanly
     (ASG scale-in policy will terminate the idle instance)
```

### Scale-out strategy

The fleet is **pre-warmed on a schedule** rather than reactively scaled. Since the trading day is highly predictable, we know when tasks will start arriving.

```
EventBridge schedule:
  09:00 ET → set ASG desired_capacity = N (instances boot and warm up)
  09:15 ET → Config Generator begins publishing to SQS
  09:30 ET → Market open, workers are already running and processing
  16:15 ET → set ASG min_size = 0 (safety net for end-of-day cleanup)
```

N is calibrated to expected peak concurrent tasks / 3,000 tasks per instance. Start with N=10 for 30,000 concurrent tasks and adjust.

**Why pre-warm rather than reactive auto-scaling?**

EC2 + ECS cold start is 2–4 minutes. A reactive scale-out triggered by SQS queue depth at 09:30 would mean the first several minutes of market data is processed late. Pre-warming eliminates this latency entirely at negligible cost (a few extra minutes of idle instance time).

### Scale-down strategy

Tasks naturally expire when they reach their `active_until` time. As tasks expire throughout the day:

1. Worker processes finish their loops and exit
2. Coordinator detects all workers done
3. Coordinator removes EC2 instance protection
4. ASG scale-in policy (`CPU < 5% for 5 minutes`) terminates the unprotected instance
5. Fleet shrinks organically as the day progresses

The `min_size = 0` scheduled at 16:15 ensures any remaining instances with protection already removed are terminated cleanly at end of day.

**No custom heartbeat or stale detection is needed** — tasks have deterministic end times. A task that stops computing simply reaches `active_until` and its process exits. There is no "crashed worker" problem because each task's time window is bounded.

### End-of-day fleet consolidation

A VM can only terminate when every task assigned to it has expired. If tasks are distributed randomly across VMs, each VM ends up with a mix of short- and long-running tasks. At end of day this produces multiple VMs each holding a handful of residual tasks — compute waste that scales with the number of instances.

**The fix is publish order, not runtime rebalancing.** Because VMs pull tasks from SQS in FIFO order, the Config Generator controls which tasks land on which VM simply by controlling publish sequence:

```
Publish order: ascending active_until (shortest-lived first)

VM 1 fills up first  → tasks expiring 09:30–12:00 → terminates midday
VM 2 fills up next   → tasks expiring 12:00–14:00 → terminates mid-afternoon
VM 3 fills up last   → tasks expiring 14:00–16:00 → terminates end of day
```

Long-running tasks consolidate onto the fewest possible VMs. No coordinator changes. No worker changes. The entire mechanism is a sort at publish time in Tier 0.

*Note: this works best when all tasks are published before the fleet starts pulling. If tasks arrive continuously throughout the day (published as they are generated), ordering provides partial but not perfect consolidation. Revisit if tail waste is significant after running in production.*

---

## 6. Tier 3 — Result Delivery

### Why Kinesis and not DynamoDB Streams or SQS

At 5,000 results/second sustained over 6.5 market hours:

| Transport           | Approx. daily cost | Notes                                       |
|---------------------|--------------------|---------------------------------------------|
| DynamoDB writes     | ~$140/day          | 5k WCUs/sec × $0.00000125 × 23,400 seconds |
| SQS                 | ~$47/day           | $0.40/million × 117M messages               |
| Kinesis             | ~$3.50/day         | 5 shards + PUT cost                         |

Kinesis is purpose-built for high-throughput ordered streams. It is the right tool at this throughput.

### Kinesis configuration

```
Stream:    task-results
Shards:    5  (1,000 records/shard/second limit → 5,000/sec total)
Retention: 24 hours (replay window if REST API is unavailable)
Partition: task_id (ensures ordered delivery per task)
```

### Lambda consumer

```
Trigger:        Kinesis stream (task-results)
Batch size:     500 records per invocation
Parallelism:    1 concurrent Lambda per shard = 5 concurrent Lambdas
Invocations:    ~10/second (5,000 records / 500 batch)

On each invocation:
  - Deserialise 500 result records
  - POST to REST API: { "results": [...500 items...] }
  - On success: Lambda checkpoints Kinesis position
  - On failure: Lambda retries (up to Kinesis 24hr retention)
  - After exhausting retries: send to SQS dead-letter queue
```

### S3 archival via Firehose

A Firehose delivery stream is attached to the Kinesis stream as an **independent second consumer**. It runs in parallel with the Lambda consumer — neither is aware of the other.

```
Kinesis stream (task-results)
  ├── Lambda consumer      → REST API         (primary delivery path)
  └── Firehose consumer    → S3               (archival, no code changes needed)
```

Firehose handles batching, buffering, compression (gzip), and partitioned S3 writes natively. No Lambda code changes. No worker changes.

**S3 key structure:**

```
s3://market-compute-archive/
  results/
    year=2026/month=02/day=22/hour=09/
      task-results-1-2026-02-22-09-30-00-uuid.gz
```

**S3 lifecycle policy** (set on bucket creation, not negotiable):

```
S3 Standard    0–30 days    Fast retrieval, full cost — active recovery window
S3 Glacier IR  30–365 days  ~$0.004/GB/month — long-term retention, hours retrieval
Delete         After 1 year  (or per compliance requirements)
```

**Rough storage cost:**

At 5k records/sec × 6.5 hours/day × ~500 bytes/record (compressed ~10×):
~3–4 GB/day compressed → ~$0.07/day in S3 Standard → negligible.

**Batch downsampling:**

S3 is the raw archive. Downsampling (e.g. 1 snapshot/minute per task_id) is a separate batch job run daily or weekly using Athena, Spark, or a simple Python script. This is intentional — pre-committing to a sampling strategy before archival is a one-way door. Keeping full fidelity in S3 and downsampling after the fact is always reversible.

**Recovery scenarios covered by each mechanism:**

| Failure scenario | Recovery mechanism |
|---|---|
| REST API down minutes/hours | Kinesis 24hr retention + Lambda auto-retry |
| REST API down > 24 hours | S3 archival replay (manual or scripted) |
| Need specific task history or audit | S3 archival (queryable by date/task prefix) |

The two mechanisms are complementary, not redundant. The Lambda/Kinesis path handles short outages automatically. S3 handles extended outages and provides an indefinite audit trail.

### Delivery guarantee

This is the **outbox pattern** implemented without a database write in the hot path:

- Worker writes result to Kinesis (durable, replicated, acknowledged in <5ms)
- If the REST API is down, records accumulate in Kinesis — Lambda retries automatically
- If a Lambda invocation fails, Kinesis does not advance its checkpoint — records are re-processed
- At-least-once delivery is guaranteed within the 24-hour retention window
- The dead-letter queue captures any results that exhaust all retries for manual inspection
- Firehose writes all records to S3 regardless of REST API status — extended recovery window

The traditional database outbox (write to table → poll → relay) adds a full synchronous DB write on every result. At 5,000 results/second this is expensive and slow. Kinesis achieves the same durability guarantee at a fraction of the cost with lower latency.

---

## 7. AWS Infrastructure Summary

| Service | Purpose | Configuration |
|---|---|---|
| **EC2 ASG** | Worker fleet | c5.4xlarge, mixed Spot + On-Demand, `BEST_FIT_PROGRESSIVE` |
| **SQS** | Task distribution | Standard queue, 1 message per task config |
| **DynamoDB** | Config + state store | TaskConfig table, TaskState table, on-demand capacity |
| **Redis (ElastiCache)** | Market data cache | Shared read by all worker processes on all instances |
| **Kinesis** | Result stream | 5 shards, 24hr retention, partition by task_id |
| **Lambda** | Result relay | Kinesis trigger, batch 500, POST to REST API |
| **Firehose** | S3 archival | Independent Kinesis consumer, gzip, partitioned by date |
| **S3** | Result archive | Full-fidelity archival; lifecycle: Standard→Glacier IR→delete |
| **EventBridge** | Scheduler | Pre-warm fleet at 09:00, safety scale-down at 16:15 |
| **SQS DLQ** | Failed results | Captures Kinesis records that exhaust Lambda retries |

### IAM roles required

**Worker EC2 instance role:**
- `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility` on TaskQueue
- `dynamodb:GetItem`, `UpdateItem` on TaskConfig and TaskState tables
- `kinesis:PutRecords` on task-results stream
- `autoscaling:SetInstanceProtection` on the worker ASG
- `ec2:DescribeInstances` (to get own instance ID)

**Lambda execution role:**
- `kinesis:GetRecords`, `GetShardIterator`, `DescribeStream`, `ListShards`
- `sqs:SendMessage` on the DLQ

**Firehose delivery role:**
- `kinesis:GetRecords`, `GetShardIterator`, `DescribeStream`, `ListShards` on task-results stream
- `s3:PutObject` on the archive bucket

---

## 8. Project Structure

```
market-compute-engine/
├── coordinator/
│   ├── main.py              # EC2 instance coordinator: SQS pull, process management, scale-in
│   ├── task_loader.py       # Fetch task configs from DynamoDB by task_id
│   └── process_manager.py   # Spawn, monitor, restart worker processes
├── worker/
│   ├── process.py           # Worker process main loop (1-second metronome)
│   ├── models/
│   │   ├── etf.py           # ETF valuation: sum-product of quantity × price
│   │   └── options.py       # Option model (Black-Scholes or similar)
│   └── kinesis_publisher.py # Batch buffer + put_records to Kinesis
├── config/
│   ├── settings.py          # Pydantic BaseSettings — all config via environment variables
│   └── task_config.py       # Pydantic model for TaskConfig, validation
├── result_relay/
│   └── lambda_handler.py    # Lambda: read Kinesis batch → POST to REST API
├── infra/
│   ├── setup.py             # Idempotent boto3 script: create all AWS resources
│   └── iam_policies.json    # IAM role definitions
├── scripts/
│   └── publish_tasks.py     # Dev/test utility: write test configs to DynamoDB + SQS
├── Dockerfile               # python:3.12-slim, ENTRYPOINT coordinator/main.py
├── pyproject.toml
└── tests/
    ├── test_etf_model.py    # Unit tests for ETF valuation logic
    ├── test_option_model.py # Unit tests for option model logic
    ├── test_metronome.py    # Timing accuracy test for 1-second loop
    └── test_kinesis_batch.py # Result batching and flush logic
```

---

## 9. Implementation Roadmap

Suggested order — each step is independently testable.

| Step | Component | Description |
|---|---|---|
| 1 | `config/task_config.py` | Pydantic model for TaskConfig, TaskState enum |
| 2 | `config/settings.py` | All AWS resource names via env vars |
| 3 | `infra/setup.py` | Create DynamoDB tables, SQS queue, Kinesis stream |
| 4 | `worker/models/etf.py` | ETF valuation logic + unit tests |
| 5 | `worker/models/options.py` | Option model logic + unit tests |
| 6 | `worker/kinesis_publisher.py` | Batch buffer, flush at 500 records or 1s |
| 7 | `worker/process.py` | 1-second metronome loop, integrates steps 4–6 |
| 8 | `coordinator/task_loader.py` | DynamoDB fetch by task_id, strong consistent read |
| 9 | `coordinator/process_manager.py` | Spawn/monitor/restart worker processes |
| 10 | `coordinator/main.py` | SQS poll, slice division, instance protection, drain |
| 11 | `result_relay/lambda_handler.py` | Kinesis batch consumer → batch POST to REST API |
| 12 | `Dockerfile` | Container packaging coordinator as entrypoint |
| 13 | Integration test | Single instance, 10 test tasks, verify 1s cadence end-to-end |
| 14 | Load test | 3,000 tasks on one instance, measure tick overruns |

---

## 10. Key Design Decisions — Rationale

### Process-per-slice, not asyncio

Asyncio is the wrong tool for CPU-bound work with a hard timing guarantee. When a coroutine executes CPU-bound code, it blocks the entire event loop — all other coroutines are delayed. There is no way to guarantee a 1-second result from an async loop running pure Python math.

Each worker process owns its tasks exclusively. The metronome loop is a simple `while` loop with `time.monotonic()` for cadence. There is no scheduler, no event loop, no IPC per calculation. This is the simplest model that satisfies the timing guarantee.

### SQS for task distribution, DynamoDB for config storage

SQS provides: at-least-once delivery, visibility timeout (no two workers claim the same task), dead-letter queue, and efficient long-polling. DynamoDB provides: a persistent, queryable record of every task config with its full parameters. Neither service alone provides both. The pattern — store the full record in DynamoDB, deliver the key via SQS — is standard for distributed work queues.

### Kinesis for result delivery

At 5,000 results/second, DynamoDB writes cost ~$140/day; Kinesis costs ~$3.50/day. Beyond cost, Kinesis provides ordered delivery per partition key (task_id), 24-hour replay, and native Lambda batch triggering. This makes it the natural outbox transport at this throughput.

### Pre-warm on schedule rather than reactive auto-scaling

The trading day is predictable to the minute. Cold-starting EC2 instances takes 2–4 minutes. Reactive scaling triggered at market open means the first several minutes of results would be delayed. Pre-warming at 09:00 means the fleet is ready and idle by 09:30 at negligible extra cost.

### Task publish order drives end-of-day fleet consolidation

Each VM can only release its EC2 instance protection — and therefore terminate — when every task it was assigned has expired. Randomly distributed tasks give every VM a mix of short- and long-running tasks, leaving many VMs alive at end of day with only a handful of residual tasks each.

The fix is a sort at publish time, not runtime rebalancing. The Config Generator publishes tasks in ascending `active_until` order. Because VMs pull from SQS in FIFO order, shortest-lived tasks fill the first VMs and longest-lived tasks fill the last. At end of day, residual tasks are already concentrated on the minimum number of VMs — no coordinator logic, no worker changes, no task migration.

This is deliberately simple and deferred to Tier 0 (Config Generator) as an interface contract. If the `active_until` distribution turns out to be narrow (most tasks expire within a short window of each other), this optimisation has negligible effect and a more dynamic approach (coordinator drain-and-handoff threshold) can be revisited.

### S3 archival via Firehose, not Lambda; full fidelity, downsample later

S3 archival is emergency recovery — if the REST API is unavailable for more than the 24-hour Kinesis retention window, or if a specific task's history is needed for audit or reconstruction.

Firehose is used as the S3 writer rather than the Lambda consumer for a clean separation of concerns: the Lambda is responsible for REST API delivery; Firehose is responsible for archival. They are independent consumers on the same Kinesis stream and have no coupling.

Full fidelity is written to S3 (every record, every second). Downsampling (e.g. 1 snapshot/minute per task) is a separate batch job. Pre-committing to a sampling strategy before archival is a one-way door — once data is discarded it cannot be recovered. Keeping full fidelity and downsampling after the fact is always reversible and allows the sampling strategy to change without re-deploying the hot path.

### No per-tick heartbeat needed

Unlike general-purpose job queues where tasks may crash mid-execution, our tasks have explicit `active_until` timestamps. A task that stops running simply reaches its end time — there is no ambiguous "is this task stuck?" state. This eliminates the need for heartbeat daemons and stale detection infrastructure.

---

## 11. Open Questions for Review

The following items should be confirmed with the team before implementation begins:

1. **Task capacity per instance** — the estimate of 200 tasks/process assumes ~0.2ms average per calculation. This should be profiled against real ETF and option model implementations before sizing the fleet.

2. **Redis topology** — a single ElastiCache node is assumed. If the Ticker Plant writes at high frequency across many symbols, a Redis cluster with read replicas may be needed to serve all worker instances without becoming a bottleneck.

3. **SQS visibility timeout** — set to 120 seconds by default. If coordinator startup (booting, fetching configs from DynamoDB, slicing tasks) takes longer than this, tasks may be re-delivered to another coordinator. Measure startup time and set the timeout accordingly.

4. **REST API batch POST contract** — the Lambda sends batches of 500 results per POST. The REST API must be able to accept and process a 500-record payload within Lambda's timeout (15 minutes, but should be much faster). Confirm the API's batch endpoint schema.

5. **Spot interruption handling** — if a Spot instance is reclaimed mid-day, its tasks are lost until the coordinator's SQS messages become visible again (after visibility timeout). Consider whether tasks need a fast re-assignment path (e.g., a secondary on-demand instance pool) or if a 2-minute gap is acceptable.

6. **Result schema** — the Kinesis record structure needs agreement between worker and Lambda teams. Suggested fields: `task_id`, `computed_at` (ISO-8601), `task_type`, `result` (dict of model outputs), `symbols_used`.

7. **`active_until` distribution** — the publish-order consolidation strategy works best when task durations are spread across the trading day. If the majority of tasks share a similar `active_until` (e.g. all expire at market close), the sort has no effect and tail waste is negligible anyway. Profile the real distribution before deciding whether more dynamic consolidation (coordinator drain-and-handoff) is worth building.
