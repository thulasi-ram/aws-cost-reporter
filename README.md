# aws-cost-reporter

Daily AWS cost report across an AWS Organization. Pulls from Cost Explorer in
the payer account, renders per-account stacked-bar charts, writes a combined
markdown report, persists daily history to DynamoDB, and posts a Slack summary
at 09:00 IST.

**Status:** Phase 1–3 complete.

## Architecture

```
EventBridge (03:30 UTC daily)
        │
        ▼
  Lambda (zip, python3.12)
        │
        ├── Cost Explorer (payer account)   ← 1 call, 95 days, grouped by account+service
        ├── Organizations.ListAccounts       ← human account names
        ├── DynamoDB  (history + run marker)
        ├── S3        (report.md + charts/*.png)
        ├── SSM       (Slack webhook URL, SecureString)
        └── Slack incoming webhook           ← Blocks payload + presigned S3 links
```

Everything is provisioned by OpenTofu (`tofu/`). The Lambda runs the same
`cost_reporter.py` pipeline used locally.

## Phase 1 — local disk-only run

Prerequisites:

- [uv](https://github.com/astral-sh/uv)
- AWS credentials for the **payer / management** account with
  `ce:GetCostAndUsage` and `organizations:ListAccounts`.

```bash
uv sync
AWS_PROFILE=root uv run cost_reporter.py
```

Output lands in `./report/` — open `report.md` (charts are in `./report/charts/`).

Override the report day (defaults to T-2 UTC):

```bash
uv run cost_reporter.py --date 2026-04-01 --output-dir ./out
```

## Phase 2 — Lambda zip + Slack delivery

`lambda_handler.py` is the Lambda entrypoint. It reuses the pipeline from
`cost_reporter.py` and layers on the delivery glue:

- S3 upload of `report.md` + chart PNGs → 7-day presigned URLs
- DynamoDB history persistence for every (account, service, date) row
- DynamoDB run marker per report date → re-invocations are no-ops
- Slack Blocks payload posted via `urllib` (no `slack-sdk`)
- On failure: short error posted to Slack and the Lambda re-raises so the
  CloudWatch alarm fires

`scripts/deploy.sh` builds the deployment zip locally: it `pip install`s the
runtime deps (polars, matplotlib, seaborn, boto3) into `build/package/`
using manylinux2014_x86_64 wheels for python 3.12, drops the two `.py`
files next to them, and zips the result into `build/function.zip`. The
zipped size lands around ~45 MiB — well under Lambda's 50 MiB direct-upload
limit and 250 MiB unzipped limit.

## Phase 3 — OpenTofu provisioning

`tofu/` provisions everything:

| Resource | Purpose |
|---|---|
| S3 bucket | Markdown reports + chart PNGs (400-day lifecycle, versioned, SSE-S3) |
| DynamoDB | History + idempotency run markers (PAY_PER_REQUEST, PITR, 400-day TTL) |
| SSM SecureString | Slack webhook URL (Tofu creates the shell; value set out-of-band) |
| Lambda (zip, python3.12) | The reporter itself |
| IAM role + policies | CE, Organizations, S3, DynamoDB, SSM read |
| EventBridge rule | `cron(30 3 * * ? *)` = 09:00 IST daily |
| CloudWatch log group | 30-day retention |
| CloudWatch alarm | Fires on any Lambda error in a 24h window |

### First-time deployment

Tofu reads `build/function.zip` as the Lambda package, so the zip must
exist before the first apply. `deploy.sh` builds it; on a fresh machine
there's no Lambda yet so it just builds and exits.

```bash
# 1. Build the deployment zip
./scripts/deploy.sh

# 2. Apply everything
cd tofu
tofu init
tofu apply

# 3. Set the real Slack webhook URL in SSM (Tofu left a placeholder)
aws ssm put-parameter \
  --name "$(tofu output -raw slack_webhook_ssm_parameter)" \
  --value "https://hooks.slack.com/services/T.../B.../..." \
  --type SecureString \
  --overwrite
```

### Subsequent deploys

When you change the Python code, just rebuild the zip. `deploy.sh` detects
the existing Lambda and calls `update-function-code` directly; Tofu
doesn't need to run.

```bash
./scripts/deploy.sh
```

When you change `tofu/` HCL:

```bash
cd tofu && tofu apply
```

### Manual invoke (local test against real resources)

```bash
aws lambda invoke \
  --function-name "$(cd tofu && tofu output -raw lambda_function_name)" \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json
```

Override the report date for ad-hoc runs:

```bash
aws lambda invoke \
  --function-name "$(cd tofu && tofu output -raw lambda_function_name)" \
  --payload '{"date":"2026-04-01"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json
```

The run marker guards against double-posting: if a date has already been
reported successfully, re-invocations return `{"status":"skipped"}`. Delete
the marker in DynamoDB (`pk="run"`, `sk="2026-04-01"`) to force a re-run.

### Tail the Lambda logs

```bash
aws logs tail "$(cd tofu && tofu output -raw cloudwatch_log_group)" --follow
```

## What the report contains

- One Cost Explorer call per run: 95 days of `AmortizedCost`, grouped by
  `LINKED_ACCOUNT` + `SERVICE`, daily granularity.
- Filters out `Tax`, `Refund`, `Credit` record types.
- Excludes monthly lump services (AWS Support Developer/Business/Enterprise)
  from the daily view so they don't skew averages or trigger false
  Appeared/Disappeared insights.
- Default report day is **T-2 UTC** (the last finalized CE day).
- **Merged insights** at the top of the report, grouped by account, with
  quiet accounts skipped. Rules: DoD jumps, anomalies vs 30d baseline,
  services appeared / disappeared vs T-30..T-1 historical window, and an
  EC2-Other callout.
- Per account: stacked bar chart (Yesterday / 30d avg / 90d avg, top 10
  services + Other) with total dollar labels on top of each bar, and a
  top-15 service table including `% of day` and day-over-day deltas.
- Consistent service colours across all account charts (hand-picked
  Carto Bold + Prism strong categorical palette).

All tunables live at the top of `cost_reporter.py` — lookback window, top-N
sizes, insight thresholds, the monthly-lump service list, and the palette.
