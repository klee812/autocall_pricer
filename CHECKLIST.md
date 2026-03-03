# Implementation Checklist

**Project:** Distributed Market Computation Engine
**Last updated:** 2026-02-22

---

## List 1 — AWS Configuration (no code, config only)

### Storage
- [ ] S3 bucket — create, enable encryption, block public access
- [ ] S3 lifecycle rule — Standard → Glacier IR at 30 days → delete at 365 days
- [ ] DynamoDB TaskConfig table — partition key `task_id`, on-demand capacity, TTL on `ttl` field
- [ ] DynamoDB TaskState table — partition key `task_id`, on-demand capacity, TTL on `ttl` field (30 days)

### Queues and Streams
- [ ] SQS TaskQueue-DLQ — message retention 14 days
- [ ] SQS TaskQueue — visibility timeout 300s, long polling 20s, point to TaskQueue-DLQ
- [ ] SQS ResultDelivery-DLQ — catches failed Lambda deliveries, retention 14 days
- [ ] Kinesis stream `task-results` — 5 shards, 24hr retention
- [ ] Firehose delivery stream — source: Kinesis `task-results`, destination: S3, gzip compression, date-partitioned prefix, buffer 128MB/300s

### Compute
- [ ] ElastiCache Redis — node type `cache.r7g.xlarge`, VPC subnet, security group, 1 replica, auto-failover
- [ ] EC2 launch template — `c5.4xlarge`, AMI, IAM instance profile, security group, user data
- [ ] EC2 Auto Scaling Group — min 0, max 20, scale-in policy CPU < 5% for 5min, Spot + On-Demand mix

### Serverless
- [ ] Lambda function `market-compute-result-relay` — Python 3.12, arm64, 512MB, 60s timeout, env vars for REST API URL and DLQ URL
- [ ] Lambda Kinesis trigger — batch size 500, parallelisation factor 1, bisect on error enabled, destination: ResultDelivery-DLQ

### IAM
- [ ] IAM role: worker EC2 — SQS (receive/delete/change visibility), DynamoDB (get/update), Kinesis (put records), ASG (set instance protection), EC2 (describe instances)
- [ ] IAM role: Lambda — Kinesis (read), SQS ResultDelivery-DLQ (send message), CloudWatch Logs
- [ ] IAM role: Firehose — Kinesis (read), S3 (put object)

### Scheduling
- [ ] EventBridge rule: pre-warm — 09:00 ET weekdays, set ASG desired capacity to N
- [ ] EventBridge rule: EOD safety — 16:15 ET weekdays, set ASG min_size to 0

### Weekly Downsample Pipeline
- [ ] Glue catalog database and table — schema over raw S3 prefix `results-raw/`
- [ ] Step Functions state machine — triggers Athena CTAS, waits for completion, handles failure
- [ ] EventBridge rule: weekly downsample — weekend schedule, triggers Step Functions

---

## List 2 — Code to Write

### Core Application

- [ ] `config/task_config.py` — Pydantic model for TaskConfig, TaskState enum (PENDING / RUNNING / COMPLETE / FAILED)
- [ ] `config/settings.py` — all AWS resource names and endpoints via environment variables
- [ ] `worker/models/etf.py` — ETF sum-product valuation logic
- [ ] `worker/models/options.py` — Black-Scholes option model
- [ ] `worker/kinesis_publisher.py` — batch buffer, flush at 500 records or end of tick
- [ ] `worker/process.py` — 1-second metronome loop, Redis reads, model dispatch, Kinesis publish
- [ ] `coordinator/task_loader.py` — fetch full TaskConfig from DynamoDB by task_id, strongly consistent read
- [ ] `coordinator/process_manager.py` — spawn, monitor, and restart worker processes
- [ ] `coordinator/main.py` — SQS poll, DynamoDB fetch, slice tasks, spawn workers, instance protection, drain and exit

- [ ] `result_relay/lambda_handler.py` — deserialise Kinesis batch (500 records), POST to REST API, handle failures

### SQL
- [ ] Athena CTAS query — GROUP BY task_id + minute, OHLC aggregation, output to `results-1min/` in Parquet

### External (out of scope, but need building by someone)
- [ ] Tier 0: Config Generator — writes TaskConfig to DynamoDB, publishes task_ids to SQS sorted ascending by `active_until`
- [ ] Tier 1: Ticker Plant — writes live prices to Redis `market:{symbol}` hash on every tick

### Supporting
- [ ] `Dockerfile` — python:3.12-slim, coordinator as entrypoint
- [ ] `pyproject.toml` — dependencies: pydantic, boto3, redis, structlog
- [ ] `scripts/publish_tasks.py` — dev utility to inject test task configs into DynamoDB and SQS
- [ ] `tests/test_etf_model.py` — unit tests for ETF valuation
- [ ] `tests/test_option_model.py` — unit tests for option model
- [ ] `tests/test_metronome.py` — timing accuracy of the 1-second loop
- [ ] `tests/test_kinesis_batch.py` — batch buffer and flush logic
