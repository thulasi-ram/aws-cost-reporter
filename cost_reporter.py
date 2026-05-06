#!/usr/bin/env python3
"""AWS Cost Reporter.

Pulls daily cost data from Cost Explorer for all accounts under an AWS
Organization (from the payer account), computes daily/monthly/quarterly
comparisons per account, and writes a single-file HTML report (ApexCharts
via CDN) to disk.

Run:
    uv sync
    AWS_PROFILE=root uv run cost_reporter.py
    # or override the report day:
    uv run cost_reporter.py --date 2026-04-01 --output-dir ./out
"""

import argparse
import calendar
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
import polars as pl

# -----------------------------------------------------------------------------
# Config (tweak freely — kept at top for visibility)
# -----------------------------------------------------------------------------
LOOKBACK_DAYS = 95              # covers 90-day avg + small buffer
TOP_N_TABLE = 15                # services shown in the per-account table
TOP_N_SLACK = 5                 # services shown in the per-account Slack block
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

# Insights thresholds
DOD_PCT_THRESHOLD = 30.0        # flag DoD moves larger than this %
DOD_ABS_THRESHOLD_USD = 5.0     # ...and larger than this $ amount
ANOMALY_MULTIPLIER = 2.0        # yesterday > N× 30d avg
ANOMALY_MIN_USD = 5.0
NEW_SERVICE_MIN_USD = 1.0
DELTA_NOISE_FLOOR_USD = 0.5     # suppress DoD/vs-30d % when both sides are below this
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
    p = argparse.ArgumentParser(description="AWS Cost Reporter")
    p.add_argument(
        "--output-dir",
        default=os.environ.get("OUTPUT_DIR", "./report"),
        help="Where to write report.html (default: ./report)",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Override report day (UTC, YYYY-MM-DD). Defaults to T-1.",
    )
    return p.parse_args()


def resolve_report_day(override: str | None) -> date:
    """Default to T-1 UTC (yesterday)."""
    if override:
        return date.fromisoformat(override)
    return datetime.now(timezone.utc).date() - timedelta(days=1)


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
    # Many usage_types collapse onto the same EC2-Other category, so concat
    # leaves duplicate (date, account_id, service) rows. Aggregate here so
    # downstream consumers (DynamoDB BatchWriteItem in particular) see a
    # unique key per row.
    return (
        df.group_by(["date", "account_id", "service"])
        .agg(pl.col("cost").sum())
        .sort(["date", "account_id", "service"])
    )


# -----------------------------------------------------------------------------
# Savings Plans / Reserved Instances utilization + coverage
# -----------------------------------------------------------------------------

def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile over `values` (zeros included). 0..100."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _linear_projection(
    series: list[float], project_days: int
) -> tuple[list[float], float]:
    """Naive least-squares fit on an evenly-spaced daily series.

    Returns (projected_values, slope_per_day). Negative projections are
    clamped to 0 — on-demand spend can't go below zero.
    """
    n = len(series)
    if n < 2 or project_days <= 0:
        return [], 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, series))
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = num / den if den else 0.0
    intercept = mean_y - slope * mean_x
    proj = [max(0.0, intercept + slope * (n + i)) for i in range(project_days)]
    return proj, slope


def _fetch_sp_inventory() -> tuple[list[dict], bool]:
    """Best-effort `savingsplans:DescribeSavingsPlans` from current creds.

    SP inventory is per-account in IAM scope, so the management account only
    sees its own SPs unless cross-account roles are wired up. We return what
    is visible and a `visible` flag the caller uses to decide whether to
    show the panel or render an explanatory note.
    """
    try:
        sp = boto3.client("savingsplans", region_name=CE_REGION)
    except Exception as e:  # noqa: BLE001
        logger.warning("savingsplans client init failed: %s", e)
        return [], False
    out: list[dict] = []
    today = datetime.now(timezone.utc).date()
    try:
        next_token: str | None = None
        page = 0
        while True:
            page += 1
            kwargs = {"states": ["active"], "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = sp.describe_savings_plans(**kwargs)
            for s in resp.get("savingsPlans", []):
                end_str = s.get("end")
                end_dt = None
                days_left = None
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        ).date()
                        days_left = (end_dt - today).days
                    except ValueError:
                        pass
                out.append({
                    "plan_id": s.get("savingsPlanId", ""),
                    "type": s.get("savingsPlanType", ""),
                    "term": s.get("termDurationInSeconds", 0) // 86400,
                    "hourly": float(s.get("commitment", "0") or 0),
                    "end": end_dt.isoformat() if end_dt else "",
                    "days_left": days_left if days_left is not None else -1,
                })
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("describe_savings_plans failed: %s", e)
        return [], False
    return out, True

# Number of trailing days included in the commitments tab.
COMMIT_LOOKBACK_DAYS = 90

# Approximate AWS list discounts vs on-demand for sizing 1y/3y commitments.
# us-east-1 EC2 / Compute SP / Standard RI ranges. Treat as ballpark.
COMMIT_DISCOUNT_1Y_NU = 0.27   # Compute SP, 1y, no upfront
COMMIT_DISCOUNT_3Y_AU = 0.54   # Compute SP, 3y, all upfront
RI_DISCOUNT_1Y_NU = 0.36       # Standard EC2 RI, 1y, no upfront
RI_DISCOUNT_3Y_AU = 0.62       # Standard EC2 RI, 3y, all upfront

# Project the on-demand series this many days into the future for the chart.
COMMIT_PROJECT_DAYS = 90


@dataclass
class CommitmentSummary:
    """Org-level savings plan + reservation snapshot.

    All money figures are in USD.

    `sp_safe_buy_hourly` is a conservative suggestion for an additional
    hourly Savings Plan commitment: the minimum daily on-demand SP-eligible
    spend over the lookback window divided by 24. Buying up to that amount
    would (under current usage) keep utilization at ~100% with zero waste.
    """

    days: int
    # Daily series for charts: each entry has {date, util_pct, coverage_pct,
    # unused_commitment, on_demand_cost}
    sp_daily: list[dict]
    ri_daily: list[dict]
    # Aggregated SP metrics
    sp_avg_util_pct: float
    sp_avg_coverage_pct: float
    sp_total_commitment: float
    sp_total_unused: float
    sp_total_savings: float
    sp_total_on_demand: float  # SP-eligible spend NOT covered (= opportunity)
    sp_min_daily_on_demand: float
    sp_safe_buy_hourly: float
    sp_safe_buy_p10_hourly: float
    sp_safe_buy_p25_hourly: float
    sp_safe_buy_p50_hourly: float
    sp_avg_daily_on_demand: float
    sp_effective_discount_pct: float
    # Aggregated RI metrics
    ri_avg_util_pct: float
    ri_avg_coverage_pct: float
    ri_total_purchased_hours: float
    ri_total_unused_hours: float
    ri_total_savings: float
    ri_total_on_demand: float
    ri_total_reserved_cost: float
    ri_min_daily_on_demand: float
    ri_safe_buy_hourly: float
    ri_safe_buy_p10_hourly: float
    ri_safe_buy_p25_hourly: float
    ri_safe_buy_p50_hourly: float
    ri_avg_daily_on_demand: float
    ri_effective_discount_pct: float
    # Per-linked-account coverage rows. Each entry:
    # {account_id, sp_on_demand, sp_covered, sp_coverage_pct,
    #  ri_on_demand, ri_covered, ri_coverage_pct, ri_util_pct}
    per_account: list[dict]
    # Linear projection of SP-eligible on-demand $ over the next 90 days.
    on_demand_projection: list[dict]   # [{date, on_demand}] (future only)
    on_demand_proj_total: float        # sum of the projection window
    on_demand_trend_slope: float       # slope per day, $; >0 = growing
    # SP inventory (best-effort, per-credentials)
    sp_inventory: list[dict]
    sp_inventory_visible: bool
    sp_expiring_30d_hourly: float
    sp_expiring_60d_hourly: float
    sp_expiring_90d_hourly: float


def _safe_float(d: dict, key: str) -> float:
    """CE returns money/percent fields as strings nested under {'Amount': '...'} or raw strings."""
    v = d.get(key)
    if v is None:
        return 0.0
    if isinstance(v, dict):
        v = v.get("Amount", "0")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_sp_utilization(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    return ce.get_savings_plans_utilization(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="DAILY",
    )


def fetch_sp_coverage(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    # No GroupBy → one row per day at the org level.
    return ce.get_savings_plans_coverage(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="DAILY",
    )


def fetch_sp_coverage_by_account(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    return ce.get_savings_plans_coverage(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="MONTHLY",  # account-level needs an aggregate; daily blows up
        GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
    )


def fetch_ri_utilization(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    return ce.get_reservation_utilization(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="DAILY",
    )


def fetch_ri_utilization_by_account(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    return ce.get_reservation_utilization(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
    )


def fetch_ri_coverage(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    return ce.get_reservation_coverage(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="DAILY",
        Metrics=["Hour", "Cost"],
    )


def fetch_ri_coverage_by_account(start: date, end_exclusive: date) -> dict:
    ce = boto3.client("ce", region_name=CE_REGION)
    return ce.get_reservation_coverage(
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="MONTHLY",
        GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
        Metrics=["Hour", "Cost"],
    )


def build_commitment_summary(
    report_date: date,
    days: int = COMMIT_LOOKBACK_DAYS,
    account_map: dict[str, str] | None = None,
) -> CommitmentSummary:
    """Build a 90-day SP + RI snapshot. Errors are logged and reported as zeros.

    Most accounts will have either SPs or RIs but not both, and brand-new
    accounts have neither. We don't want a missing-permission or empty-result
    case to break the daily report, so each fetch is independent and any
    failure produces an empty section rather than a crash.
    """
    start = report_date - timedelta(days=days - 1)
    end_exclusive = report_date + timedelta(days=1)

    # --- Savings Plans ---
    sp_util_by_day: dict[str, dict] = {}
    sp_total_commit = sp_total_unused = sp_total_savings = 0.0
    try:
        u = fetch_sp_utilization(start, end_exclusive)
        for entry in u.get("SavingsPlansUtilizationsByTime", []):
            day = entry["TimePeriod"]["Start"]
            util = entry.get("Utilization", {})
            sav = entry.get("Savings", {})
            sp_util_by_day[day] = {
                "util_pct": _safe_float(util, "UtilizationPercentage"),
                "total_commitment": _safe_float(util, "TotalCommitment"),
                "unused_commitment": _safe_float(util, "UnusedCommitment"),
                "savings": _safe_float(sav, "NetSavings"),
            }
        tot = u.get("Total", {})
        sp_total_commit = _safe_float(tot.get("Utilization", {}), "TotalCommitment")
        sp_total_unused = _safe_float(tot.get("Utilization", {}), "UnusedCommitment")
        sp_total_savings = _safe_float(tot.get("Savings", {}), "NetSavings")
    except Exception as e:  # noqa: BLE001
        logger.warning("SP utilization fetch failed: %s", e)

    sp_cov_by_day: dict[str, dict] = {}
    sp_total_on_demand = 0.0
    sp_avg_cov = 0.0
    try:
        c = fetch_sp_coverage(start, end_exclusive)
        cov_pcts: list[float] = []
        for entry in c.get("CoveragesByTime", []):
            day = entry["TimePeriod"]["Start"]
            cov = entry.get("Coverage", {})
            on_demand = _safe_float(cov, "OnDemandCost")
            sp_cov_by_day[day] = {
                "coverage_pct": _safe_float(cov, "CoveragePercentage"),
                "on_demand_cost": on_demand,
            }
            cov_pcts.append(_safe_float(cov, "CoveragePercentage"))
            sp_total_on_demand += on_demand
        if cov_pcts:
            sp_avg_cov = sum(cov_pcts) / len(cov_pcts)
    except Exception as e:  # noqa: BLE001
        logger.warning("SP coverage fetch failed: %s", e)

    sp_daily: list[dict] = []
    util_pcts: list[float] = []
    on_demand_per_day: list[float] = []
    for day in sorted(set(sp_util_by_day) | set(sp_cov_by_day)):
        u_row = sp_util_by_day.get(day, {})
        c_row = sp_cov_by_day.get(day, {})
        sp_daily.append(
            {
                "date": day,
                "util_pct": u_row.get("util_pct", 0.0),
                "coverage_pct": c_row.get("coverage_pct", 0.0),
                "unused_commitment": u_row.get("unused_commitment", 0.0),
                "on_demand_cost": c_row.get("on_demand_cost", 0.0),
            }
        )
        if u_row.get("util_pct") is not None:
            util_pcts.append(u_row["util_pct"])
        if c_row.get("on_demand_cost") is not None:
            on_demand_per_day.append(c_row["on_demand_cost"])

    sp_avg_util = sum(util_pcts) / len(util_pcts) if util_pcts else 0.0
    # Floor of daily uncovered-but-eligible spend → safe headroom for new SPs.
    # Skip the most recent day if it's zero (CE often lags by ~24h on coverage).
    eligible = [v for v in on_demand_per_day if v > 0]
    sp_min_on_demand = min(eligible) if eligible else 0.0
    sp_safe_buy_hourly = sp_min_on_demand / 24.0
    # Percentile-based safe-buy: more aggressive headroom than the floor.
    # P10/P25/P50 are computed over ALL days (including zeros) so a workload
    # that is fully covered half the time correctly suggests a smaller buy.
    sp_safe_p10 = _percentile(on_demand_per_day, 10) / 24.0
    sp_safe_p25 = _percentile(on_demand_per_day, 25) / 24.0
    sp_safe_p50 = _percentile(on_demand_per_day, 50) / 24.0
    sp_avg_daily_on_demand = (
        sum(on_demand_per_day) / len(on_demand_per_day) if on_demand_per_day else 0.0
    )
    # Effective SP discount = savings / (savings + commitment paid for covered).
    sp_paid_for_covered = max(0.0, sp_total_commit - sp_total_unused)
    sp_eff_discount = (
        100.0 * sp_total_savings / (sp_total_savings + sp_paid_for_covered)
        if (sp_total_savings + sp_paid_for_covered) > 0
        else 0.0
    )

    # --- Reserved Instances ---
    ri_util_by_day: dict[str, dict] = {}
    ri_total_purchased = ri_total_unused = ri_total_savings = 0.0
    try:
        u = fetch_ri_utilization(start, end_exclusive)
        for entry in u.get("UtilizationsByTime", []):
            day = entry["TimePeriod"]["Start"]
            tot_attr = entry.get("Total", {})
            ri_util_by_day[day] = {
                "util_pct": _safe_float(tot_attr, "UtilizationPercentage"),
                "purchased_hours": _safe_float(tot_attr, "PurchasedHours"),
                "unused_hours": _safe_float(tot_attr, "UnusedHours"),
                "savings": _safe_float(tot_attr, "NetRISavings"),
            }
        tot = u.get("Total", {})
        ri_total_purchased = _safe_float(tot, "PurchasedHours")
        ri_total_unused = _safe_float(tot, "UnusedHours")
        ri_total_savings = _safe_float(tot, "NetRISavings")
    except Exception as e:  # noqa: BLE001
        logger.warning("RI utilization fetch failed: %s", e)

    ri_cov_by_day: dict[str, dict] = {}
    ri_total_on_demand = 0.0
    ri_total_reserved_cost = 0.0
    ri_avg_cov = 0.0
    try:
        c = fetch_ri_coverage(start, end_exclusive)
        cov_pcts: list[float] = []
        for entry in c.get("CoveragesByTime", []):
            day = entry["TimePeriod"]["Start"]
            tot_attr = entry.get("Total", {})
            cov = tot_attr.get("CoverageCost", {}) or tot_attr.get("Coverage", {})
            # CoverageCost has OnDemandCost / ReservedCost / TotalCost.
            on_demand = _safe_float(cov, "OnDemandCost")
            reserved_cost = _safe_float(cov, "ReservedCost")
            cov_pct_obj = tot_attr.get("CoverageHours", {}) or tot_attr.get("Coverage", {})
            cov_pct = _safe_float(cov_pct_obj, "CoverageHoursPercentage")
            ri_cov_by_day[day] = {
                "coverage_pct": cov_pct,
                "on_demand_cost": on_demand,
                "reserved_cost": reserved_cost,
            }
            cov_pcts.append(cov_pct)
            ri_total_on_demand += on_demand
            ri_total_reserved_cost += reserved_cost
        if cov_pcts:
            ri_avg_cov = sum(cov_pcts) / len(cov_pcts)
    except Exception as e:  # noqa: BLE001
        logger.warning("RI coverage fetch failed: %s", e)

    ri_daily = []
    util_pcts = []
    on_demand_per_day = []
    for day in sorted(set(ri_util_by_day) | set(ri_cov_by_day)):
        u_row = ri_util_by_day.get(day, {})
        c_row = ri_cov_by_day.get(day, {})
        ri_daily.append(
            {
                "date": day,
                "util_pct": u_row.get("util_pct", 0.0),
                "coverage_pct": c_row.get("coverage_pct", 0.0),
                "unused_hours": u_row.get("unused_hours", 0.0),
                "on_demand_cost": c_row.get("on_demand_cost", 0.0),
            }
        )
        if u_row.get("util_pct") is not None:
            util_pcts.append(u_row["util_pct"])
        if c_row.get("on_demand_cost") is not None:
            on_demand_per_day.append(c_row["on_demand_cost"])

    ri_avg_util = sum(util_pcts) / len(util_pcts) if util_pcts else 0.0
    eligible = [v for v in on_demand_per_day if v > 0]
    ri_min_on_demand = min(eligible) if eligible else 0.0
    ri_safe_buy_hourly = ri_min_on_demand / 24.0
    ri_safe_p10 = _percentile(on_demand_per_day, 10) / 24.0
    ri_safe_p25 = _percentile(on_demand_per_day, 25) / 24.0
    ri_safe_p50 = _percentile(on_demand_per_day, 50) / 24.0
    ri_avg_daily_on_demand = (
        sum(on_demand_per_day) / len(on_demand_per_day) if on_demand_per_day else 0.0
    )
    ri_eff_discount = (
        100.0 * ri_total_savings / (ri_total_savings + ri_total_reserved_cost)
        if (ri_total_savings + ri_total_reserved_cost) > 0
        else 0.0
    )

    # --- Per-linked-account coverage (also doubles as a fallback when org-level
    # RI metrics return zero because RI sharing is disabled — querying with
    # GroupBy=LINKED_ACCOUNT bypasses sharing semantics).
    per_account: dict[str, dict] = {}
    try:
        sp_acct = fetch_sp_coverage_by_account(start, end_exclusive)
        for entry in sp_acct.get("CoveragesByTime", []):
            for grp in entry.get("Groups", []):
                acct_id = (grp.get("Attributes", {}) or {}).get("LINKED_ACCOUNT", "")
                if not acct_id:
                    continue
                cov = grp.get("Coverage", {})
                on_demand = _safe_float(cov, "OnDemandCost")
                covered = _safe_float(cov, "SpendCoveredBySavingsPlans")
                cov_pct = _safe_float(cov, "CoveragePercentage")
                row = per_account.setdefault(acct_id, _empty_account_row(acct_id))
                row["sp_on_demand"] += on_demand
                row["sp_covered"] += covered
                row["_sp_cov_pcts"].append(cov_pct)
    except Exception as e:  # noqa: BLE001
        logger.warning("SP coverage by-account fetch failed: %s", e)

    try:
        ri_acct_cov = fetch_ri_coverage_by_account(start, end_exclusive)
        for entry in ri_acct_cov.get("CoveragesByTime", []):
            for grp in entry.get("Groups", []):
                acct_id = (grp.get("Attributes", {}) or {}).get("LINKED_ACCOUNT", "")
                if not acct_id:
                    continue
                attrs = grp.get("Coverage", {})
                cov_cost = attrs.get("CoverageCost", {}) or {}
                cov_hrs = attrs.get("CoverageHours", {}) or {}
                on_demand = _safe_float(cov_cost, "OnDemandCost")
                reserved = _safe_float(cov_cost, "ReservedCost")
                cov_pct = _safe_float(cov_hrs, "CoverageHoursPercentage")
                row = per_account.setdefault(acct_id, _empty_account_row(acct_id))
                row["ri_on_demand"] += on_demand
                row["ri_covered"] += reserved
                row["_ri_cov_pcts"].append(cov_pct)
    except Exception as e:  # noqa: BLE001
        logger.warning("RI coverage by-account fetch failed: %s", e)

    try:
        ri_acct_util = fetch_ri_utilization_by_account(start, end_exclusive)
        for entry in ri_acct_util.get("UtilizationsByTime", []):
            for grp in entry.get("Groups", []):
                acct_id = (grp.get("Attributes", {}) or {}).get("LINKED_ACCOUNT", "")
                if not acct_id:
                    continue
                tot = grp.get("Attributes", {})  # group-level attrs
                util = grp.get("Utilization", {}) or {}
                util_pct = _safe_float(util, "UtilizationPercentage")
                purchased = _safe_float(util, "PurchasedHours")
                unused = _safe_float(util, "UnusedHours")
                savings = _safe_float(util, "NetRISavings")
                row = per_account.setdefault(acct_id, _empty_account_row(acct_id))
                row["ri_purchased_hours"] += purchased
                row["ri_unused_hours"] += unused
                row["ri_savings"] += savings
                row["_ri_util_pcts"].append(util_pct)
                _ = tot  # silence
    except Exception as e:  # noqa: BLE001
        logger.warning("RI utilization by-account fetch failed: %s", e)

    per_account_rows = []
    name_lookup = account_map or {}
    for acct_id, row in per_account.items():
        row["account_name"] = name_lookup.get(acct_id, "")
        sp_cov_pcts = row.pop("_sp_cov_pcts")
        ri_cov_pcts = row.pop("_ri_cov_pcts")
        ri_util_pcts = row.pop("_ri_util_pcts")
        row["sp_coverage_pct"] = sum(sp_cov_pcts) / len(sp_cov_pcts) if sp_cov_pcts else 0.0
        row["ri_coverage_pct"] = sum(ri_cov_pcts) / len(ri_cov_pcts) if ri_cov_pcts else 0.0
        row["ri_util_pct"] = sum(ri_util_pcts) / len(ri_util_pcts) if ri_util_pcts else 0.0
        per_account_rows.append(row)
    # Sort by SP+RI on-demand desc — the accounts with the most opportunity surface first.
    per_account_rows.sort(
        key=lambda r: r["sp_on_demand"] + r["ri_on_demand"], reverse=True
    )

    # Backfill org-level RI metrics from per-account aggregation if CE returned 0
    # (typical when RI/SP discount sharing is disabled).
    if ri_total_on_demand == 0 and per_account_rows:
        ri_total_on_demand = sum(r["ri_on_demand"] for r in per_account_rows)
        ri_total_reserved_cost = sum(r["ri_covered"] for r in per_account_rows)
        if ri_total_on_demand > 0 or ri_total_reserved_cost > 0:
            ri_avg_cov = (
                sum(r["ri_coverage_pct"] for r in per_account_rows)
                / len(per_account_rows)
            )
    if ri_total_purchased == 0 and per_account_rows:
        ri_total_purchased = sum(r["ri_purchased_hours"] for r in per_account_rows)
        ri_total_unused = sum(r["ri_unused_hours"] for r in per_account_rows)
        ri_total_savings = sum(r["ri_savings"] for r in per_account_rows)
        if ri_total_purchased > 0:
            non_zero = [r["ri_util_pct"] for r in per_account_rows if r["ri_util_pct"] > 0]
            ri_avg_util = sum(non_zero) / len(non_zero) if non_zero else 0.0
    if ri_eff_discount == 0 and (ri_total_savings + ri_total_reserved_cost) > 0:
        ri_eff_discount = (
            100.0 * ri_total_savings / (ri_total_savings + ri_total_reserved_cost)
        )

    # --- Linear projection of on-demand $ for the next COMMIT_PROJECT_DAYS days.
    sp_on_demand_series = [d["on_demand_cost"] for d in sp_daily]
    proj_values, slope = _linear_projection(sp_on_demand_series, COMMIT_PROJECT_DAYS)
    proj_series: list[dict] = []
    if proj_values and sp_daily:
        last_date = date.fromisoformat(sp_daily[-1]["date"])
        for i, v in enumerate(proj_values, 1):
            proj_series.append({
                "date": (last_date + timedelta(days=i)).isoformat(),
                "on_demand": round(v, 2),
            })

    # --- SP inventory + expiry buckets
    sp_inventory, sp_visible = _fetch_sp_inventory()
    exp_30 = exp_60 = exp_90 = 0.0
    for s in sp_inventory:
        d = s.get("days_left", -1)
        h = s.get("hourly", 0.0)
        if d is None or d < 0:
            continue
        if d <= 30:
            exp_30 += h
        if d <= 60:
            exp_60 += h
        if d <= 90:
            exp_90 += h

    return CommitmentSummary(
        days=days,
        sp_daily=sp_daily,
        ri_daily=ri_daily,
        sp_avg_util_pct=sp_avg_util,
        sp_avg_coverage_pct=sp_avg_cov,
        sp_total_commitment=sp_total_commit,
        sp_total_unused=sp_total_unused,
        sp_total_savings=sp_total_savings,
        sp_total_on_demand=sp_total_on_demand,
        sp_min_daily_on_demand=sp_min_on_demand,
        sp_safe_buy_hourly=sp_safe_buy_hourly,
        sp_safe_buy_p10_hourly=sp_safe_p10,
        sp_safe_buy_p25_hourly=sp_safe_p25,
        sp_safe_buy_p50_hourly=sp_safe_p50,
        sp_avg_daily_on_demand=sp_avg_daily_on_demand,
        sp_effective_discount_pct=sp_eff_discount,
        ri_avg_util_pct=ri_avg_util,
        ri_avg_coverage_pct=ri_avg_cov,
        ri_total_purchased_hours=ri_total_purchased,
        ri_total_unused_hours=ri_total_unused,
        ri_total_savings=ri_total_savings,
        ri_total_on_demand=ri_total_on_demand,
        ri_total_reserved_cost=ri_total_reserved_cost,
        ri_min_daily_on_demand=ri_min_on_demand,
        ri_safe_buy_hourly=ri_safe_buy_hourly,
        ri_safe_buy_p10_hourly=ri_safe_p10,
        ri_safe_buy_p25_hourly=ri_safe_p25,
        ri_safe_buy_p50_hourly=ri_safe_p50,
        ri_avg_daily_on_demand=ri_avg_daily_on_demand,
        ri_effective_discount_pct=ri_eff_discount,
        per_account=per_account_rows,
        on_demand_projection=proj_series,
        on_demand_proj_total=sum(proj_values),
        on_demand_trend_slope=slope,
        sp_inventory=sp_inventory,
        sp_inventory_visible=sp_visible,
        sp_expiring_30d_hourly=exp_30,
        sp_expiring_60d_hourly=exp_60,
        sp_expiring_90d_hourly=exp_90,
    )


def _empty_account_row(account_id: str) -> dict:
    return {
        "account_id": account_id,
        "sp_on_demand": 0.0,
        "sp_covered": 0.0,
        "_sp_cov_pcts": [],
        "ri_on_demand": 0.0,
        "ri_covered": 0.0,
        "_ri_cov_pcts": [],
        "ri_purchased_hours": 0.0,
        "ri_unused_hours": 0.0,
        "ri_savings": 0.0,
        "_ri_util_pcts": [],
    }


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
# Report writing
# -----------------------------------------------------------------------------
def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def fmt_delta_pct(a: float, b: float, *, arrow: bool = False) -> str:
    if b == 0:
        return "—"
    pct = (a - b) / b * 100
    if arrow:
        return f"{'↑' if pct > 0 else '↓'}{abs(pct):.1f}%"
    return f"{pct:+.1f}%"


# Number of trailing days of history embedded into the HTML for charts.
HTML_TREND_DAYS = 30


def _org_daily_series(df: pl.DataFrame, end: date, days: int) -> list[dict]:
    """Return [{date, cost}] for the org total across the last `days` days."""
    if df.is_empty():
        return []
    start = end - timedelta(days=days - 1)
    agg = (
        df.filter(~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .group_by("date")
        .agg(pl.col("cost").sum())
        .sort("date")
    )
    return [
        {"date": row["date"].isoformat(), "cost": float(row["cost"])}
        for row in agg.iter_rows(named=True)
    ]


def _account_daily_series(
    df: pl.DataFrame, account_id: str, end: date, days: int
) -> list[dict]:
    """Return [{date, cost}] for one account across the last `days` days."""
    if df.is_empty():
        return []
    start = end - timedelta(days=days - 1)
    agg = (
        df.filter(pl.col("account_id") == account_id)
        .filter(~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .group_by("date")
        .agg(pl.col("cost").sum())
        .sort("date")
    )
    return [
        {"date": row["date"].isoformat(), "cost": float(row["cost"])}
        for row in agg.iter_rows(named=True)
    ]


def _safe_json(obj: object) -> str:
    """json.dumps with </script> sequences neutered for safe inline embedding."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


_HTML_STYLE = """
:root {
  --bg: #0b0d10;
  --panel: #13161a;
  --panel-hi: #1a1f25;
  --border: #232932;
  --fg: #e6eaf0;
  --muted: #8892a0;
  --accent: #6ea8fe;
  --up: #ef6c6c;
  --down: #51cf66;
  --tag-bg: #23384f;
  --tag-fg: #9fc2ff;
  --tag-up-bg: #3a1f22;
  --tag-up-fg: #ff9c9c;
  --tag-down-bg: #1f3a28;
  --tag-down-fg: #8be3a4;
  --bar-2: #384860;
  --tooltip-theme: dark;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
:root[data-theme="light"] {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --panel-hi: #f1f4f8;
  --border: #e3e7ed;
  --fg: #1a1f2c;
  --muted: #5d6573;
  --accent: #2563eb;
  --up: #c0322f;
  --down: #1f8a44;
  --tag-bg: #dbe7fb;
  --tag-fg: #1c4cb8;
  --tag-up-bg: #fbe1e0;
  --tag-up-fg: #a32320;
  --tag-down-bg: #def3e2;
  --tag-down-fg: #166c33;
  --bar-2: #b9c4d6;
  --tooltip-theme: light;
}
* { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--fg); margin: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.45;
  padding: 28px 20px 64px;
  max-width: 1200px;
  margin: 0 auto;
}
h1 { font-size: 28px; margin: 0; font-weight: 600;
     line-height: 1.15; letter-spacing: -0.01em; }
h2 { font-size: 13px; margin: 0 0 12px; font-weight: 600; }
h3 { font-size: 13px; margin: 0; font-weight: 600; }
code { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.meta { color: var(--muted); font-size: 12px; margin-bottom: 20px; }
.page-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-bottom: 4px;
}
.theme-toggle {
  appearance: none;
  background: var(--panel-hi);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  font: inherit;
  font-size: 12px;
  padding: 4px 10px;
  cursor: pointer;
  line-height: 1;
}
.theme-toggle:hover { color: var(--fg); }
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
  margin-bottom: 18px;
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.kpi {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}
.kpi .label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.kpi .value { font-size: 20px; font-weight: 600; margin-top: 4px; }
.kpi .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
.up { color: var(--up); }
.down { color: var(--down); }
.chart-wrap { margin-top: 12px; }
.subsection {
  margin-top: 22px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
}
.insights ul { margin: 0; padding-left: 18px; }
.insights li { margin: 2px 0; }
.insights .acct-label {
  font-weight: 600;
  margin-top: 12px;
  margin-bottom: 2px;
}
.insights .acct-label:first-child { margin-top: 0; }
.acct-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.acct-head h2 {
  font-size: 28px;
  font-weight: 600;
  line-height: 1.15;
  letter-spacing: -0.01em;
  margin: 0;
}
.acct-head code { font-size: 13px; }
.grid-2 {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}
@media (max-width: 760px) {
  .kpi-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-2 { grid-template-columns: minmax(0, 1fr); }
}
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 14px; }
th, td {
  text-align: right; padding: 6px 10px;
  border-bottom: 1px solid var(--border);
}
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.04em; }
tr:last-child td { border-bottom: none; }
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  background: var(--tag-bg);
  color: var(--tag-fg);
}
.tag.up { background: var(--tag-up-bg); color: var(--tag-up-fg); }
.tag.down { background: var(--tag-down-bg); color: var(--tag-down-fg); }
.tabs {
  display: flex; gap: 6px; margin: 12px 0 14px;
  border-bottom: 1px solid var(--border);
}
.tabs button {
  appearance: none; background: transparent; border: 0;
  border-bottom: 2px solid transparent;
  color: var(--muted); font: inherit; font-size: 13px;
  padding: 8px 14px; cursor: pointer; line-height: 1;
}
.tabs button:hover { color: var(--fg); }
.tabs button.active {
  color: var(--fg); border-bottom-color: var(--accent); font-weight: 600;
}
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.callout {
  background: var(--panel-hi);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  padding: 10px 14px;
  margin: 12px 0;
  font-size: 13px;
}
.callout strong { color: var(--fg); }
.ai-analysis .ai-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 12px; flex-wrap: wrap; margin-bottom: 4px;
}
.ai-analysis .ai-head h2 { margin: 0; }
.ai-analysis .ai-sub {
  color: var(--muted); font-weight: 400; font-size: 13px;
}
.ai-analysis h3 {
  margin: 18px 0 6px; font-size: 15px; color: var(--fg);
  border-bottom: 1px solid var(--border); padding-bottom: 4px;
}
.ai-analysis h4 { margin: 12px 0 4px; font-size: 13px; color: var(--fg); }
.ai-analysis p { font-size: 13px; line-height: 1.55; margin: 8px 0; }
.ai-analysis ul, .ai-analysis ol { font-size: 13px; line-height: 1.55; padding-left: 22px; }
.ai-analysis li { margin: 3px 0; }
.ai-analysis code {
  font-family: var(--mono); font-size: 12px;
  background: var(--panel-hi); padding: 1px 5px; border-radius: 4px;
}
"""


_HTML_SCRIPT = """
const DATA = window.__COST_DATA__;
const CHARTS = [];

function cssVar(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
}

function themeColors() {
  return {
    fg: cssVar('--fg'),
    muted: cssVar('--muted'),
    border: cssVar('--border'),
    accent: cssVar('--accent'),
    bar2: cssVar('--bar-2'),
    tooltip: cssVar('--tooltip-theme') || 'dark',
  };
}

function fmtUsd(v) {
  return '$' + Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

function chartBase(c) {
  return {
    chart: {
      toolbar: {
        show: true,
        tools: {
          download: false,
          selection: true,
          zoom: true,
          zoomin: true,
          zoomout: true,
          pan: false,
          reset: true,
        },
        autoSelected: 'zoom',
      },
      zoom: { enabled: true, type: 'x', autoScaleYaxis: true },
      foreColor: c.muted,
      fontFamily: 'inherit',
      animations: { enabled: false },
    },
    grid: { borderColor: c.border, strokeDashArray: 3 },
    tooltip: { theme: c.tooltip, x: { format: 'yyyy-MM-dd' } },
  };
}

function lineOptions(series, opts, c) {
  const base = chartBase(c);
  return {
    ...base,
    chart: { ...base.chart, type: 'area', height: opts.height || 180 },
    stroke: { curve: 'smooth', width: 2 },
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 0.6, opacityFrom: 0.35, opacityTo: 0.05 },
    },
    colors: [opts.color || c.accent],
    dataLabels: { enabled: false },
    xaxis: {
      type: 'datetime',
      labels: { style: { colors: c.muted, fontSize: '11px' } },
      axisBorder: { show: false }, axisTicks: { show: false },
    },
    yaxis: {
      min: 0,
      forceNiceScale: true,
      labels: {
        style: { colors: c.muted, fontSize: '11px' },
        formatter: (v) => '$' + Math.round(v).toLocaleString(),
      },
    },
    tooltip: { ...base.tooltip, y: { formatter: fmtUsd } },
    series: [{ name: opts.name || 'Cost', data: series.map(d => [d.date, d.cost]) }],
  };
}

function pctLineOptions(series, opts, c) {
  const base = chartBase(c);
  return {
    ...base,
    chart: { ...base.chart, type: 'line', height: opts.height || 200 },
    stroke: { curve: 'smooth', width: 2 },
    colors: opts.colors || [c.accent],
    dataLabels: { enabled: false },
    legend: { position: 'top', horizontalAlign: 'right', fontSize: '12px',
              labels: { colors: c.fg } },
    xaxis: {
      type: 'datetime',
      labels: { style: { colors: c.muted, fontSize: '11px' } },
      axisBorder: { show: false }, axisTicks: { show: false },
    },
    yaxis: {
      min: 0,
      max: 100,
      labels: {
        style: { colors: c.muted, fontSize: '11px' },
        formatter: (v) => Math.round(v) + '%',
      },
    },
    tooltip: { ...base.tooltip,
               y: { formatter: (v) => Number(v).toFixed(1) + '%' } },
    series: opts.series,
  };
}

function barOptions(services, c) {
  const base = chartBase(c);
  return {
    ...base,
    chart: { ...base.chart, type: 'bar',
             height: Math.max(220, services.length * 26), stacked: false },
    plotOptions: { bar: { horizontal: true, barHeight: '70%', borderRadius: 3 } },
    colors: [c.accent, c.bar2],
    dataLabels: { enabled: false },
    legend: { position: 'top', horizontalAlign: 'right', fontSize: '12px' },
    xaxis: {
      labels: {
        style: { colors: c.muted, fontSize: '11px' },
        formatter: (v) => '$' + Math.round(v).toLocaleString(),
      },
    },
    yaxis: { labels: { style: { colors: c.fg, fontSize: '12px' } } },
    tooltip: { ...base.tooltip, y: { formatter: fmtUsd }, x: { show: false } },
    series: [
      { name: 'Day', data: services.map(s => ({ x: s.service, y: s.yesterday })) },
      { name: '30d avg', data: services.map(s => ({ x: s.service, y: s.avg_30d })) },
    ],
  };
}

function renderLine(elId, series, opts) {
  const el = document.getElementById(elId);
  if (!el || !series.length) return;
  const chart = new ApexCharts(el, lineOptions(series, opts, themeColors()));
  chart.render();
  CHARTS.push({ chart, kind: 'line', series, opts });
}

function renderPctLine(elId, opts) {
  const el = document.getElementById(elId);
  if (!el) return;
  const hasData = (opts.series || []).some(s => (s.data || []).length);
  if (!hasData) return;
  const chart = new ApexCharts(el, pctLineOptions(null, opts, themeColors()));
  chart.render();
  CHARTS.push({ chart, kind: 'pct', opts });
}

function onDemandProjOptions(opts, c) {
  const base = chartBase(c);
  return {
    ...base,
    chart: { ...base.chart, type: 'line', height: opts.height || 240 },
    stroke: { curve: 'smooth', width: [2, 2], dashArray: [0, 6] },
    colors: [c.accent, c.bar2],
    dataLabels: { enabled: false },
    legend: { position: 'top', horizontalAlign: 'right', fontSize: '12px',
              labels: { colors: c.fg } },
    xaxis: {
      type: 'datetime',
      labels: { style: { colors: c.muted, fontSize: '11px' } },
      axisBorder: { show: false }, axisTicks: { show: false },
    },
    yaxis: {
      min: 0,
      forceNiceScale: true,
      labels: {
        style: { colors: c.muted, fontSize: '11px' },
        formatter: (v) => '$' + Math.round(v).toLocaleString(),
      },
    },
    tooltip: { ...base.tooltip, y: { formatter: fmtUsd } },
    series: [
      { name: 'Actual on-demand $/day',
        data: opts.actual.map(d => [d.date, d.on_demand]) },
      { name: 'Projected (linear, 90d)',
        data: opts.projection.map(d => [d.date, d.on_demand]) },
    ],
  };
}

function renderOnDemandProjection(elId, opts) {
  const el = document.getElementById(elId);
  if (!el) return;
  const chart = new ApexCharts(el, onDemandProjOptions(opts, themeColors()));
  chart.render();
  CHARTS.push({ chart, kind: 'odp', opts });
}

function renderServicesBar(elId, services) {
  const el = document.getElementById(elId);
  if (!el || !services.length) return;
  const chart = new ApexCharts(el, barOptions(services, themeColors()));
  chart.render();
  CHARTS.push({ chart, kind: 'bar', services });
}

function repaintCharts() {
  const c = themeColors();
  for (const entry of CHARTS) {
    let opts;
    if (entry.kind === 'line') opts = lineOptions(entry.series, entry.opts, c);
    else if (entry.kind === 'pct') opts = pctLineOptions(null, entry.opts, c);
    else if (entry.kind === 'odp') opts = onDemandProjOptions(entry.opts, c);
    else opts = barOptions(entry.services, c);
    entry.chart.updateOptions(opts, false, false);
  }
}

function activateTab(name) {
  document.querySelectorAll('[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('active', p.id === 'tab-' + name);
  });
  // ApexCharts mis-measures hidden parents; nudge them after the panel becomes
  // visible so any unseeded width gets recomputed.
  window.dispatchEvent(new Event('resize'));
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('cost-report-theme', theme); } catch (_) {}
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'light' ? '☽ Dark' : '☀ Light';
  repaintCharts();
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.textContent = document.documentElement.getAttribute('data-theme') === 'light'
      ? '☽ Dark' : '☀ Light';
    btn.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      applyTheme(cur === 'light' ? 'dark' : 'light');
    });
  }
  document.querySelectorAll('[data-tab]').forEach(b => {
    b.addEventListener('click', () => activateTab(b.dataset.tab));
  });
  renderLine('org-trend', DATA.org_trend, { name: 'Org total', height: 220 });
  for (const a of DATA.accounts) {
    renderLine('acct-trend-' + a.account_id, a.trend, {
      name: a.account_name, height: 160,
    });
    renderServicesBar('acct-services-' + a.account_id, a.top_services);
  }
  if (DATA.commitments) {
    const c = DATA.commitments;
    if (c.on_demand_actual && c.on_demand_actual.length) {
      renderOnDemandProjection('on-demand-trend-chart', {
        actual: c.on_demand_actual,
        projection: c.on_demand_projection || [],
        height: 260,
      });
    }
    renderPctLine('sp-util-chart', {
      height: 240,
      colors: ['#51cf66', '#6ea8fe'],
      series: [
        { name: 'Utilization %',
          data: c.sp_daily.map(d => [d.date, d.util_pct]) },
        { name: 'Coverage %',
          data: c.sp_daily.map(d => [d.date, d.coverage_pct]) },
      ],
    });
    renderPctLine('ri-util-chart', {
      height: 240,
      colors: ['#51cf66', '#6ea8fe'],
      series: [
        { name: 'Utilization %',
          data: c.ri_daily.map(d => [d.date, d.util_pct]) },
        { name: 'Coverage %',
          data: c.ri_daily.map(d => [d.date, d.coverage_pct]) },
      ],
    });
  }
});
"""


def _delta_pct_html(a: float, b: float) -> str:
    """Render a DoD/vs-30d delta as a colored tag, or em-dash when undefined."""
    if b == 0:
        return '<span class="tag">—</span>'
    if max(a, b) < DELTA_NOISE_FLOOR_USD:
        return '<span class="tag">—</span>'
    pct = (a - b) / b * 100
    cls = "up" if pct > 0 else "down"
    arrow = "↑" if pct > 0 else "↓"
    return f'<span class="tag {cls}">{arrow}{abs(pct):.1f}%</span>'


def _render_insights_html(
    summaries: list[AccountSummary], insights: dict[str, list[str]]
) -> str:
    noisy = [s for s in summaries if insights.get(s.account_id)]
    if not noisy:
        return (
            '<div class="subsection insights"><h2>Insights</h2>'
            '<p style="color:var(--muted);margin:0">'
            "Nothing unusual across any account today.</p></div>"
        )
    parts = ['<div class="subsection insights"><h2>Insights</h2>']
    for s in noisy:
        parts.append(
            f'<div class="acct-label">{html.escape(s.account_name)} '
            f'<code>{html.escape(s.account_id)}</code></div>'
        )
        parts.append("<ul>")
        for note in insights[s.account_id]:
            # Notes use **bold** markdown — convert to <strong>.
            safe = html.escape(note)
            while "**" in safe:
                safe = safe.replace("**", "<strong>", 1)
                safe = safe.replace("**", "</strong>", 1)
            parts.append(f"<li>{safe}</li>")
        parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def _render_summary_table_html(summaries: list[AccountSummary]) -> str:
    rows = []
    for s in summaries:
        rows.append(
            "<tr>"
            f"<td>{html.escape(s.account_name)} "
            f'<code>{html.escape(s.account_id)}</code></td>'
            f"<td>{fmt_usd(s.total_yesterday)}</td>"
            f"<td>{fmt_usd(s.total_day_before)}</td>"
            f"<td>{fmt_usd(s.total_avg_30d)}</td>"
            f"<td>{fmt_usd(s.total_avg_90d)}</td>"
            f"<td>{_delta_pct_html(s.total_yesterday, s.total_day_before)}</td>"
            "</tr>"
        )
    grand_y = sum(s.total_yesterday for s in summaries)
    grand_p = sum(s.total_day_before for s in summaries)
    grand_30 = sum(s.total_avg_30d for s in summaries)
    grand_90 = sum(s.total_avg_90d for s in summaries)
    rows.append(
        "<tr>"
        "<td><strong>TOTAL</strong></td>"
        f"<td><strong>{fmt_usd(grand_y)}</strong></td>"
        f"<td><strong>{fmt_usd(grand_p)}</strong></td>"
        f"<td><strong>{fmt_usd(grand_30)}</strong></td>"
        f"<td><strong>{fmt_usd(grand_90)}</strong></td>"
        f"<td>{_delta_pct_html(grand_y, grand_p)}</td>"
        "</tr>"
    )
    return (
        '<div class="subsection"><h2>Summary across accounts</h2>'
        "<table><thead><tr>"
        "<th>Account</th><th>Day</th><th>Day Before</th>"
        "<th>30d Avg</th><th>90d Avg</th><th>DoD</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _render_account_card_html(s: AccountSummary) -> str:
    kpis = (
        '<div class="kpi-row">'
        f'<div class="kpi"><div class="label">Day</div>'
        f'<div class="value">{fmt_usd(s.total_yesterday)}</div></div>'
        f'<div class="kpi"><div class="label">DoD</div>'
        f'<div class="value">{_delta_pct_html(s.total_yesterday, s.total_day_before)}</div>'
        f'<div class="sub">was {fmt_usd(s.total_day_before)}</div></div>'
        f'<div class="kpi"><div class="label">30d avg</div>'
        f'<div class="value">{fmt_usd(s.total_avg_30d)}</div>'
        f'<div class="sub">vs 30d {_delta_pct_html(s.total_yesterday, s.total_avg_30d)}</div></div>'
        f'<div class="kpi"><div class="label">90d avg</div>'
        f'<div class="value">{fmt_usd(s.total_avg_90d)}</div></div>'
        "</div>"
    )

    rows = []
    table_rows = s.services.head(TOP_N_TABLE)
    for i, row in enumerate(table_rows.iter_rows(named=True), 1):
        pct_of_day = (
            f"{row['yesterday'] / s.total_yesterday * 100:.1f}%"
            if s.total_yesterday > 0
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{i}. {html.escape(row['service'])}</td>"
            f"<td>{fmt_usd(row['yesterday'])}</td>"
            f"<td>{fmt_usd(row['day_before'])}</td>"
            f"<td>{fmt_usd(row['avg_30d'])}</td>"
            f"<td>{fmt_usd(row['avg_90d'])}</td>"
            f"<td>{pct_of_day}</td>"
            f"<td>{_delta_pct_html(row['yesterday'], row['day_before'])}</td>"
            f"<td>{_delta_pct_html(row['yesterday'], row['avg_30d'])}</td>"
            "</tr>"
        )

    charts = (
        '<div class="grid-2 chart-wrap">'
        f'<div><div class="label" style="color:var(--muted);font-size:11px;'
        f'text-transform:uppercase;margin-bottom:4px;">Last {HTML_TREND_DAYS}d</div>'
        f'<div id="acct-trend-{html.escape(s.account_id)}"></div></div>'
        f'<div><div class="label" style="color:var(--muted);font-size:11px;'
        'text-transform:uppercase;margin-bottom:4px;">Top services — Day vs 30d avg</div>'
        f'<div id="acct-services-{html.escape(s.account_id)}"></div></div>'
        "</div>"
    )

    return (
        '<section class="panel">'
        '<div class="acct-head">'
        f"<h2>{html.escape(s.account_name)}</h2>"
        f'<code>{html.escape(s.account_id)}</code>'
        "</div>"
        + kpis
        + charts
        + "<table><thead><tr>"
        "<th>Service</th><th>Day</th><th>Day Before</th>"
        "<th>30d Avg</th><th>90d Avg</th><th>% of Day</th>"
        "<th>DoD</th><th>vs 30d</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )


def _build_account_payload(
    s: AccountSummary, df: pl.DataFrame, top_n: int
) -> dict:
    top = s.services.head(top_n)
    top_services = [
        {
            "service": row["service"],
            "yesterday": float(row["yesterday"]),
            "avg_30d": float(row["avg_30d"]),
        }
        for row in top.iter_rows(named=True)
    ]
    return {
        "account_id": s.account_id,
        "account_name": s.account_name,
        "trend": _account_daily_series(df, s.account_id, s.report_date, HTML_TREND_DAYS),
        "top_services": top_services,
    }


def _fmt_hours(h: float) -> str:
    return f"{h:,.0f} h"


def _safe_buy_block(
    label: str,
    floor_hr: float,
    p10_hr: float,
    p25_hr: float,
    p50_hr: float,
    discount_1y: float,
    discount_3y: float,
) -> str:
    """Render the percentile-based safe-buy table for either SP or RI.

    The percentile buckets answer "how aggressive a commitment is OK?". Floor
    = zero-waste guarantee, P25 = some days have unused spend, P50 = half the
    days have unused spend (only sane when usage is growing).
    """
    def row(name: str, hr: float, note: str) -> str:
        if hr <= 0.001:
            return ""
        monthly = hr * 24 * 30
        s_1y = hr * 24 * 365 * discount_1y
        s_3y = hr * 24 * 365 * 3 * discount_3y
        return (
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{fmt_usd(hr)}/hr</td>"
            f"<td>{fmt_usd(monthly)}</td>"
            f"<td>{fmt_usd(s_1y)}</td>"
            f"<td>{fmt_usd(s_3y)}</td>"
            f"<td style=\"color:var(--muted);font-size:11px;\">{note}</td>"
            "</tr>"
        )
    rows = [
        row("Floor (zero-waste)", floor_hr, "smallest day's on-demand"),
        row("P10 (very safe)", p10_hr, "≤10% of days have leftover"),
        row("P25 (moderate)", p25_hr, "≤25% of days have leftover"),
        row("P50 (aggressive)", p50_hr, "half of days have leftover"),
    ]
    rows = [r for r in rows if r]
    if not rows:
        return ""
    return (
        f'<div style="margin:6px 0 14px;"><div class="label" '
        'style="color:var(--muted);font-size:11px;text-transform:uppercase;'
        'margin-bottom:6px;">Safe-to-buy headroom — ' + label + "</div>"
        "<table>"
        "<thead><tr>"
        "<th>Tier</th><th>Hourly</th><th>~Monthly</th>"
        "<th>Est. 1y savings</th><th>Est. 3y savings</th><th></th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _render_per_account_table(
    rows: list[dict], section_days: int
) -> str:
    if not rows:
        return ""
    body: list[str] = []
    for r in rows:
        # Skip rows that are pure noise (no SP and no RI activity)
        if (r["sp_on_demand"] + r["sp_covered"]
                + r["ri_on_demand"] + r["ri_covered"]) < 0.5:
            continue
        name = r.get("account_name") or "—"
        body.append(
            "<tr>"
            f"<td>{html.escape(name)} <code>{html.escape(r['account_id'])}</code></td>"
            f"<td>{fmt_usd(r['sp_on_demand'])}</td>"
            f"<td>{fmt_usd(r['sp_covered'])}</td>"
            f"<td>{r['sp_coverage_pct']:.1f}%</td>"
            f"<td>{fmt_usd(r['ri_on_demand'])}</td>"
            f"<td>{fmt_usd(r['ri_covered'])}</td>"
            f"<td>{r['ri_coverage_pct']:.1f}%</td>"
            f"<td>{r['ri_util_pct']:.1f}%</td>"
            "</tr>"
        )
    if not body:
        return ""
    return (
        '<h3 style="font-size:15px;margin:24px 0 10px;">Per-account coverage '
        f"— last {section_days} days</h3>"
        '<div class="meta" style="margin:0 0 8px;">'
        "Aggregated from Cost Explorer with <code>GroupBy=LinkedAccount</code>. "
        "This view sees RIs/SPs even when org-level discount sharing is off."
        "</div>"
        "<table><thead><tr>"
        "<th>Account</th>"
        "<th>SP on-demand</th><th>SP covered</th><th>SP cov %</th>"
        "<th>RI on-demand</th><th>RI covered</th><th>RI cov %</th>"
        "<th>RI util %</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _render_inventory(c: "CommitmentSummary") -> str:
    if not c.sp_inventory_visible:
        return (
            '<h3 style="font-size:15px;margin:24px 0 10px;">'
            "SP inventory &amp; expirations</h3>"
            '<div class="meta" style="margin:0;">'
            "Could not enumerate Savings Plans from current credentials. "
            "<code>savingsplans:DescribeSavingsPlans</code> is per-account; "
            "for org-wide inventory, run from each account or wire up "
            "cross-account roles."
            "</div>"
        )
    if not c.sp_inventory:
        return (
            '<h3 style="font-size:15px;margin:24px 0 10px;">'
            "SP inventory &amp; expirations</h3>"
            '<div class="meta" style="margin:0;">'
            "No active Savings Plans owned by this account."
            "</div>"
        )
    rows = []
    for s in sorted(c.sp_inventory, key=lambda x: x.get("days_left", 9999)):
        d = s.get("days_left", -1)
        d_str = f"{d}d" if d >= 0 else "—"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(s.get('plan_id', '')[:24])}…</code></td>"
            f"<td>{html.escape(s.get('type', ''))}</td>"
            f"<td>{int(s.get('term', 0))}d</td>"
            f"<td>{fmt_usd(s.get('hourly', 0.0))}/hr</td>"
            f"<td>{html.escape(s.get('end', ''))}</td>"
            f"<td>{d_str}</td>"
            "</tr>"
        )
    callout = ""
    if c.sp_expiring_90d_hourly > 0.01:
        callout = (
            '<div class="callout"><strong>Heads up:</strong> '
            f"{fmt_usd(c.sp_expiring_30d_hourly)}/hr expires in ≤30d, "
            f"{fmt_usd(c.sp_expiring_60d_hourly)}/hr in ≤60d, "
            f"{fmt_usd(c.sp_expiring_90d_hourly)}/hr in ≤90d. "
            "Coverage will drop unless you renew or reshape ahead of time."
            "</div>"
        )
    return (
        '<h3 style="font-size:15px;margin:24px 0 10px;">'
        "SP inventory &amp; expirations</h3>"
        + callout
        + "<table><thead><tr>"
        "<th>Plan ID</th><th>Type</th><th>Term</th>"
        "<th>Hourly</th><th>End</th><th>Left</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_commitments_html(c: "CommitmentSummary | None") -> str:
    if c is None or (not c.sp_daily and not c.ri_daily and not c.per_account):
        return (
            '<section class="panel"><h2>Commitments</h2>'
            '<p style="color:var(--muted);margin:0">'
            "No Savings Plan or Reservation activity over the last "
            f"{COMMIT_LOOKBACK_DAYS} days.</p></section>"
        )

    def kpi(label: str, value: str, sub: str = "") -> str:
        sub_html = f'<div class="sub">{sub}</div>' if sub else ""
        return (
            f'<div class="kpi"><div class="label">{label}</div>'
            f'<div class="value">{value}</div>{sub_html}</div>'
        )

    parts: list[str] = ['<section class="panel">']
    parts.append(
        f'<h2 style="font-size:18px;margin-bottom:6px;">Commitments — '
        f"last {c.days} days</h2>"
        '<div class="meta" style="margin-bottom:14px;">'
        "Org-level Savings Plan and Reserved Instance utilization &amp; coverage "
        "from Cost Explorer. Safe-to-buy tiers and 1y / 3y savings estimates "
        "use AWS list discounts (~27% / 54% for Compute SP, ~36% / 62% for "
        "Standard EC2 RI) — adjust for your actual mix."
        "</div>"
    )

    # --- Headline: what you're paying retail today, and the projection
    monthly_run_rate = c.sp_avg_daily_on_demand * 30
    trend_arrow = "↑" if c.on_demand_trend_slope > 0.5 else (
        "↓" if c.on_demand_trend_slope < -0.5 else "→"
    )
    trend_word = "growing" if c.on_demand_trend_slope > 0.5 else (
        "shrinking" if c.on_demand_trend_slope < -0.5 else "flat"
    )
    parts.append('<h3 style="font-size:15px;margin:8px 0 10px;">'
                 "On-demand you’re paying today (post-SP/RI)</h3>")
    parts.append('<div class="kpi-row">')
    parts.append(kpi(
        "On-demand / day",
        fmt_usd(c.sp_avg_daily_on_demand),
        f"~{fmt_usd(monthly_run_rate)}/mo run-rate",
    ))
    parts.append(kpi(
        "On-demand / hr",
        fmt_usd(c.sp_avg_daily_on_demand / 24.0),
        "average over the window",
    ))
    parts.append(kpi(
        "Trend",
        f"{trend_arrow} {trend_word}",
        f"{fmt_usd(abs(c.on_demand_trend_slope))}/day slope",
    ))
    parts.append(kpi(
        "Projected next 90d",
        fmt_usd(c.on_demand_proj_total),
        "linear fit on daily on-demand",
    ))
    parts.append("</div>")
    parts.append(
        '<div class="chart-wrap">'
        '<div class="label" style="color:var(--muted);font-size:11px;'
        'text-transform:uppercase;margin-bottom:4px;">'
        "On-demand $/day — actual + 90d linear projection</div>"
        '<div id="on-demand-trend-chart"></div></div>'
    )

    # --- Savings Plans
    if c.sp_daily:
        parts.append('<h3 style="font-size:15px;margin:24px 0 10px;">Savings Plans</h3>')
        parts.append('<div class="kpi-row">')
        parts.append(kpi("Avg utilization", f"{c.sp_avg_util_pct:.1f}%",
                         f"unused {fmt_usd(c.sp_total_unused)}"))
        parts.append(kpi("Avg coverage", f"{c.sp_avg_coverage_pct:.1f}%",
                         f"effective discount {c.sp_effective_discount_pct:.1f}%"))
        parts.append(kpi("Total commitment", fmt_usd(c.sp_total_commitment),
                         f"savings {fmt_usd(c.sp_total_savings)}"))
        parts.append(kpi("Safe to buy (floor)",
                         f"{fmt_usd(c.sp_safe_buy_hourly)}/hr",
                         f"min daily uncovered {fmt_usd(c.sp_min_daily_on_demand)}"))
        parts.append("</div>")

        parts.append(_safe_buy_block(
            "Savings Plans",
            c.sp_safe_buy_hourly, c.sp_safe_buy_p10_hourly,
            c.sp_safe_buy_p25_hourly, c.sp_safe_buy_p50_hourly,
            COMMIT_DISCOUNT_1Y_NU, COMMIT_DISCOUNT_3Y_AU,
        ))

        if c.sp_avg_util_pct > 0 and c.sp_avg_util_pct < 95:
            parts.append(
                '<div class="callout">'
                f"<strong>Heads up:</strong> SP utilization is {c.sp_avg_util_pct:.1f}% — "
                f"{fmt_usd(c.sp_total_unused)} of commitment went unused over "
                f"the last {c.days} days. Reshape the existing commitment before "
                "stacking more on top."
                "</div>"
            )

        parts.append(
            '<div class="chart-wrap">'
            '<div class="label" style="color:var(--muted);font-size:11px;'
            'text-transform:uppercase;margin-bottom:4px;">'
            "SP utilization &amp; coverage</div>"
            '<div id="sp-util-chart"></div></div>'
        )

    # --- Reservations
    if c.ri_daily or any(r["ri_on_demand"] + r["ri_covered"] > 0 for r in c.per_account):
        parts.append(
            '<h3 style="font-size:15px;margin:24px 0 10px;">Reserved Instances</h3>'
        )
        parts.append('<div class="kpi-row">')
        parts.append(kpi("Avg utilization", f"{c.ri_avg_util_pct:.1f}%",
                         f"unused {_fmt_hours(c.ri_total_unused_hours)}"))
        parts.append(kpi("Avg coverage", f"{c.ri_avg_coverage_pct:.1f}%",
                         f"effective discount {c.ri_effective_discount_pct:.1f}%"))
        parts.append(kpi("Purchased hours", _fmt_hours(c.ri_total_purchased_hours),
                         f"savings {fmt_usd(c.ri_total_savings)}"))
        parts.append(kpi("Safe to buy (floor)",
                         f"{fmt_usd(c.ri_safe_buy_hourly)}/hr",
                         f"min daily uncovered {fmt_usd(c.ri_min_daily_on_demand)}"))
        parts.append("</div>")

        parts.append(_safe_buy_block(
            "Reserved Instances",
            c.ri_safe_buy_hourly, c.ri_safe_buy_p10_hourly,
            c.ri_safe_buy_p25_hourly, c.ri_safe_buy_p50_hourly,
            RI_DISCOUNT_1Y_NU, RI_DISCOUNT_3Y_AU,
        ))

        if c.ri_avg_util_pct > 0 and c.ri_avg_util_pct < 95:
            parts.append(
                '<div class="callout">'
                f"<strong>Heads up:</strong> RI utilization is {c.ri_avg_util_pct:.1f}% — "
                f"{_fmt_hours(c.ri_total_unused_hours)} unused over the last "
                f"{c.days} days. Investigate before buying more."
                "</div>"
            )

        if c.ri_daily and any(d.get("util_pct", 0) > 0 for d in c.ri_daily):
            parts.append(
                '<div class="chart-wrap">'
                '<div class="label" style="color:var(--muted);font-size:11px;'
                'text-transform:uppercase;margin-bottom:4px;">'
                "RI utilization &amp; coverage</div>"
                '<div id="ri-util-chart"></div></div>'
            )

    # --- Per-linked-account drilldown
    parts.append(_render_per_account_table(c.per_account, c.days))

    # --- SP inventory + expirations
    parts.append(_render_inventory(c))

    parts.append("</section>")
    return "".join(parts)


def _commitments_payload(c: "CommitmentSummary | None") -> dict | None:
    if c is None:
        return None
    actual = [
        {"date": d["date"], "on_demand": round(d.get("on_demand_cost", 0.0), 2)}
        for d in c.sp_daily
    ]
    return {
        "sp_daily": c.sp_daily,
        "ri_daily": c.ri_daily,
        "on_demand_actual": actual,
        "on_demand_projection": c.on_demand_projection,
    }


# -----------------------------------------------------------------------------
# AI analysis (optional — Gemini AI Studio)
# -----------------------------------------------------------------------------
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)


def _gemini_model() -> str:
    """Resolve the Gemini model at call time (not import time).

    Read late so callers (e.g. the Lambda handler) can populate `GEMINI_MODEL`
    from a secrets bundle after the module has already been imported.
    """
    return os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


AI_TOP_SERVICES = 10
# How many days of daily history we feed the LLM (org + per-account series).
AI_HISTORY_DAYS = 90


def _ai_api_key() -> str | None:
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_AI_STUDIO_KEY")
    )


def _window_avg_org(df: pl.DataFrame, end: date, days: int) -> float:
    """Org-wide daily average over the last `days` days (lump services excluded)."""
    if df.is_empty():
        return 0.0
    start = end - timedelta(days=days - 1)
    total = (
        df.filter(~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .select(pl.col("cost").sum())
        .item()
    )
    return float(total or 0.0) / days


def _window_avg_account(
    df: pl.DataFrame, account_id: str, end: date, days: int
) -> float:
    """Account-level daily average over the last `days` days."""
    if df.is_empty():
        return 0.0
    start = end - timedelta(days=days - 1)
    total = (
        df.filter(pl.col("account_id") == account_id)
        .filter(~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .select(pl.col("cost").sum())
        .item()
    )
    return float(total or 0.0) / days


def _mtd_org(df: pl.DataFrame, end: date) -> float:
    """Org-wide month-to-date spend, lump services excluded, end-inclusive."""
    if df.is_empty():
        return 0.0
    start = end.replace(day=1)
    total = (
        df.filter(~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .select(pl.col("cost").sum())
        .item()
    )
    return float(total or 0.0)


def _mtd_account(df: pl.DataFrame, account_id: str, end: date) -> float:
    if df.is_empty():
        return 0.0
    start = end.replace(day=1)
    total = (
        df.filter(pl.col("account_id") == account_id)
        .filter(~pl.col("service").is_in(list(MONTHLY_LUMP_SERVICES)))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end))
        .select(pl.col("cost").sum())
        .item()
    )
    return float(total or 0.0)


def _eom_projection(mtd: float, recent_daily_avg: float, end: date) -> dict:
    """Naive end-of-month projection: MTD + recent_daily_avg × days_remaining.

    Uses the trailing 7d average as the run-rate proxy. Returns a small
    dict with the inputs surfaced so the LLM can sanity-check the figure.
    """
    days_in_month = calendar.monthrange(end.year, end.month)[1]
    days_elapsed = end.day  # end-inclusive
    days_remaining = days_in_month - days_elapsed
    projection = mtd + recent_daily_avg * days_remaining
    return {
        "mtd": round(mtd, 2),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "days_in_month": days_in_month,
        "run_rate_daily": round(recent_daily_avg, 2),
        "eom_projection": round(projection, 2),
    }


def _budget_view(
    monthly_budget: float | None, mtd: float, projection: float
) -> dict | None:
    """Compute budget-relative figures for the AI payload. Returns None if no budget."""
    if not monthly_budget or monthly_budget <= 0:
        return None
    pct_used = mtd / monthly_budget * 100.0
    pct_projected = projection / monthly_budget * 100.0
    return {
        "monthly_budget": round(float(monthly_budget), 2),
        "mtd_pct_of_budget": round(pct_used, 1),
        "projected_pct_of_budget": round(pct_projected, 1),
        "projected_overrun": round(max(0.0, projection - monthly_budget), 2),
        "projected_headroom": round(max(0.0, monthly_budget - projection), 2),
    }


def _weekly_buckets(daily: list[dict], end: date, weeks: int) -> list[dict]:
    """Roll a daily series into ISO-week buckets ending on `end`.

    Each bucket: {week_start, week_end, total, avg_per_day}.
    Most recent week last; partial trailing week is included as-is.
    """
    if not daily:
        return []
    by_date: dict[str, float] = {d["date"]: float(d["cost"]) for d in daily}
    out: list[dict] = []
    for w in range(weeks - 1, -1, -1):
        week_end = end - timedelta(days=7 * w)
        week_start = week_end - timedelta(days=6)
        total = 0.0
        days_present = 0
        cursor = week_start
        while cursor <= week_end:
            v = by_date.get(cursor.isoformat())
            if v is not None:
                total += v
                days_present += 1
            cursor += timedelta(days=1)
        if days_present == 0:
            continue
        out.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "total": round(total, 2),
            "avg_per_day": round(total / days_present, 2),
        })
    return out


def _build_ai_payload(
    summaries: list[AccountSummary],
    insights: dict[str, list[str]],
    commitments: "CommitmentSummary | None",
    df: pl.DataFrame,
    report_date: date,
    budgets: dict[str, float] | None = None,
) -> dict:
    """JSON-friendly snapshot of the report for the LLM.

    Includes up to 90 days of daily history at both the org and per-account
    level, pre-computed 7/14/30/90-day averages, weekly rollups, MTD totals,
    end-of-month projections, and (optionally) per-account monthly budgets so
    the model can reason about variance, burn, and savings opportunities
    without needing to re-aggregate.
    """
    grand_y = sum(s.total_yesterday for s in summaries)
    grand_p = sum(s.total_day_before for s in summaries)
    budgets = budgets or {}

    org_daily_90 = _org_daily_series(df, report_date, AI_HISTORY_DAYS)
    org_weekly = _weekly_buckets(org_daily_90, report_date, weeks=13)

    org_avg_7d = _window_avg_org(df, report_date, 7)
    org_mtd = _mtd_org(df, report_date)
    org_forecast = _eom_projection(org_mtd, org_avg_7d, report_date)
    org_budget_total = sum(
        float(v) for v in budgets.values() if v and float(v) > 0
    ) if budgets else 0.0
    org_budget = _budget_view(
        org_budget_total or None, org_mtd, org_forecast["eom_projection"]
    )

    accounts_payload = []
    for s in summaries:
        top = s.services.head(AI_TOP_SERVICES)
        services = [
            {
                "service": row["service"],
                "yesterday": round(row["yesterday"], 2),
                "day_before": round(row["day_before"], 2),
                "avg_30d": round(row["avg_30d"], 2),
                "avg_90d": round(row["avg_90d"], 2),
            }
            for row in top.iter_rows(named=True)
        ]
        acct_daily = _account_daily_series(
            df, s.account_id, report_date, AI_HISTORY_DAYS
        )
        acct_avg_7d = _window_avg_account(df, s.account_id, report_date, 7)
        acct_mtd = _mtd_account(df, s.account_id, report_date)
        acct_forecast = _eom_projection(acct_mtd, acct_avg_7d, report_date)
        acct_budget_raw = budgets.get(s.account_id) if budgets else None
        try:
            acct_budget_val = float(acct_budget_raw) if acct_budget_raw is not None else None
        except (TypeError, ValueError):
            acct_budget_val = None
        acct_budget = _budget_view(
            acct_budget_val, acct_mtd, acct_forecast["eom_projection"]
        )
        accounts_payload.append({
            "account_id": s.account_id,
            "account_name": s.account_name,
            "totals": {
                "yesterday": round(s.total_yesterday, 2),
                "day_before": round(s.total_day_before, 2),
                "avg_7d": round(acct_avg_7d, 2),
                "avg_14d": round(_window_avg_account(df, s.account_id, report_date, 14), 2),
                "avg_30d": round(s.total_avg_30d, 2),
                "avg_90d": round(s.total_avg_90d, 2),
            },
            "burn": acct_forecast,
            "budget": acct_budget,
            "top_services": services,
            "insights": insights.get(s.account_id, []),
            "daily_90d": acct_daily,
            "weekly_13w": _weekly_buckets(acct_daily, report_date, weeks=13),
        })

    commitments_payload = None
    if commitments is not None:
        commitments_payload = {
            "lookback_days": commitments.days,
            "sp_avg_util_pct": round(commitments.sp_avg_util_pct, 2),
            "sp_avg_coverage_pct": round(commitments.sp_avg_coverage_pct, 2),
            "sp_total_unused": round(commitments.sp_total_unused, 2),
            "sp_total_savings": round(commitments.sp_total_savings, 2),
            "sp_total_on_demand": round(commitments.sp_total_on_demand, 2),
            "sp_avg_daily_on_demand": round(commitments.sp_avg_daily_on_demand, 2),
            "sp_effective_discount_pct": round(commitments.sp_effective_discount_pct, 2),
            "sp_safe_buy_hourly": round(commitments.sp_safe_buy_hourly, 4),
            "sp_safe_buy_p10_hourly": round(commitments.sp_safe_buy_p10_hourly, 4),
            "sp_safe_buy_p25_hourly": round(commitments.sp_safe_buy_p25_hourly, 4),
            "sp_safe_buy_p50_hourly": round(commitments.sp_safe_buy_p50_hourly, 4),
            "ri_avg_util_pct": round(commitments.ri_avg_util_pct, 2),
            "ri_avg_coverage_pct": round(commitments.ri_avg_coverage_pct, 2),
            "ri_total_unused_hours": round(commitments.ri_total_unused_hours, 2),
            "ri_total_savings": round(commitments.ri_total_savings, 2),
            "ri_total_on_demand": round(commitments.ri_total_on_demand, 2),
            "ri_avg_daily_on_demand": round(commitments.ri_avg_daily_on_demand, 2),
            "ri_effective_discount_pct": round(commitments.ri_effective_discount_pct, 2),
            "ri_safe_buy_hourly": round(commitments.ri_safe_buy_hourly, 4),
            "ri_safe_buy_p10_hourly": round(commitments.ri_safe_buy_p10_hourly, 4),
            "ri_safe_buy_p25_hourly": round(commitments.ri_safe_buy_p25_hourly, 4),
            "ri_safe_buy_p50_hourly": round(commitments.ri_safe_buy_p50_hourly, 4),
            "on_demand_proj_total_90d": round(commitments.on_demand_proj_total, 2),
            "on_demand_trend_slope_per_day": round(commitments.on_demand_trend_slope, 4),
            "sp_expiring_30d_hourly": round(commitments.sp_expiring_30d_hourly, 4),
            "sp_expiring_60d_hourly": round(commitments.sp_expiring_60d_hourly, 4),
            "sp_expiring_90d_hourly": round(commitments.sp_expiring_90d_hourly, 4),
            "per_account": [
                {
                    "account_id": r["account_id"],
                    "account_name": r.get("account_name", ""),
                    "sp_on_demand": round(r["sp_on_demand"], 2),
                    "sp_covered": round(r["sp_covered"], 2),
                    "sp_coverage_pct": round(r["sp_coverage_pct"], 2),
                    "ri_on_demand": round(r["ri_on_demand"], 2),
                    "ri_covered": round(r["ri_covered"], 2),
                    "ri_coverage_pct": round(r["ri_coverage_pct"], 2),
                    "ri_util_pct": round(r["ri_util_pct"], 2),
                }
                for r in commitments.per_account
            ],
        }

    return {
        "report_date": report_date.isoformat(),
        "history_days": AI_HISTORY_DAYS,
        "org_totals": {
            "yesterday": round(grand_y, 2),
            "day_before": round(grand_p, 2),
            "avg_7d": round(org_avg_7d, 2),
            "avg_14d": round(_window_avg_org(df, report_date, 14), 2),
            "avg_30d": round(_window_avg_org(df, report_date, 30), 2),
            "avg_90d": round(_window_avg_org(df, report_date, 90), 2),
        },
        "org_burn": org_forecast,
        "org_budget": org_budget,
        "org_daily_90d": org_daily_90,
        "org_weekly_13w": org_weekly,
        "accounts": accounts_payload,
        "commitments": commitments_payload,
    }


_AI_PROMPT = """You are a senior AWS FinOps analyst writing a daily review
for an engineering team. You have up to 90 days of daily cost history
(org-level and per-account), pre-computed averages across 7/14/30/90-day
windows, weekly rollups for the last 13 weeks, month-to-date totals, naive
end-of-month projections (MTD + 7d-avg × days_remaining), optional per-account
monthly budgets, and a 90-day Savings Plan / Reserved Instance commitment
snapshot (per-account coverage, percentile safe-buy tiers, effective discount
%, expiring-SP hourly buckets, and a linear 90-day projection of SP-eligible
on-demand spend). Analyze the JSON below and produce an HTML fragment.

Output rules:
- Output ONLY an HTML fragment (no <html>, <head>, <body>, no markdown fences).
- Use these tags only: <h3>, <h4>, <p>, <ul>, <ol>, <li>, <strong>, <em>, <code>.
- No inline styles, no scripts, no images, no links.
- Open with a 2-3 sentence executive summary in a <p>: today's spend vs 7d/30d/90d
  averages, the dominant trend direction, the projected month-end vs budget (if
  known), and the single biggest savings lever.

Then produce these sections in order (each as <h3> + content). Skip a section
only if the data genuinely doesn't support it; do not pad.

  1. Burn &amp; Forecast — Cite org_burn (MTD, run_rate_daily, eom_projection,
     days_remaining). If org_budget is present, state MTD %, projected %, and
     projected overrun/headroom in USD. List the top 3 accounts ranked by
     projected_pct_of_budget (overruns first); flag any account already past
     100% of MTD pace.
  2. Trend Snapshot — Compare today vs 7d, 14d, 30d, and 90d averages at the
     org level. State whether spend is accelerating, plateauing, or declining,
     and quantify the slope (e.g. "+18% vs 30d, +9% vs 90d"). Note WoW change
     using the last two weekly buckets.
  3. Historic Patterns — Use org_weekly_13w and the 90-day daily series to
     identify weekday/weekend seasonality, recurring spikes, growth vs flat
     baselines, and structural shifts (e.g. step-up after a launch). Be
     concrete with week ranges and USD figures.
  4. Account Hotspots — Accounts driving the biggest absolute and relative
     changes vs their own 30d/90d baselines, and accounts with the worst
     budget variance. Lead with the top 2-3.
  5. Service Drivers — Services responsible for the largest movements
     org-wide. Combine signal across accounts where the same service is rising.
  6. Anomalies &amp; Watchlist — Surprising spikes AND unexpected drops
     (drops can indicate a broken pipeline / lost data ingestion, not a win —
     treat them as red flags). Include "appeared" / "disappeared" entries
     from the rule-based insights. Items that warrant follow-up tomorrow.
  7. Savings Opportunities — A dedicated, AWS-specific savings analysis.
     Walk this checklist explicitly and report each item, even briefly:
       a. Commitments — Savings Plans / RI utilization gaps and whether the
          safe-buy hourly recommendation is meaningful given on-demand spend.
          Stable baselines that are good commitment candidates.
       b. Idle / decaying — services where avg_90d &gt;&gt; avg_7d, or where
          the daily series shows a steady decline.
       c. Data transfer — flag if "DataTransfer", "AWS Data Transfer", or
          inter-AZ / NAT-driven services are large and rising. NAT Gateway
          and cross-AZ transfer are common silent killers.
       d. Storage tiering — S3 Standard with stable baselines (Lifecycle to
          IA / Glacier candidate); EBS spend that may be gp2 (gp3 saves ~20%);
          snapshot accumulation.
       e. Compute rightsizing — services with flat baselines and high spend
          that suggest over-provisioned EC2 / RDS / OpenSearch / ElastiCache.
       f. Idle infra patterns — load balancers, NAT, RDS, EKS that bill
          steadily but show no day-of-week variance (suggestive of idle).
     Quantify each opportunity in $/day AND $/month. If you cannot estimate,
     say so rather than guessing.
  8. Data Caveats — One short bulleted list. Call out: short baselines (new
     accounts), one-day spikes that distort an average, missing days, or
     services with too-volatile-to-trend behavior. Skip if nothing applies.
  9. Recommended Actions — Group as <h4>P0 (this week)</h4>, <h4>P1 (this
     month)</h4>, <h4>P2 (this quarter)</h4>. Under each, 1-3 bullets, each
     naming the account/service AND an expected $/month impact range
     (e.g. "$200-$400/mo"). Prioritize by impact × confidence.

Quality bar:
- Be quantitative. Cite USD amounts and percentages directly from the data.
- Prefer comparisons across multiple windows over single-day deltas.
- Treat unexpected drops as red flags, not wins.
- Do NOT invent services, accounts, services, or numbers not present in the JSON.
- If a number is zero, missing, or not estimable, say so plainly. No fabrication.
- Keep it skimmable — short paragraphs, tight bullets, bold key figures with
  <strong>.

Data:
"""


def _strip_html_fences(text: str) -> str:
    """Strip ```html ... ``` fences if the model includes them anyway."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


def generate_ai_analysis(payload: dict, api_key: str) -> str | None:
    """Call Gemini AI Studio. Returns an HTML fragment or None on error."""
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _AI_PROMPT + json.dumps(payload, separators=(",", ":"))}
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 4096,
            "responseMimeType": "text/plain",
        },
    }
    url = GEMINI_ENDPOINT.format(model=_gemini_model(), key=api_key)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning("Gemini call failed: %s", e)
        return None
    except json.JSONDecodeError as e:
        logger.warning("Gemini returned non-JSON: %s", e)
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        logger.warning("Gemini response missing text: %s", str(data)[:300])
        return None

    return _strip_html_fences(text) or None


def maybe_generate_ai_analysis(
    summaries: list[AccountSummary],
    insights: dict[str, list[str]],
    commitments: "CommitmentSummary | None",
    df: pl.DataFrame,
    report_date: date,
    budgets: dict[str, float] | None = None,
) -> tuple[str | None, bool]:
    """Generate AI analysis if a Gemini API key is in the env.

    Returns (analysis_html, enabled). `enabled` reflects whether a key was
    found, regardless of whether the API call ultimately succeeded.

    `budgets` is an optional {account_id: monthly_usd} map. When provided,
    per-account and org-level burn-vs-budget figures are added to the payload
    and the AI is steered toward variance-aware recommendations.
    """
    key = _ai_api_key()
    if not key:
        logger.info("No GEMINI_API_KEY in env — skipping AI analysis")
        return None, False
    logger.info("Generating AI analysis with %s", _gemini_model())
    payload = _build_ai_payload(
        summaries, insights, commitments, df, report_date, budgets=budgets
    )
    analysis = generate_ai_analysis(payload, key)
    if analysis:
        logger.info("AI analysis generated (%d chars)", len(analysis))
    return analysis, True


def _budgets_from_env() -> dict[str, float] | None:
    """Parse MONTHLY_BUDGETS_JSON env var as a {account_id: usd} map.

    Useful for CLI runs without going through the SSM bundle.
    """
    raw = os.environ.get("MONTHLY_BUDGETS_JSON")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("MONTHLY_BUDGETS_JSON is not valid JSON; ignoring")
        return None
    if not isinstance(parsed, dict):
        logger.warning("MONTHLY_BUDGETS_JSON must be a JSON object; ignoring")
        return None
    out: dict[str, float] = {}
    for k, v in parsed.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            logger.warning("Skipping non-numeric budget for account %s", k)
    return out or None


def _render_ai_panel_html(analysis: str | None, has_key: bool, model: str) -> str:
    """Render the AI analysis as a panel at the top of the Daily tab.

    When AI is not configured we render nothing — the Daily tab should not be
    cluttered with a setup nag on every report. Configuration help lives in
    the README. When configured but generation failed, show a small notice so
    the failure isn't silent.
    """
    if not has_key:
        return ""
    if analysis is None:
        return (
            '<section class="panel ai-analysis">'
            '<div class="ai-head">'
            '<h2>AI Analysis</h2>'
            f'<span class="tag">{html.escape(model)}</span>'
            "</div>"
            '<div class="callout">'
            "<strong>Generation failed.</strong> Check the cost_reporter logs "
            "for the underlying error (network, quota, or API key issue)."
            "</div></section>"
        )
    return (
        '<section class="panel ai-analysis">'
        '<div class="ai-head">'
        '<h2>AI Analysis <span class="ai-sub">— trends, patterns &amp; savings</span></h2>'
        f'<span class="tag">{html.escape(model)}</span>'
        "</div>"
        + analysis +
        "</section>"
    )


def write_report(
    summaries: list[AccountSummary],
    insights: dict[str, list[str]],
    report_date: date,
    out_dir: Path,
    df: pl.DataFrame | None = None,
    commitments: "CommitmentSummary | None" = None,
    ai_analysis: str | None = None,
    ai_enabled: bool = False,
) -> Path:
    """Render a single-file HTML report (charts via ApexCharts CDN).

    `df` is the 95-day cost DataFrame; when omitted (e.g. tests), charts render
    empty but the page still works.
    """
    df = df if df is not None else pl.DataFrame(
        schema={"date": pl.Date, "account_id": pl.Utf8,
                "service": pl.Utf8, "cost": pl.Float64}
    )

    grand_y = sum(s.total_yesterday for s in summaries)
    grand_p = sum(s.total_day_before for s in summaries)
    grand_30 = sum(s.total_avg_30d for s in summaries)
    grand_90 = sum(s.total_avg_90d for s in summaries)

    payload = {
        "org_trend": _org_daily_series(df, report_date, HTML_TREND_DAYS),
        "accounts": [_build_account_payload(s, df, TOP_N_TABLE) for s in summaries],
        "commitments": _commitments_payload(commitments),
    }

    header = (
        '<div class="page-head">'
        f"<h1>AWS Cost Report — {report_date} (UTC)</h1>"
        '<button id="theme-toggle" class="theme-toggle" type="button"'
        ' aria-label="Toggle color theme">☀ Light</button>'
        '</div>'
        f'<div class="meta">'
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        " · AmortizedCost · excludes "
        f"{', '.join(EXCLUDED_RECORD_TYPES)}"
        f" and lumpy services ({', '.join(sorted(MONTHLY_LUMP_SERVICES))})."
        "</div>"
        '<div class="kpi-row">'
        f'<div class="kpi"><div class="label">Org total (Day)</div>'
        f'<div class="value">{fmt_usd(grand_y)}</div></div>'
        f'<div class="kpi"><div class="label">DoD</div>'
        f'<div class="value">{_delta_pct_html(grand_y, grand_p)}</div>'
        f'<div class="sub">was {fmt_usd(grand_p)}</div></div>'
        f'<div class="kpi"><div class="label">30d avg</div>'
        f'<div class="value">{fmt_usd(grand_30)}</div>'
        f'<div class="sub">vs 30d {_delta_pct_html(grand_y, grand_30)}</div></div>'
        f'<div class="kpi"><div class="label">90d avg</div>'
        f'<div class="value">{fmt_usd(grand_90)}</div></div>'
        "</div>"
        f'<div class="chart-wrap"><div id="org-trend"></div></div>'
    )

    overview = (
        '<section class="panel">'
        + header
        + _render_insights_html(summaries, insights)
        + _render_summary_table_html(summaries)
        + "</section>"
    )

    daily_panel = (
        '<div id="tab-daily" class="tab-panel active">'
        + _render_ai_panel_html(ai_analysis, ai_enabled, _gemini_model())
        + overview
        + "".join(_render_account_card_html(s) for s in summaries)
        + "</div>"
    )
    commitments_panel = (
        '<div id="tab-commitments" class="tab-panel">'
        + _render_commitments_html(commitments)
        + "</div>"
    )
    tabs_nav = (
        '<nav class="tabs">'
        '<button data-tab="daily" class="active" type="button">Daily</button>'
        '<button data-tab="commitments" type="button">Commitments</button>'
        "</nav>"
    )
    body = tabs_nav + daily_panel + commitments_panel

    theme_init = (
        "(function(){try{var t=localStorage.getItem('cost-report-theme');"
        "if(!t){t=window.matchMedia&&window.matchMedia"
        "('(prefers-color-scheme: light)').matches?'light':'dark';}"
        "document.documentElement.setAttribute('data-theme',t);}catch(_){}})();"
    )

    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>AWS Cost Report — {report_date}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<script>{theme_init}</script>"
        f"<style>{_HTML_STYLE}</style></head><body>"
        + body
        + '<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>'
        f"<script>window.__COST_DATA__ = {_safe_json(payload)};</script>"
        f"<script>{_HTML_SCRIPT}</script>"
        "</body></html>"
    )

    path = out_dir / "report.html"
    path.write_text(doc)
    return path


# -----------------------------------------------------------------------------
# Main (procedural flow)
# -----------------------------------------------------------------------------
def main() -> int:
    setup_logging()
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rpt_date = resolve_report_day(args.date)
    start = rpt_date - timedelta(days=LOOKBACK_DAYS - 1)
    end_exclusive = rpt_date + timedelta(days=1)
    logger.info("Report day: %s UTC (window %s -> %s inclusive)", rpt_date, start, rpt_date)

    accounts = fetch_account_map()
    df = build_cost_dataframe(start, end_exclusive)

    summaries: list[AccountSummary] = []
    insights: dict[str, list[str]] = {}
    for acct_id in sorted(accounts):
        name = accounts[acct_id]
        logger.info("Processing %s (%s)", name, acct_id)
        summary = build_account_summary(df, acct_id, name, rpt_date)
        summaries.append(summary)
        insights[acct_id] = build_insights(summary)

    summaries.sort(key=lambda s: s.total_yesterday, reverse=True)

    commitments = build_commitment_summary(rpt_date, account_map=accounts)
    ai_analysis, ai_enabled = maybe_generate_ai_analysis(
        summaries, insights, commitments, df, rpt_date,
        budgets=_budgets_from_env(),
    )
    report_path = write_report(
        summaries,
        insights,
        rpt_date,
        out_dir,
        df,
        commitments=commitments,
        ai_analysis=ai_analysis,
        ai_enabled=ai_enabled,
    )
    logger.info("Wrote %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
