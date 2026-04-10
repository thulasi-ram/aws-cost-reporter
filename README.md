# aws-cost-reporter

Daily AWS cost report across an AWS Organization. Single-file Python script
that pulls from Cost Explorer in the payer account and writes a combined
markdown report + per-account charts to disk.

**Status:** Phase 1 (local, disk-only).

## Prerequisites

- [uv](https://github.com/astral-sh/uv)
- AWS credentials for the **payer / management** account with:
  - `ce:GetCostAndUsage`
  - `organizations:ListAccounts`

## Run

```bash
uv sync
AWS_PROFILE=root uv run cost_reporter.py
```

Output lands in `./report/` — open `report.md` (charts are in `./report/charts/`).

Override the report day (defaults to T-2 UTC):

```bash
uv run cost_reporter.py --date 2026-04-01 --output-dir ./out
```

## What it does

- One Cost Explorer call per run: 95 days of `AmortizedCost`, grouped by
  `LINKED_ACCOUNT` + `SERVICE`, daily granularity.
- Filters out `Tax`, `Refund`, `Credit` record types.
- Default report day is **T-2 UTC** (the last finalized CE day).
- Per account it produces:
  - A stacked bar chart: Yesterday / 30d avg / 90d avg, segmented by the
    top 10 services + "Other".
  - A top-15 table with DoD and 30d-avg deltas.
  - A rule-based analysis section (DoD jumps, anomalies, new / dropped
    services, EC2-Other callout).

All tunables live at the top of `cost_reporter.py` under the `Config` block.

## Roadmap

- **Phase 2:** Lambda container image, DynamoDB history cache, Slack webhook
  posting with chart images uploaded to S3 and linked via presigned URLs,
  idempotency run-marker, failure-to-Slack error handling.
- **Phase 3:** OpenTofu provisioning — Lambda, IAM, DynamoDB, EventBridge
  (03:30 UTC = 09:00 IST), SSM SecureString for the webhook URL, ECR repo,
  CloudWatch error alarm.
