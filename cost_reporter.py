#!/usr/bin/env python3
"""AWS Cost Reporter — Phase 1 (local, disk-only).

Pulls daily cost data from Cost Explorer for all accounts under an AWS
Organization (from the payer account), computes daily/monthly/quarterly
comparisons per account, renders per-account stacked-bar charts, and writes
a combined markdown report to disk.

Run:
    uv sync
    AWS_PROFILE=root uv run cost_reporter.py
    # or override the report day:
    uv run cost_reporter.py --date 2026-04-01 --output-dir ./out
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
import matplotlib

matplotlib.use("Agg")  # headless — must be set before pyplot import
import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402
import seaborn as sns  # noqa: E402

# Module-level chart theme — one place to own the aesthetic.
sns.set_theme(style="white", context="notebook")
matplotlib.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "axes.titlesize": 15,
        "axes.titleweight": "semibold",
        "axes.titlepad": 14,
        "axes.titlecolor": "#222222",
        "axes.labelsize": 11,
        "axes.labelcolor": "#3a3a3a",
        "axes.edgecolor": "#cccccc",
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "xtick.color": "#555555",
        "ytick.color": "#555555",
        "xtick.labelsize": 11,
        "ytick.labelsize": 10,
        "legend.frameon": False,
        "legend.fontsize": 9,
        "grid.color": "#eeeeee",
        "grid.linewidth": 0.8,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "savefig.dpi": 140,
    }
)

# -----------------------------------------------------------------------------
# Config (tweak freely — kept at top for visibility)
# -----------------------------------------------------------------------------
LOOKBACK_DAYS = 95              # covers 90-day avg + small buffer
TOP_N_CHART = 10                # services shown in the per-account chart
TOP_N_TABLE = 15                # services shown in the per-account table
EXCLUDED_RECORD_TYPES = ["Tax", "Refund", "Credit"]
CE_REGION = "us-east-1"         # Cost Explorer is served from us-east-1

# Services that AWS bills as a single monthly lump instead of spreading daily.
# AmortizedCost does NOT smooth these (unlike RIs/SPs), so they create false
# "Appeared"/"Disappeared" alerts and skew the 30d avg row. Pulled out of the
# main view and reported in a separate per-account block. Extend as you
# discover more. SSO and Route 53 domains are other candidates worth watching.
MONTHLY_LUMP_SERVICES = {
    "AWS Support (Developer)",
    "AWS Support (Business)",
    "AWS Support (Enterprise)",
}

# Hand-picked strong categorical palette — Carto "Bold" extended with "Prism".
# Designed for categorical data (high saturation, distinguishable hues). No
# extra dependency, just good design choices. Cycles if the org's top-N cross
# the palette size.
STRONG_PALETTE = [
    "#7F3C8D", "#11A579", "#3969AC", "#F2B701", "#E73F74",
    "#80BA5A", "#E68310", "#008695", "#CF1C90", "#F97B72",
    "#4B4B8F", "#A5AA99",
    "#1D6996", "#38A6A5", "#0F8554", "#73AF48",
    "#EDAD08", "#E17C05", "#CC503E", "#94346E",
]
OTHER_COLOR = "#BDBDBD"

# Insights thresholds
DOD_PCT_THRESHOLD = 30.0        # flag DoD moves larger than this %
DOD_ABS_THRESHOLD_USD = 5.0     # ...and larger than this $ amount
ANOMALY_MULTIPLIER = 2.0        # yesterday > N× 30d avg
ANOMALY_MIN_USD = 5.0
NEW_SERVICE_MIN_USD = 1.0
DROPPED_SERVICE_MIN_AVG_USD = 1.0

logger = logging.getLogger("cost_reporter")


# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------
def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AWS Cost Reporter (Phase 1, disk-only)")
    p.add_argument(
        "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "./report"),
        help="Where to write report.md and charts/ (default: ./report)",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Override report day (UTC, YYYY-MM-DD). Defaults to T-2.",
    )
    return p.parse_args()


def resolve_report_day(override: str | None) -> date:
    """Default to T-2 UTC (last finalized CE day)."""
    if override:
        return date.fromisoformat(override)
    return datetime.now(timezone.utc).date() - timedelta(days=2)


# -----------------------------------------------------------------------------
# AWS: Organizations + Cost Explorer
# -----------------------------------------------------------------------------
def fetch_account_map() -> dict[str, str]:
    """Return {account_id: account_name} for all ACTIVE accounts in the org."""
    logger.info("Fetching account map from Organizations")
    org = boto3.client("organizations")
    accounts: dict[str, str] = {}
    for page in org.get_paginator("list_accounts").paginate():
        for acct in page["Accounts"]:
            if acct.get("Status") == "ACTIVE":
                accounts[acct["Id"]] = acct["Name"]
    logger.info("Found %d active accounts", len(accounts))
    return accounts


def _ec2_other_category(usage_type: str) -> str:
    """Map an EC2-Other usage type to one of three display categories."""
    ut = usage_type.upper()
    if "EBS:" in ut:
        return "EC2 - Other (EBS Volumes)"
    if "DATATRANSFER" in ut:
        return "EC2 - Other (Data Transfers)"
    return "EC2 - Other (Misc)"


def fetch_cost_data(start: date, end_exclusive: date) -> list[dict]:
    """Fetch daily AmortizedCost grouped by (LINKED_ACCOUNT, SERVICE).

    Returns one flat row per (date, account_id, service). CE pagination is
    handled here. Tax / Refund / Credit record types are filtered out so they
    don't pollute the main view.
    """
    logger.info("Fetching CE data %s -> %s (exclusive)", start, end_exclusive)
    ce = boto3.client("ce", region_name=CE_REGION)
    rows: list[dict] = []
    next_token: str | None = None
    page_no = 0
    while True:
        page_no += 1
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
            Granularity="DAILY",
            Metrics=["AmortizedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
            Filter={
                "Not": {
                    "Dimensions": {
                        "Key": "RECORD_TYPE",
                        "Values": EXCLUDED_RECORD_TYPES,
                    }
                }
            },
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        for result in resp["ResultsByTime"]:
            day = date.fromisoformat(result["TimePeriod"]["Start"])
            for group in result["Groups"]:
                account_id, service = group["Keys"]
                amount = float(group["Metrics"]["AmortizedCost"]["Amount"])
                rows.append(
                    {
                        "date": day,
                        "account_id": account_id,
                        "service": service,
                        "cost": amount,
                    }
                )
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    logger.info("Fetched %d CE rows across %d page(s)", len(rows), page_no)
    return rows


def to_polars(rows: list[dict]) -> pl.DataFrame:
    schema = {
        "date": pl.Date,
        "account_id": pl.Utf8,
        "service": pl.Utf8,
        "cost": pl.Float64,
    }
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema)


def fetch_ec2_other_breakdown(start: date, end_exclusive: date) -> list[dict]:
    """Fetch daily AmortizedCost for EC2 - Other broken down by USAGE_TYPE.

    Returns rows already mapped to three service-name categories so the caller
    can concat them into the main DataFrame after dropping 'EC2 - Other'.
    """
    logger.info("Fetching EC2-Other breakdown %s -> %s (exclusive)", start, end_exclusive)
    ce = boto3.client("ce", region_name=CE_REGION)
    rows: list[dict] = []
    next_token: str | None = None
    page_no = 0
    while True:
        page_no += 1
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
            Granularity="DAILY",
            Metrics=["AmortizedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            Filter={
                "And": [
                    {
                        "Dimensions": {
                            "Key": "SERVICE",
                            "Values": ["EC2 - Other"],
                        }
                    },
                    {
                        "Not": {
                            "Dimensions": {
                                "Key": "RECORD_TYPE",
                                "Values": EXCLUDED_RECORD_TYPES,
                            }
                        }
                    },
                ]
            },
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        for result in resp["ResultsByTime"]:
            day = date.fromisoformat(result["TimePeriod"]["Start"])
            for group in result["Groups"]:
                account_id, usage_type = group["Keys"]
                amount = float(group["Metrics"]["AmortizedCost"]["Amount"])
                rows.append(
                    {
                        "date": day,
                        "account_id": account_id,
                        "service": _ec2_other_category(usage_type),
                        "cost": amount,
                    }
                )
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    logger.info("Fetched %d EC2-Other breakdown rows across %d page(s)", len(rows), page_no)
    return rows


def build_cost_dataframe(start: date, end_exclusive: date) -> pl.DataFrame:
    """Fetch CE data and return a unified DataFrame.

    EC2 - Other is replaced with three categorized sub-rows:
    EBS Volumes, Data Transfers, and Misc.
    """
    rows = fetch_cost_data(start, end_exclusive)
    df = to_polars(rows)
    ec2_rows = fetch_ec2_other_breakdown(start, end_exclusive)
    if ec2_rows:
        df = df.filter(pl.col("service") != "EC2 - Other")
        df = pl.concat([df, to_polars(ec2_rows)])
    return df


# -----------------------------------------------------------------------------
# Per-account summary
# -----------------------------------------------------------------------------
@dataclass
class AccountSummary:
    account_id: str
    account_name: str
    report_date: date
    # Per-service table, sorted by yesterday cost desc. EXCLUDES lump services
    # (AWS Support etc.) — they are dropped up front because AmortizedCost does
    # not spread them, which would otherwise skew the 30d avg row and trip
    # false Appeared/Disappeared insights.
    # Columns: service, yesterday, day_before, avg_30d, avg_90d, hist_30d_sum
    # hist_30d_sum is the T-30..T-1 window (excludes report_date) and is used
    # purely for "appeared / disappeared" presence detection.
    services: pl.DataFrame
    total_yesterday: float
    total_day_before: float
    total_avg_30d: float
    total_avg_90d: float


def build_account_summary(
    df: pl.DataFrame,
    account_id: str,
    account_name: str,
    report_date: date,
) -> AccountSummary:
    """Aggregate one account's cost frame into an AccountSummary."""
    empty_services = pl.DataFrame(
        schema={
            "service": pl.Utf8,
            "yesterday": pl.Float64,
            "day_before": pl.Float64,
            "avg_30d": pl.Float64,
            "avg_90d": pl.Float64,
            "hist_30d_sum": pl.Float64,
        }
    )
    # Drop lumpy monthly services before any aggregation (see dataclass note).
    acct = df.filter(
        (pl.col("account_id") == account_id)
        & (~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
    )
    if acct.is_empty():
        return AccountSummary(
            account_id=account_id,
            account_name=account_name,
            report_date=report_date,
            services=empty_services,
            total_yesterday=0.0,
            total_day_before=0.0,
            total_avg_30d=0.0,
            total_avg_90d=0.0,
        )

    day_before = report_date - timedelta(days=1)
    start_30d = report_date - timedelta(days=29)  # inclusive 30-day window
    start_90d = report_date - timedelta(days=89)  # inclusive 90-day window
    hist_start = report_date - timedelta(days=30)  # T-30..T-1 (excludes today)

    yesterday = (
        acct.filter(pl.col("date") == report_date)
        .group_by("service")
        .agg(pl.col("cost").sum().alias("yesterday"))
    )
    prev_day = (
        acct.filter(pl.col("date") == day_before)
        .group_by("service")
        .agg(pl.col("cost").sum().alias("day_before"))
    )
    avg_30 = (
        acct.filter((pl.col("date") >= start_30d) & (pl.col("date") <= report_date))
        .group_by("service")
        .agg((pl.col("cost").sum() / 30.0).alias("avg_30d"))
    )
    avg_90 = (
        acct.filter((pl.col("date") >= start_90d) & (pl.col("date") <= report_date))
        .group_by("service")
        .agg((pl.col("cost").sum() / 90.0).alias("avg_90d"))
    )
    hist_30d = (
        acct.filter((pl.col("date") >= hist_start) & (pl.col("date") <= day_before))
        .group_by("service")
        .agg(pl.col("cost").sum().alias("hist_30d_sum"))
    )

    services = (
        yesterday.join(prev_day, on="service", how="full", coalesce=True)
        .join(avg_30, on="service", how="full", coalesce=True)
        .join(avg_90, on="service", how="full", coalesce=True)
        .join(hist_30d, on="service", how="full", coalesce=True)
        .fill_null(0.0)
        .filter(
            (pl.col("yesterday") != 0)
            | (pl.col("day_before") != 0)
            | (pl.col("avg_30d") != 0)
            | (pl.col("avg_90d") != 0)
        )
        .sort("yesterday", descending=True)
    )

    return AccountSummary(
        account_id=account_id,
        account_name=account_name,
        report_date=report_date,
        services=services,
        total_yesterday=float(services["yesterday"].sum()),
        total_day_before=float(services["day_before"].sum()),
        total_avg_30d=float(services["avg_30d"].sum()),
        total_avg_90d=float(services["avg_90d"].sum()),
    )


# -----------------------------------------------------------------------------
# Insights (rule-based, no LLM)
# -----------------------------------------------------------------------------
def build_insights(summary: AccountSummary) -> list[str]:
    """Return bullet points surfacing interesting movements for an account.

    Returns an empty list when nothing noteworthy is found, so the merged
    top-of-report section can skip quiet accounts entirely.
    """
    notes: list[str] = []
    if summary.services.is_empty() or summary.total_yesterday == 0:
        return notes

    for row in summary.services.iter_rows(named=True):
        svc = row["service"]
        y = row["yesterday"]
        d = row["day_before"]
        avg30 = row["avg_30d"]

        # DoD movement
        if d > 0:
            pct = (y - d) / d * 100
            if abs(pct) >= DOD_PCT_THRESHOLD and abs(y - d) >= DOD_ABS_THRESHOLD_USD:
                arrow = "up" if pct > 0 else "down"
                notes.append(
                    f"**DoD {arrow} {abs(pct):.0f}%** — {svc}: "
                    f"${d:,.2f} -> ${y:,.2f}"
                )

        # Anomaly vs 30d baseline
        if y >= ANOMALY_MIN_USD and avg30 > 0 and y >= ANOMALY_MULTIPLIER * avg30:
            notes.append(
                f"**Anomaly** — {svc}: ${y:,.2f} is "
                f"{y / avg30:.1f}x its 30d avg (${avg30:,.2f})"
            )

        # Presence changes vs the T-30..T-1 historical window (excludes today)
        hist_sum = row["hist_30d_sum"]
        hist_avg = hist_sum / 30.0
        if hist_sum < 0.01 and y >= NEW_SERVICE_MIN_USD:
            notes.append(
                f"**Appeared** — {svc}: ${y:,.2f} today "
                "(no spend in previous 30d)"
            )
        elif y < 0.01 and hist_avg >= DROPPED_SERVICE_MIN_AVG_USD:
            notes.append(
                f"**Disappeared** — {svc}: was ${hist_avg:,.2f}/day "
                "in previous 30d, now ~$0"
            )

    return notes


# -----------------------------------------------------------------------------
# Chart rendering
# -----------------------------------------------------------------------------
def build_service_palette(summaries: list[AccountSummary]) -> dict[str, str]:
    """Assign a stable color per service across all accounts.

    Any service that appears in *any* account's top-N chart slot gets a color
    from STRONG_PALETTE, ordered by descending total yesterday spend so the
    biggest services land on the first (most distinct) slots. The palette
    cycles if there are more services than slots. 'Other' is a fixed gray.
    """
    totals: dict[str, float] = {}
    for s in summaries:
        for row in s.services.head(TOP_N_CHART).iter_rows(named=True):
            totals[row["service"]] = (
                totals.get(row["service"], 0.0) + row["yesterday"]
            )
    ranked = sorted(totals.keys(), key=lambda k: -totals[k])
    color_map: dict[str, str] = {
        svc: STRONG_PALETTE[i % len(STRONG_PALETTE)]
        for i, svc in enumerate(ranked)
    }
    color_map["Other"] = OTHER_COLOR
    return color_map


def render_chart(
    summary: AccountSummary,
    charts_dir: Path,
    color_map: dict[str, str],
) -> Path:
    """Render a stacked bar: 3 bars (Yesterday / 30d avg / 90d avg).

    Stack segments are the top-N services by yesterday cost, plus "Other".
    """
    charts_dir.mkdir(parents=True, exist_ok=True)
    path = charts_dir / f"{summary.account_id}.png"

    if summary.services.is_empty() or summary.total_yesterday == 0:
        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.text(
            0.5,
            0.5,
            f"No cost data for {summary.account_name}",
            ha="center",
            va="center",
            transform=ax.transAxes,
            color="#888888",
            fontsize=12,
        )
        ax.set_axis_off()
        fig.savefig(path)
        plt.close(fig)
        return path

    # Only the columns we actually plot — keeps schema alignment simple.
    chart_cols = ["service", "yesterday", "avg_30d", "avg_90d"]
    top = summary.services.head(TOP_N_CHART).select(chart_cols)
    rest = summary.services.slice(TOP_N_CHART)
    if rest.height > 0:
        other_df = pl.DataFrame(
            {
                "service": ["Other"],
                "yesterday": [float(rest["yesterday"].sum())],
                "avg_30d": [float(rest["avg_30d"].sum())],
                "avg_90d": [float(rest["avg_90d"].sum())],
            }
        )
        chart_df = pl.concat([top, other_df])
    else:
        chart_df = top

    categories = ["Yesterday", "30d avg", "90d avg"]
    value_cols = ["yesterday", "avg_30d", "avg_90d"]

    fig, ax = plt.subplots(figsize=(11, 6.5))
    bottoms = [0.0, 0.0, 0.0]
    for row in chart_df.iter_rows(named=True):
        svc = row["service"]
        heights = [row[c] for c in value_cols]
        ax.bar(
            categories,
            heights,
            bottom=bottoms,
            color=color_map.get(svc, "#999999"),
            label=svc,
            width=0.55,
            edgecolor="white",
            linewidth=0.8,
        )
        bottoms = [b + h for b, h in zip(bottoms, heights)]

    # Total label on top of each bar — makes the chart scannable without
    # squinting at the y-axis.
    max_total = max(bottoms) if bottoms else 0
    offset = max_total * 0.015
    for i, total in enumerate(bottoms):
        ax.text(
            i,
            total + offset,
            f"${total:,.0f}",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="semibold",
            color="#222222",
        )

    ax.set_ylabel("USD")
    ax.set_ylim(0, max_total * 1.12 if max_total > 0 else 1)
    ax.set_title(
        f"{summary.account_name}  ·  {summary.account_id}  ·  "
        f"{summary.report_date} UTC",
        loc="left",
    )
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"${x:,.0f}")
    )
    ax.legend(
        bbox_to_anchor=(1.02, 1),
        loc="upper left",
        borderaxespad=0,
        handlelength=1.2,
        handleheight=1.2,
    )
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


# -----------------------------------------------------------------------------
# Report writing
# -----------------------------------------------------------------------------
def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def _fmt_delta_pct(a: float, b: float) -> str:
    if b == 0:
        return "—"
    return f"{(a - b) / b * 100:+.1f}%"


def write_report(
    summaries: list[AccountSummary],
    insights: dict[str, list[str]],
    chart_paths: dict[str, Path],
    report_date: date,
    out_dir: Path,
) -> Path:
    lines: list[str] = []
    lines.append(f"# AWS Cost Report — {report_date} (UTC)")
    lines.append("")
    lines.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )
    lines.append("")
    lines.append(
        "_Metric: AmortizedCost. Excluded record types: "
        f"{', '.join(EXCLUDED_RECORD_TYPES)}. "
        "Monthly lump services excluded from the daily view: "
        f"{', '.join(sorted(MONTHLY_LUMP_SERVICES))}._"
    )
    lines.append("")

    # Merged insights across all accounts — quiet accounts are skipped so the
    # section surfaces only what is actually worth looking at.
    lines.append("## Insights")
    lines.append("")
    noisy_accounts = [s for s in summaries if insights.get(s.account_id)]
    if not noisy_accounts:
        lines.append("_Nothing unusual across any account today._")
        lines.append("")
    else:
        for s in noisy_accounts:
            lines.append(f"**{s.account_name}** (`{s.account_id}`)")
            lines.append("")
            for note in insights[s.account_id]:
                lines.append(f"- {note}")
            lines.append("")

    # Org-wide summary table
    lines.append("## Summary across accounts")
    lines.append("")
    lines.append("| Account | Day | Day Before | 30d Avg | 90d Avg | Δ% DoD |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for s in summaries:
        lines.append(
            f"| {s.account_name} (`{s.account_id}`) "
            f"| {_fmt_usd(s.total_yesterday)} "
            f"| {_fmt_usd(s.total_day_before)} "
            f"| {_fmt_usd(s.total_avg_30d)} "
            f"| {_fmt_usd(s.total_avg_90d)} "
            f"| {_fmt_delta_pct(s.total_yesterday, s.total_day_before)} |"
        )
    grand_y = sum(s.total_yesterday for s in summaries)
    grand_p = sum(s.total_day_before for s in summaries)
    grand_30 = sum(s.total_avg_30d for s in summaries)
    grand_90 = sum(s.total_avg_90d for s in summaries)
    lines.append(
        f"| **TOTAL** "
        f"| **{_fmt_usd(grand_y)}** "
        f"| **{_fmt_usd(grand_p)}** "
        f"| **{_fmt_usd(grand_30)}** "
        f"| **{_fmt_usd(grand_90)}** "
        f"| **{_fmt_delta_pct(grand_y, grand_p)}** |"
    )
    lines.append("")

    # Per-account sections
    for s in summaries:
        lines.append(f"## {s.account_name} (`{s.account_id}`)")
        lines.append("")
        rel_chart = chart_paths[s.account_id].relative_to(out_dir)
        lines.append(f"![chart]({rel_chart})")
        lines.append("")
        lines.append(
            f"**Day:** {_fmt_usd(s.total_yesterday)}  "
            f"**Day Before:** {_fmt_usd(s.total_day_before)}  "
            f"**30d Avg:** {_fmt_usd(s.total_avg_30d)}  "
            f"**90d Avg:** {_fmt_usd(s.total_avg_90d)}  "
            f"**DoD:** {_fmt_delta_pct(s.total_yesterday, s.total_day_before)}"
        )
        lines.append("")
        lines.append(
            "| # | Service | Day | Day Before | 30d Avg | 90d Avg "
            "| % of Day | Δ% DoD | Δ% vs 30d |"
        )
        lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|")
        table_rows = s.services.head(TOP_N_TABLE)
        for i, row in enumerate(table_rows.iter_rows(named=True), 1):
            pct_of_day = (
                f"{row['yesterday'] / s.total_yesterday * 100:.1f}%"
                if s.total_yesterday > 0
                else "—"
            )
            lines.append(
                f"| {i} | {row['service']} "
                f"| {_fmt_usd(row['yesterday'])} "
                f"| {_fmt_usd(row['day_before'])} "
                f"| {_fmt_usd(row['avg_30d'])} "
                f"| {_fmt_usd(row['avg_90d'])} "
                f"| {pct_of_day} "
                f"| {_fmt_delta_pct(row['yesterday'], row['day_before'])} "
                f"| {_fmt_delta_pct(row['yesterday'], row['avg_30d'])} |"
            )
        lines.append("")

    path = out_dir / "report.md"
    path.write_text("\n".join(lines))
    return path


# -----------------------------------------------------------------------------
# Main (procedural flow)
# -----------------------------------------------------------------------------
def main() -> int:
    setup_logging()
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    charts_dir = out_dir / "charts"

    rpt_date = resolve_report_day(args.date)
    start = rpt_date - timedelta(days=LOOKBACK_DAYS - 1)
    end_exclusive = rpt_date + timedelta(days=1)
    logger.info("Report day: %s UTC (window %s -> %s inclusive)", rpt_date, start, rpt_date)

    accounts = fetch_account_map()
    df = build_cost_dataframe(start, end_exclusive)

    # Pass 1: build every account summary so we can derive a stable palette
    summaries: list[AccountSummary] = []
    insights: dict[str, list[str]] = {}
    for acct_id in sorted(accounts):
        name = accounts[acct_id]
        logger.info("Processing %s (%s)", name, acct_id)
        summary = build_account_summary(df, acct_id, name, rpt_date)
        summaries.append(summary)
        insights[acct_id] = build_insights(summary)

    # Pass 2: render charts with a palette that is consistent across accounts
    color_map = build_service_palette(summaries)
    chart_paths: dict[str, Path] = {
        s.account_id: render_chart(s, charts_dir, color_map) for s in summaries
    }

    # Sort report sections by yesterday cost desc (biggest spenders first)
    summaries.sort(key=lambda s: s.total_yesterday, reverse=True)

    report_path = write_report(summaries, insights, chart_paths, rpt_date, out_dir)
    logger.info("Wrote %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
