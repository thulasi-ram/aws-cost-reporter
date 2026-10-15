"""Stable report links: a Lambda Function URL that redirects to a fresh
presigned S3 URL on every click.

Why this exists: a presigned URL is only valid while the credentials that
signed it are. The reporter Lambda signs with its execution role's temporary
STS credentials, which expire within hours — so "7-day" links were dying the
same day with `ExpiredToken`. This function signs a 5-minute URL at click
time instead, so the link in Slack keeps working for as long as the report
object exists (400-day bucket lifecycle).

Link shape:  {function_url}r/{YYYY-MM-DD}?t={token}
`token` is an HMAC of the report key under `report_link_key` from the SSM
bundle, so dates can't be enumerated by guessing URLs. Anything that doesn't
verify gets the same bare 404 as a missing report.

Kept dependency-free (boto3 ships with the runtime) so it deploys as its own
tiny zip; lambda_handler imports `report_link` from here to mint links.

Environment:
    S3_BUCKET          — bucket holding reports/
    SECRETS_SSM_PARAM  — SSM SecureString JSON bundle with `report_link_key`
"""

import base64
import hashlib
import hmac
import json
import os
import re

import boto3

REDIRECT_TTL_SECONDS = 300
_PATH_RE = re.compile(r"^/r/(\d{4}-\d{2}-\d{2})/?$")

_S3 = boto3.client("s3")
_SSM = boto3.client("ssm")
_secret_cache: str | None = None


def report_key(report_date: str) -> str:
    return f"reports/{report_date}/report.html"


def link_token(secret: str, report_date: str) -> str:
    mac = hmac.new(
        secret.encode(), report_key(report_date).encode(), hashlib.sha256
    ).digest()
    # 128 bits is plenty for an unguessable link and keeps the URL short.
    return base64.urlsafe_b64encode(mac[:16]).decode().rstrip("=")


def report_link(base_url: str, secret: str, report_date: str) -> str:
    """Stable link for a report day. `base_url` is the Function URL."""
    return (
        f"{base_url.rstrip('/')}/r/{report_date}"
        f"?t={link_token(secret, report_date)}"
    )


def _secret() -> str:
    global _secret_cache
    if _secret_cache is None:
        resp = _SSM.get_parameter(
            Name=os.environ["SECRETS_SSM_PARAM"], WithDecryption=True
        )
        _secret_cache = str(
            json.loads(resp["Parameter"]["Value"]).get("report_link_key") or ""
        )
    return _secret_cache


def _not_found() -> dict:
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "text/plain", "Cache-Control": "no-store"},
        "body": "Report not found.",
    }


def handler(event: dict, context: object) -> dict:
    match = _PATH_RE.match(event.get("rawPath") or "")
    token = (event.get("queryStringParameters") or {}).get("t", "")
    secret = _secret()
    if not match or not secret:
        return _not_found()
    report_date = match.group(1)
    if not hmac.compare_digest(link_token(secret, report_date), token):
        return _not_found()

    bucket = os.environ["S3_BUCKET"]
    key = report_key(report_date)
    try:
        _S3.head_object(Bucket=bucket, Key=key)
    except _S3.exceptions.ClientError:
        # No s3:ListBucket on this role, so a missing key surfaces as 403
        # rather than 404 — treat every miss the same.
        return _not_found()

    url = _S3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=REDIRECT_TTL_SECONDS,
    )
    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    }
