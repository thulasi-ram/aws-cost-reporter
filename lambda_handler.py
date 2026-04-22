"""AWS Lambda entrypoint for the cost reporter.

Procedural flow:
  1. Read config from environment (set by the Tofu-provisioned Lambda)
  2. Resolve the Slack webhook URL from SSM
  3. Run the shared pipeline from cost_reporter.py (CE -> polars -> HTML)
  4. Persist the report-day slice to DynamoDB (history + future trend queries)
  5. Upload the HTML report to S3, generate a presigned URL (7 days)
  6. Post a Slack Block Kit message (rich tables, no images) via urllib
  7. Write the run marker to DynamoDB (audit trail of last successful run)
  8. On any failure, post a short Slack error and re-raise so the CloudWatch
     alarm fires.

The pipeline is safe to re-run for the same report day: every downstream
write (DynamoDB PutItem, S3 PutObject) overwrites, so re-invoking just
refreshes history and re-posts the same report to Slack. That is by
design — invocation means "notify", and idempotency belongs on the data
writes, not on the Slack post.

Required environment variables:
    S3_BUCKET                — bucket for the HTML report
    DYNAMODB_TABLE           — table for history + run markers
    SLACK_WEBHOOK_SSM_PARAM  — SSM parameter name holding the webhook URL
Optional:
    PRESIGNED_URL_TTL_DAYS   — default 7 (the S3 SigV4 maximum)
    ENVIRONMENT              — label shown in the Slack title (default: prod)
"""

import json
import logging
import os
import shutil
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
    TOP_N_SLACK,
    AccountSummary,
    build_account_summary,
    build_cost_dataframe,
    build_insights,
    fetch_account_map,
    fmt_delta_pct,
    fmt_usd,
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
# Slack hard limits: 2900 chars/section, 50 blocks/message.
_SECTION_LIMIT = 2900
_MAX_BLOCKS = 50
DIVIDER = {"type": "divider"}


def _mrkdwn(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _section(text: str) -> dict:
    return {"type": "section", "text": _mrkdwn(text[:_SECTION_LIMIT])}


def _dod(a: float, b: float) -> str:
    return fmt_delta_pct(a, b, arrow=True)


def _delta_emoji(a: float, b: float) -> str:
    if b == 0:
        return ":black_small_square:"
    return ":small_red_triangle:" if a > b else ":small_red_triangle_down:"


# Fixed-width column layout for the per-account monospace table.
# Total width = 26 + 1 + 11 + 1 + 8 + 1 + 11 + 1 + 8 = 68 cols.
_COL_SERVICE = 26
_COL_USD = 11
_COL_PCT = 8


def _pct_cell(a: float, b: float) -> str:
    if b == 0:
        return "—"
    return f"{(a - b) / b * 100:+.1f}%"


def _usd_cell(x: float) -> str:
    return f"${x:,.2f}"


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


def _table_row(service: str, day: float, day_before: float, avg_30d: float) -> str:
    return (
        f"{_truncate(service, _COL_SERVICE):<{_COL_SERVICE}} "
        f"{_usd_cell(day):>{_COL_USD}} "
        f"{_pct_cell(day, day_before):>{_COL_PCT}} "
        f"{_usd_cell(avg_30d):>{_COL_USD}} "
        f"{_pct_cell(day, avg_30d):>{_COL_PCT}}"
    )


def _account_table(s: AccountSummary) -> str:
    header = (
        f"{'Service':<{_COL_SERVICE}} "
        f"{'Day':>{_COL_USD}} "
        f"{'DoD':>{_COL_PCT}} "
        f"{'30d avg':>{_COL_USD}} "
        f"{'vs 30d':>{_COL_PCT}}"
    )
    width = _COL_SERVICE + 1 + _COL_USD + 1 + _COL_PCT + 1 + _COL_USD + 1 + _COL_PCT
    rule_solid = "─" * width
    rule_dotted = "·" * width
    rows = [header, rule_solid]
    for row in s.services.head(TOP_N_SLACK).iter_rows(named=True):
        rows.append(
            _table_row(
                row["service"], row["yesterday"], row["day_before"], row["avg_30d"]
            )
        )
    rows.append(rule_dotted)
    rows.append(
        _table_row(
            "TOTAL", s.total_yesterday, s.total_day_before, s.total_avg_30d
        )
    )
    return "\n".join(rows)


def _rich_preformatted(text: str) -> dict:
    """Monospace block — Slack renders rich_text_preformatted as a code block."""
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_preformatted",
                "elements": [{"type": "text", "text": text}],
            }
        ],
    }


def _slack_payload(
    summaries: list[AccountSummary],
    insights: dict[str, list[str]],
    report_date: date,
    report_url: str,
    environment: str,
) -> dict:
    """Build a Block Kit message with clear visual hierarchy.

    Structure per message:
      [header] [context metadata]
      [org totals: 4 fields] [divider]
      [insights section]     [divider]
      For each account:
        [header: account name] [context: account id]
        [rich_text_preformatted: aligned monospace table — TOTAL + top services]
      [action button: Full report]
    """
    title = f"AWS Cost Report — {report_date} (UTC)"
    if environment and environment != "prod":
        title += f" [{environment}]"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {
            "type": "context",
            "elements": [
                _mrkdwn(
                    "_AmortizedCost · excludes Tax/Refund/Credit and monthly "
                    "lump services (AWS Support)._"
                )
            ],
        },
    ]

    # --- Org totals ---
    grand_y = sum(s.total_yesterday for s in summaries)
    grand_p = sum(s.total_day_before for s in summaries)
    grand_30 = sum(s.total_avg_30d for s in summaries)
    grand_90 = sum(s.total_avg_90d for s in summaries)
    blocks.append(
        {
            "type": "section",
            "fields": [
                _mrkdwn(f"*Org total (Day)*\n{fmt_usd(grand_y)}"),
                _mrkdwn(
                    f"*DoD* {_delta_emoji(grand_y, grand_p)}\n"
                    f"{_dod(grand_y, grand_p)}  _was {fmt_usd(grand_p)}_"
                ),
                _mrkdwn(
                    f"*30d avg*\n{fmt_usd(grand_30)}  "
                    f"_({_dod(grand_y, grand_30)})_"
                ),
                _mrkdwn(f"*90d avg*\n{fmt_usd(grand_90)}"),
            ],
        }
    )
    blocks.append(DIVIDER)

    # --- Insights ---
    noisy = [s for s in summaries if insights.get(s.account_id)]
    if noisy:
        blocks.append(
            {
                "type": "section",
                "text": _mrkdwn(":mag: *Insights*"),
            }
        )
        for s in noisy:
            bullets = [f"• {note}" for note in insights[s.account_id][:6]]
            blocks.append(
                _section(f"*{s.account_name}*\n" + "\n".join(bullets))
            )
    else:
        blocks.append(
            _section(
                ":white_check_mark: *Insights*\n"
                "_Nothing unusual across any account today._"
            )
        )
    blocks.append(DIVIDER)

    # --- Per-account table ---
    for s in summaries:
        blocks.append(
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{s.account_name} ({s.account_id})",
                },
            }
        )
        blocks.append(_rich_preformatted(_account_table(s)))

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open full report"},
                    "url": report_url,
                    "style": "primary",
                }
            ],
        }
    )

    return {"blocks": blocks[:_MAX_BLOCKS]}


def _post_slack(webhook_url: str, payload: dict) -> None:
    """POST a Block Kit payload to a Slack incoming webhook.

    urllib raises HTTPError for non-2xx, so the previous `resp.status >= 300`
    check was unreachable. Catch HTTPError explicitly and include Slack's
    response body in the log so invalid-blocks / no-such-channel surface
    loudly instead of silently.
    """
    host = webhook_url.split("/")[2] if "://" in webhook_url else webhook_url
    data = json.dumps(payload).encode("utf-8")
    logger.info("Posting Slack payload (%d bytes, %d blocks) to %s",
                len(data), len(payload.get("blocks", [])), host)
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            logger.info("Slack responded %d: %s", resp.status, body[:200])
            # Slack webhooks return "ok" on success. Anything else is a warning
            # (e.g. "invalid_blocks") returned as a 200 — surface it.
            if body.strip() != "ok":
                raise RuntimeError(f"Slack webhook non-ok response: {body!r}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Slack webhook HTTP {e.code}: {body!r}"
        ) from e


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
                _section(f"```{type(err).__name__}: {err}```"),
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
        # Lambda's only writable location is /tmp. Wipe per-invocation so warm
        # containers don't leak artifacts between runs.
        out_dir = Path("/tmp/report")
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)

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

        summaries.sort(key=lambda s: s.total_yesterday, reverse=True)
        report_path = write_report(summaries, insights, report_date, out_dir, df)

        # --- Persist raw data to DynamoDB (unfiltered: includes lump services) ---
        _persist_history(cfg["ddb_table"], df, report_date)

        # --- Upload HTML report to S3 and presign ---
        report_key = f"reports/{report_date.isoformat()}/report.html"
        _upload_artifact(cfg["s3_bucket"], report_key, report_path, "text/html")
        ttl_seconds = cfg["ttl_days"] * 24 * 3600
        report_url = _presign(cfg["s3_bucket"], report_key, ttl_seconds)

        # --- Post to Slack ---
        payload = _slack_payload(
            summaries, insights, report_date, report_url, cfg["environment"]
        )
        _post_slack(webhook_url, payload)

        # --- Audit marker (updates report_url on every successful run) ---
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
