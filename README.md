# aws-cost-reporter

Daily AWS cost report across an AWS Organization. Pulls from Cost Explorer in
the payer account, persists a per-day slice to DynamoDB, writes a combined
markdown report to S3, and posts a rich Slack Block Kit summary.

No charts, no images — everything lives in the Slack message or the markdown
report.

## Architecture

```
EventBridge (03:30 UTC = 09:00 IST)
        │
        ▼
  Lambda (zip, python3.12)
        │
        ├── Cost Explorer       ← 1 paginated call, 95 days, grouped by account+service
        ├── Organizations       ← human account names
        ├── DynamoDB            ← daily history + idempotency run marker
        ├── S3                  ← markdown report (text only)
        ├── SSM SecureString    ← Slack webhook URL
        └── Slack webhook       ← Block Kit payload (org totals, insights, per-account)
```

One shared S3 bucket holds everything, organised by prefix:

```
<project-prefix>-aws-cost-reporter-prod/
├── tofu/
│   └── terraform.tfstate
├── lambda/
│   └── function.zip
└── reports/
    └── YYYY-MM-DD/
        └── report.md
```

## Prerequisites

- [uv](https://github.com/astral-sh/uv)
- [OpenTofu](https://opentofu.org) 1.6+
- `aws` CLI, authenticated as an identity with admin-ish rights in the payer
  (management) account — Cost Explorer, Organizations, Lambda, S3, DynamoDB,
  SSM, IAM, EventBridge, CloudWatch.
- `zip` on `PATH` (standard on macOS and most Linux).
- A Slack incoming webhook URL.

## Local run (no AWS deploy needed)

```bash
uv sync
AWS_PROFILE=root uv run cost_reporter.py
```

Writes `./report/report.md`. Override the date:

```bash
uv run cost_reporter.py --date 2026-04-01 --output-dir ./out
```

## Deploy

One command does everything — bucket bootstrap, build, tofu init, tofu apply:

```bash
PROJECT_PREFIX=treebo AWS_REGION=ap-south-1 ./scripts/deploy.sh
```

- `PROJECT_PREFIX` (optional) — prepended to resource names wherever AWS
  reserves the `aws` namespace (SSM parameter paths). Without it, the SSM
  path would be `/aws-cost-reporter-prod/...` which hits
  `No access to reserved parameter name`.
- `AWS_REGION` (optional) — defaults to `us-east-1`. Flows through to both
  the tofu S3 backend and the AWS provider, so the bucket and all resources
  land in the same region.

The script is idempotent — safe to re-run on every code or infra change.

### First-time post-deploy

Set the real Slack webhook (tofu leaves a placeholder so secrets stay out of
git):

```bash
aws ssm put-parameter \
  --name "$(cd tofu && tofu output -raw slack_webhook_ssm_parameter)" \
  --value "https://hooks.slack.com/services/T.../B.../..." \
  --type SecureString \
  --overwrite
```

### What deploy.sh does, step by step

1. Computes the bucket name: `{prefix}-aws-cost-reporter-prod` or
   `aws-cost-reporter-prod` if no prefix.
2. Creates the bucket if missing; applies encryption, versioning,
   public-access-block, and a 400-day lifecycle rule on every run (all
   idempotent `put-*` calls).
3. Runs `./scripts/build.sh` (invoked via tofu `null_resource`):
    - `uv export` → pinned `requirements.txt` from `uv.lock`
    - `uv pip install --python-platform x86_64-manylinux_2_28 --python-version 3.12 --no-build`
      downloads Linux wheels directly into `build/package/` — no venv
      involved, no host-platform sdist fallback.
    - Copies `cost_reporter.py` + `lambda_handler.py` into the package.
    - Strips `tests/`, `__pycache__/`, `.pyi` to keep the zip lean.
    - `zip`s to `build/function.zip`.
4. `tofu init -reconfigure -backend-config=bucket=... -backend-config=region=...`
   — backend bucket is passed at init because the state lives in the same
   bucket (no chicken-and-egg).
5. `tofu apply -var=region=... [-var=project_prefix=...]` creates everything
   else.

### Subsequent deploys

```bash
PROJECT_PREFIX=treebo AWS_REGION=ap-south-1 ./scripts/deploy.sh
```

The `source_hash` on `aws_s3_object.lambda_zip` is keyed off the source
files (`cost_reporter.py`, `lambda_handler.py`, `uv.lock`, `build.sh`) — if
none changed, tofu skips the upload.

## Resources

All provisioned by tofu except the S3 bucket:

| Resource | Notes |
|---|---|
| S3 bucket | **Bootstrapped by `deploy.sh`** (not managed by tofu). Holds tofu state, the lambda zip, and markdown reports under distinct prefixes. |
| `aws_s3_object.lambda_zip` | Uploads the zip. Keyed on source-file hashes to avoid plan/apply drift. |
| DynamoDB | `(pk, sk)` table — history rows (`pk=account_id`, `sk=date#service`) and run markers (`pk="run"`, `sk=date`). PAY_PER_REQUEST, PITR, 400-day TTL. |
| SSM SecureString | Slack webhook. Tofu sets a placeholder, `ignore_changes = [value]` so your real value survives. |
| Lambda (zip, python3.12) | Reads from S3. |
| IAM role + inline policy | CE, Organizations, S3, DynamoDB, SSM. |
| EventBridge rule | `cron(30 3 * * ? *)` — daily 03:30 UTC / 09:00 IST. |
| CloudWatch log group | 30-day retention. |
| CloudWatch alarm | Fires on ≥1 Lambda error in a 24 h window. |

## Operate

### Manual invoke (real AWS, real Slack)

```bash
aws lambda invoke \
  --function-name "$(cd tofu && tofu output -raw lambda_function_name)" \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json
```

Override the report date (useful for backfills):

```bash
aws lambda invoke \
  --function-name "$(cd tofu && tofu output -raw lambda_function_name)" \
  --payload '{"date":"2026-04-01"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json
```

Re-invocations on a date already reported return `{"status":"skipped"}`.
Delete the DynamoDB item (`pk="run"`, `sk="2026-04-01"`) to force a re-run.

### Tail logs

```bash
aws logs tail "$(cd tofu && tofu output -raw cloudwatch_log_group)" --follow
```

## What the Slack message contains

- **Header** with the report date and environment.
- **Org totals** as a 4-field block: org total, DoD, 30d avg, vs 30d.
- **Insights** merged across all accounts — quiet accounts are skipped.
  Rules: DoD jumps (≥30 % and ≥$5), anomalies vs 30d baseline (≥2× and
  ≥$5), services appeared/disappeared vs the previous 30d, and an
  EC2-Other breakdown (EBS / data transfer / misc).
- **Per-account sections**: header line with yesterday / 30d / 90d / DoD,
  followed by the top-5 services for that account with DoD arrows.
- **Full report button** linking to the markdown in S3 (presigned URL, 7 d).

## What the markdown report contains

Everything in Slack plus a full per-account table of the top-15 services
with yesterday / day-before / 30d avg / 90d avg / % of day / Δ DoD / Δ vs
30d. Lives at `reports/{date}/report.md` in the bucket.

## Configuration

All tunables live at the top of `cost_reporter.py`:

- `LOOKBACK_DAYS = 95` — covers the 90-day avg plus buffer.
- `TOP_N_TABLE = 15` — services per account in the markdown table.
- `TOP_N_SLACK = 5` — services per account in the Slack block.
- `EXCLUDED_RECORD_TYPES = ["Tax", "Refund", "Credit"]`
- `MONTHLY_LUMP_SERVICES` — AWS Support tiers excluded from the daily view
  because AmortizedCost doesn't smooth them and they create false
  Appeared/Disappeared alerts.
- Insight thresholds (`DOD_PCT_THRESHOLD`, `ANOMALY_MULTIPLIER`, etc.).

Schedule lives in `tofu/main.tf` as `var.schedule_expression`:

```hcl
variable "schedule_expression" {
  default = "cron(30 3 * * ? *)"  # 03:30 UTC = 09:00 IST
}
```

## Design notes

**Why zip, not a container image.** Container images have a 10 GB cap but
add ECR, image tagging, and cold-start weight. A trimmed zip with
cross-platform wheels is smaller, simpler, and quicker to deploy.

**Why Block Kit, not charts.** matplotlib + seaborn + pandas + numpy +
pillow pushed the unzipped package over Lambda's 250 MB limit and
dominated cold-start import time. Slack Block Kit renders totals, fields,
and trend arrows natively — no images required, and the message stays
scannable without leaving Slack.

**Why one bucket.** State, the lambda zip, and reports all live together
under different prefixes. Avoids managing a separate state bucket, and
sidesteps the chicken-and-egg of storing tofu state in a tofu-managed
bucket: the bucket is bootstrapped by `deploy.sh` and intentionally not a
tofu resource.

**Why `PROJECT_PREFIX`.** AWS reserves parameter paths starting with `aws`,
so `/aws-cost-reporter-prod/slack-webhook-url` is rejected. The prefix
prepends to `local.full_name` wherever it matters (SSM, etc.) so the
project works inside orgs with that constraint.

**Why `x86_64-manylinux_2_28`.** Lambda Python 3.12 runs on Amazon Linux
2023 (glibc 2.34). Newer packages (numpy, contourpy) stopped shipping
`manylinux2014` wheels, so we target a modern glibc baseline that Lambda
satisfies.

**Why `uv pip install --no-build`.** Without it, uv falls back to building
sdists from source for the host platform (macOS arm64) when it can't find
a matching wheel — which then fails with "wheel not compatible with
target". `--no-build` surfaces the real problem (missing wheel) instead of
hiding it behind a broken build.
