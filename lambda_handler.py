"""AWS Lambda entrypoint for the cost reporter (Phase 2).

Procedural flow:
  1. Read config from environment (set by the Tofu-provisioned Lambda)
  2. Resolve the Slack webhook URL from SSM
  3. Idempotency check: skip if this report day already succeeded
  4. Run the shared pipeline from cost_reporter.py (CE -> polars -> charts + md)
  5. Persist the report-day slice to DynamoDB (history + future trend queries)
  6. Upload markdown + charts to S3, generate presigned URLs (7 days)
  7. Post a Slack Blocks message via urllib (no slack-sdk)
  8. Write the run marker to DynamoDB so re-invocations are no-ops
  9. On any failure, post a short Slack error and re-raise so the CloudWatch
     alarm fires.

Required environment variables:
    S3_BUCKET                — bucket for charts + markdown report
    DYNAMODB_TABLE           — table for history + run markers
    SLACK_WEBHOOK_SSM_PARAM  — SSM parameter name holding the webhook URL
Optional:
    PRESIGNED_URL_TTL_DAYS   — default 7 (the S3 SigV4 maximum)
    ENVIRONMENT              — label shown in the Slack title (default: prod)
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import polars as pl

from cost_reporter import (
    LOOKBACK_DAYS,
    AccountSummary,
    build_account_summary,
    build_cost_dataframe,
    build_insights,
    build_service_palette,
    fetch_account_map,
    render_chart,
    resolve_report_day,
    write_report,
)

logger = logging.getLogger("cost_reporter.lambda")
logger.setLevel(logging.INFO)

# Clients created at module scope so they are reused across warm invocations.
_S3 = boto3.client("s3")
_DDB = boto3.resource("dynamodb")
_SSM = boto3.client("ssm")


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
def _env(name: str, default: str | None = None, required: bool = True) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val  # type: ignore[return-value]


def _load_config() -> dict[str, Any]:
    return {
        "s3_bucket": _env("S3_BUCKET"),
        "ddb_table": _env("DYNAMODB_TABLE"),
        "ssm_param": _env("SLACK_WEBHOOK_SSM_PARAM"),
        "ttl_days": int(_env("PRESIGNED_URL_TTL_DAYS", "7", required=False)),
        "environment": _env("ENVIRONMENT", "prod", required=False),
    }


def _slack_webhook_url(ssm_param: str) -> str:
    resp = _SSM.get_parameter(Name=ssm_param, WithDecryption=True)
    return resp["Parameter"]["Value"]


# -----------------------------------------------------------------------------
# DynamoDB: run markers + history persistence
# -----------------------------------------------------------------------------
def _already_ran(table_name: str, report_date: date) -> bool:
    table = _DDB.Table(table_name)
    resp = table.get_item(Key={"pk": "run", "sk": report_date.isoformat()})
    return resp.get("Item", {}).get("status") == "success"


def _mark_run(table_name: str, report_date: date, report_url: str) -> None:
    table = _DDB.Table(table_name)
    ttl = int((datetime.now(timezone.utc) + timedelta(days=400)).timestamp())
    table.put_item(
        Item={
            "pk": "run",
            "sk": report_date.isoformat(),
            "status": "success",
            "report_url": report_url,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": ttl,
        }
    )


def _persist_history(table_name: str, df: pl.DataFrame, report_date: date) -> None:
    """Write the report-day slice to DynamoDB.

    Idempotent — PutItem overwrites on re-runs. Over time this accumulates a
    longer history than CE retains cheaply, which is what unlocks the Phase 2+
    long-range trend work.
    """
    slice_df = df.filter(pl.col("date") == report_date)
    if slice_df.is_empty():
        logger.info("No rows to persist for %s", report_date)
        return

    table = _DDB.Table(table_name)
    ttl = int((datetime.now(timezone.utc) + timedelta(days=400)).timestamp())
    ingested_at = datetime.now(timezone.utc).isoformat()
    written = 0
    with table.batch_writer() as batch:
        for row in slice_df.iter_rows(named=True):
            batch.put_item(
                Item={
                    "pk": row["account_id"],
                    "sk": f"{row['date'].isoformat()}#{row['service']}",
                    "date": row["date"].isoformat(),
                    "service": row["service"],
                    "cost": Decimal(f"{row['cost']:.6f}"),
                    "ingested_at": ingested_at,
                    "expires_at": ttl,
                }
            )
            written += 1
    logger.info("Persisted %d history rows to %s", written, table_name)


# -----------------------------------------------------------------------------
# S3: upload + presign
# -----------------------------------------------------------------------------
def _upload_artifact(
    bucket: str, key: str, path: Path, content_type: str
) -> None:
    _S3.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "ServerSideEncryption": "AES256",
        },
    )


def _presign(bucket: str, key: str, ttl_seconds: int) -> str:
    return _S3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl_seconds,
    )


# -----------------------------------------------------------------------------
# Slack formatting + POST
# -----------------------------------------------------------------------------
def _dod(a: float, b: float) -> str:
    if b == 0:
        return "—"
    pct = (a - b) / b * 100
    arrow = "↑" if pct > 0 else "↓"
    return f"{arrow}{abs(pct):.1f}%"


def _slack_payload(
    summaries: list[AccountSummary],
    insights: dict[str, list[str]],
    report_date: date,
    report_url: str,
    chart_urls: dict[str, str],
    environment: str,
) -> dict:
    """Build a Slack Blocks payload that fits inside the incoming-webhook limits.

    Slack caps each section text at 3000 chars and the total message at 50
    blocks, so we truncate defensively.
    """
    title = f"AWS Cost Report — {report_date} (UTC)"
    if environment and environment != "prod":
        title += f" [{environment}]"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_AmortizedCost. Excludes Tax/Refund/Credit and "
                        "monthly lump services (AWS Support)._"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]

    # Insights section — merged across accounts, quiet accounts skipped
    noisy = [s for s in summaries if insights.get(s.account_id)]
    if noisy:
        insight_lines = ["*Insights*"]
        for s in noisy:
            insight_lines.append(f"*{s.account_name}*")
            for note in insights[s.account_id][:6]:
                insight_lines.append(f"• {note}")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(insight_lines)[:2900]},
            }
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Insights*\n_Nothing unusual across any account today._",
                },
            }
        )
    blocks.append({"type": "divider"})

    # Summary across accounts (compact one-line-per-account format)
    summary_lines = ["*Summary* _(Yesterday · 30d avg · 90d avg · Δ DoD)_"]
    for s in summaries:
        summary_lines.append(
            f"• `{s.account_name}`  "
            f"${s.total_yesterday:,.2f}  ·  "
            f"${s.total_avg_30d:,.2f}  ·  "
            f"${s.total_avg_90d:,.2f}  ·  "
            f"{_dod(s.total_yesterday, s.total_day_before)}"
        )
    grand_y = sum(s.total_yesterday for s in summaries)
    grand_p = sum(s.total_day_before for s in summaries)
    summary_lines.append(
        f"*Total*: ${grand_y:,.2f}  ({_dod(grand_y, grand_p)} DoD)"
    )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(summary_lines)[:2900]},
        }
    )

    # Chart links (one per account)
    if chart_urls:
        chart_lines = ["*Charts*"]
        for s in summaries:
            url = chart_urls.get(s.account_id)
            if url:
                chart_lines.append(f"• <{url}|{s.account_name}>")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(chart_lines)[:2900]},
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Full report"},
                    "url": report_url,
                }
            ],
        }
    )

    return {"blocks": blocks}


def _post_slack(webhook_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(
                f"Slack webhook returned {resp.status}: {resp.read()!r}"
            )


def _post_slack_error(webhook_url: str, err: Exception, report_date: date) -> None:
    """Best-effort error notification. Swallows its own failures."""
    try:
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"AWS cost report failed ({report_date})",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"```{type(err).__name__}: {err}```",
                    },
                },
            ]
        }
        _post_slack(webhook_url, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to post error notification to Slack")


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
def handler(event: dict, context: object) -> dict:
    cfg = _load_config()
    webhook_url = _slack_webhook_url(cfg["ssm_param"])

    # Allow an event-level date override for ad-hoc re-runs via aws lambda invoke
    date_override = event.get("date") if isinstance(event, dict) else None
    report_date = resolve_report_day(date_override)

    try:
        if _already_ran(cfg["ddb_table"], report_date):
            logger.info(
                "Report for %s already succeeded — exiting idempotently",
                report_date,
            )
            return {"status": "skipped", "report_date": report_date.isoformat()}

        # Lambda's only writable location is /tmp. Wipe it per invocation so
        # warm containers don't leak artifacts between runs.
        out_dir = Path("/tmp/report")
        if out_dir.exists():
            for p in sorted(out_dir.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
        out_dir.mkdir(parents=True, exist_ok=True)
        charts_dir = out_dir / "charts"

        # --- Shared pipeline (same functions the CLI uses) ---
        start = report_date - timedelta(days=LOOKBACK_DAYS - 1)
        end_exclusive = report_date + timedelta(days=1)
        logger.info(
            "Generating report for %s (window %s -> %s inclusive)",
            report_date,
            start,
            report_date,
        )

        accounts = fetch_account_map()
        df = build_cost_dataframe(start, end_exclusive)

        summaries: list[AccountSummary] = []
        insights: dict[str, list[str]] = {}
        for acct_id in sorted(accounts):
            s = build_account_summary(df, acct_id, accounts[acct_id], report_date)
            summaries.append(s)
            insights[acct_id] = build_insights(s)

        color_map = build_service_palette(summaries)
        chart_paths = {
            s.account_id: render_chart(s, charts_dir, color_map) for s in summaries
        }
        summaries.sort(key=lambda s: s.total_yesterday, reverse=True)
        report_path = write_report(
            summaries, insights, chart_paths, report_date, out_dir
        )

        # --- Persist raw data to DynamoDB (unfiltered: includes lump services) ---
        _persist_history(cfg["ddb_table"], df, report_date)

        # --- Upload artifacts to S3 and presign ---
        date_prefix = f"reports/{report_date.isoformat()}"
        report_key = f"{date_prefix}/report.md"
        _upload_artifact(cfg["s3_bucket"], report_key, report_path, "text/markdown")

        chart_urls: dict[str, str] = {}
        ttl_seconds = cfg["ttl_days"] * 24 * 3600
        for acct_id, path in chart_paths.items():
            key = f"{date_prefix}/charts/{path.name}"
            _upload_artifact(cfg["s3_bucket"], key, path, "image/png")
            chart_urls[acct_id] = _presign(cfg["s3_bucket"], key, ttl_seconds)
        report_url = _presign(cfg["s3_bucket"], report_key, ttl_seconds)

        # --- Post to Slack ---
        payload = _slack_payload(
            summaries, insights, report_date, report_url, chart_urls, cfg["environment"]
        )
        _post_slack(webhook_url, payload)

        # --- Mark run successful (last step so any earlier failure retries) ---
        _mark_run(cfg["ddb_table"], report_date, report_url)

        logger.info("Report posted successfully for %s", report_date)
        return {
            "status": "success",
            "report_date": report_date.isoformat(),
            "report_url": report_url,
            "accounts": len(summaries),
        }

    except Exception as err:  # noqa: BLE001
        logger.exception("Report generation failed")
        _post_slack_error(webhook_url, err, report_date)
        raise
