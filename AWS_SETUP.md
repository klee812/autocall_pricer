# AWS Infrastructure Setup Guide

**Purpose:** Step-by-step AWS configuration for the market-compute-engine.
**Last updated:** 2026-02-22
**Region:** us-east-1 (or whichever region hosts the trading infrastructure)

Services must be created in the order below — later steps reference resources created in earlier ones.

---

## Step 1 — S3 Archive Bucket

Create before Firehose (Firehose needs the bucket ARN at creation time).

**Bucket name:** `market-compute-archive`

Key settings:
- Versioning: off (append-only archive, no need)
- Default encryption: SSE-S3
- Block all public access: yes
- Lifecycle rule named `archive-tiering`:
  - Transition to S3 Glacier Instant Retrieval after 30 days
  - Expire (delete) objects after 365 days (adjust to compliance requirements)

---

## Step 2 — DynamoDB Tables

Both tables use on-demand capacity — no provisioning needed, billing is per request.

### TaskConfig table

- Table name: `TaskConfig`
- Partition key: `task_id` (String)
- Capacity mode: on-demand
- TTL attribute: `ttl` (enable TTL on this field — DynamoDB will auto-delete records 7 days after the value set in the field)
- Point-in-time recovery: enabled

No GSIs needed for the initial design — tasks are always looked up by `task_id`.

### TaskState table

- Table name: `TaskState`
- Partition key: `task_id` (String)
- Capacity mode: on-demand
- TTL attribute: `ttl` (30-day retention)
- Point-in-time recovery: enabled

---

## Step 3 — SQS Queues

Create the dead-letter queue first — the main queue references its ARN.

### Dead-letter queue (DLQ)

- Queue name: `TaskQueue-DLQ`
- Type: Standard
- Message retention: 14 days (maximum — gives maximum time to investigate failures)
- All other settings: defaults

### Main task queue

- Queue name: `TaskQueue`
- Type: Standard
- Visibility timeout: 300 seconds (5 minutes — must exceed maximum coordinator startup time; measure and tune before go-live)
- Message retention: 4 days
- Dead-letter queue: `TaskQueue-DLQ`, maximum receives: 3
- Long-polling: receive message wait time = 20 seconds (reduces empty receives, cuts cost)

### Result dead-letter queue

- Queue name: `ResultDelivery-DLQ`
- Type: Standard
- Message retention: 14 days
- Purpose: catches Kinesis records that exhaust all Lambda retries

---

## Step 4 — Kinesis Data Stream

- Stream name: `task-results`
- Capacity mode: Provisioned
- Shard count: 5 (supports 5,000 records/second write throughput and 10 MB/sec)
- Data retention: 24 hours (default; extend to 7 days if budget allows — widens the replay window)
- Encryption: SSE with AWS-managed key

---

## Step 5 — Kinesis Data Firehose (S3 Archival)

The Firehose reads from the Kinesis stream as an independent consumer and writes to S3.

- Delivery stream name: `task-results-archive`
- Source: Kinesis Data Stream → `task-results`
- Destination: S3 → `market-compute-archive`
- S3 prefix: `results/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/`
- S3 error prefix: `errors/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/`
- Buffer size: 128 MB or 300 seconds (whichever comes first) — tune based on record volume
- Compression: GZIP
- IAM role: create a new role, allow Firehose to read from `task-results` and write to `market-compute-archive`

---

## Step 6 — IAM Roles

### Worker instance role (`market-compute-worker-role`)

Attach to the EC2 instance profile used by the ASG. Permissions needed:

- SQS: `ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility` on `TaskQueue`
- DynamoDB: `GetItem`, `UpdateItem` on `TaskConfig` and `TaskState`
- Kinesis: `PutRecords` on `task-results`
- Auto Scaling: `SetInstanceProtection` on the worker ASG
- EC2: `DescribeInstances` (scoped to own instance — use condition on instance ID tag)

### Lambda execution role (`market-compute-lambda-role`)

- Kinesis: `GetRecords`, `GetShardIterator`, `DescribeStream`, `ListShards` on `task-results`
- SQS: `SendMessage` on `ResultDelivery-DLQ`
- CloudWatch Logs: `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` (standard Lambda logging)

---

## Step 7 — Lambda Function (Result Relay)

- Function name: `market-compute-result-relay`
- Runtime: Python 3.12
- Architecture: arm64 (Graviton — ~20% cheaper than x86 for same performance)
- Memory: 512 MB (batch deserialisation of 500 records; tune down if profiling shows less needed)
- Timeout: 60 seconds (REST API POST should complete in <5s; 60s gives ample headroom)
- Execution role: `market-compute-lambda-role`
- Environment variables:
  - `REST_API_URL` — batch POST endpoint
  - `DLQ_URL` — ARN of `ResultDelivery-DLQ`

**Trigger (Kinesis event source mapping):**
- Stream: `task-results`
- Batch size: 500
- Parallelisation factor: 1 (one Lambda per shard = 5 concurrent; increase if delivery latency becomes a concern)
- Starting position: LATEST
- Bisect on error: enabled (on batch failure, splits batch in half to isolate the bad record)
- Destination on failure: `ResultDelivery-DLQ`
- Maximum retry attempts: 3

---

## Step 8 — ElastiCache (Redis)

Used exclusively by the Ticker Plant (writes) and worker fleet (reads). Not part of the result delivery path.

- Cluster name: `market-data-cache`
- Engine: Redis 7.x
- Node type: `cache.r7g.xlarge` (start here; scale up if Ticker Plant write rate causes saturation)
- Number of nodes: 1 primary + 1 replica (replica provides read scale-out for worker fleet)
- Subnet group: place in the same VPC and subnets as the EC2 worker fleet
- Security group: allow inbound on port 6379 from worker ASG security group and Ticker Plant security group only; no public access
- Automatic failover: enabled (promotes replica if primary fails)
- Encryption in transit: enabled

---

## Step 9 — EC2 Auto Scaling Group (Worker Fleet)

### Launch template

- Name: `market-compute-worker-lt`
- AMI: Amazon Linux 2023 (or a custom AMI with Python 3.12 and dependencies pre-installed — strongly recommended to avoid slow cold starts)
- Instance type: `c5.4xlarge`
- IAM instance profile: `market-compute-worker-role`
- User data: script that pulls and starts the coordinator container on boot
- Security group: outbound only — HTTPS (443) to AWS APIs, port 6379 to ElastiCache; no inbound required

### Auto Scaling Group

- ASG name: `market-compute-worker-asg`
- Launch template: `market-compute-worker-lt`
- VPC / subnets: same VPC as ElastiCache, spread across 2–3 AZs
- Desired capacity: 0 (managed by EventBridge schedule)
- Minimum capacity: 0
- Maximum capacity: 20 (safety ceiling)
- Instance mix: On-Demand base of 2 instances, remainder as Spot with `BEST_FIT_PROGRESSIVE` allocation strategy
- Scale-in policy: target tracking, `ASGAverageCPUUtilization` < 5%, cooldown 300 seconds
- Instance protection: set programmatically by the coordinator at startup (not in ASG config)

---

## Step 10 — EventBridge Scheduled Rules

All times are US Eastern. EventBridge uses UTC — adjust accordingly (ET is UTC-5 in winter, UTC-4 in summer).

### Rule 1 — Pre-warm fleet

- Name: `market-compute-prewarm`
- Schedule: `cron(0 14 ? * MON-FRI *)` (09:00 ET winter / adjust for DST)
- Target: Auto Scaling Group API — set `desired_capacity` to N (start with 10; tune after load testing)
- Input: hardcoded JSON with ASG name and desired capacity

### Rule 2 — End-of-day safety scale-down

- Name: `market-compute-eod-scaledown`
- Schedule: `cron(15 21 ? * MON-FRI *)` (16:15 ET winter)
- Target: Auto Scaling Group API — set `min_size` to 0
- Purpose: ensures any instances whose coordinator already removed protection are terminated; not the primary scale-down mechanism (that is organic), just a safety net

---

## Step 11 — Verify connectivity before go-live

In order:

1. Launch one worker instance manually (outside the ASG) and confirm it can read from SQS, read/write DynamoDB, write to Kinesis, and call `SetInstanceProtection` without permission errors
2. Send a test record to Kinesis manually and confirm Lambda invokes, POSTs to the REST API, and checkpoints correctly
3. Send a test record to Kinesis manually and confirm Firehose delivers a compressed file to S3 within the buffer window (up to 5 minutes)
4. Publish 10 test task configs to DynamoDB + SQS using `scripts/publish_tasks.py` and confirm the coordinator picks them up, spawns workers, and results appear in Kinesis
5. Trigger the EventBridge pre-warm rule manually and confirm the ASG scales to the desired capacity

---

## Open configuration questions

- **Visibility timeout (Step 3):** defaulted to 300 seconds. Measure actual coordinator startup time (boot → SQS pull → DynamoDB fetch → process spawn) and set to 2× that value.
- **Firehose buffer size (Step 5):** 128 MB / 300 seconds is a reasonable starting point. At 5k records/sec × ~500 bytes uncompressed, the time buffer (300s) will likely trigger before the size buffer. Tune after observing actual record sizes.
- **Redis node type (Step 8):** `cache.r7g.xlarge` is a starting estimate. Monitor `CurrConnections`, `NetworkBytesIn`, and `EngineCPUUtilization` on day one and resize if needed.
- **ASG desired capacity N (Step 10):** start at 10 for ~30,000 concurrent tasks. Revise after profiling real task calculation times (see open question 1 in DESIGN.md).
