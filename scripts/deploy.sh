#!/usr/bin/env bash
# Bootstrap the shared S3 bucket (state + reports), then run tofu init + apply.
# The bucket is intentionally NOT managed by tofu to avoid the chicken-and-egg
# problem of storing state in a bucket that tofu would create/destroy.
# Fully idempotent: safe to run on every deploy.
#
# Usage:
#   PROJECT_PREFIX=treebo ./scripts/deploy.sh
#   PROJECT_PREFIX=treebo AWS_REGION=ap-south-1 ./scripts/deploy.sh
set -euo pipefail

PROJECT_PREFIX="${PROJECT_PREFIX:-}"
PROJECT_NAME="aws-cost-reporter-prod"
REGION="${AWS_REGION:-us-east-1}"

if [[ -n "$PROJECT_PREFIX" ]]; then
  BUCKET="${PROJECT_PREFIX}-${PROJECT_NAME}"
else
  BUCKET="${PROJECT_NAME}"
fi

# ---------------------------------------------------------------------------
# Bootstrap S3 bucket
# ---------------------------------------------------------------------------
echo "==> Ensuring S3 bucket: $BUCKET (region: $REGION)"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "    Creating bucket..."
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
fi

# Applied every run — all are idempotent PUT operations.
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration '{
    "Rules":[{
      "ID":"expire-old-reports",
      "Status":"Enabled",
      "Filter":{},
      "Expiration":{"Days":400},
      "NoncurrentVersionExpiration":{"NoncurrentDays":30}
    }]
  }'

echo "    Bucket ready."

# ---------------------------------------------------------------------------
# Tofu
# ---------------------------------------------------------------------------
cd "$(dirname "$0")/../tofu"

TOFU_VARS=("-var=region=${REGION}")
[[ -n "$PROJECT_PREFIX" ]] && TOFU_VARS+=("-var=project_prefix=${PROJECT_PREFIX}")

echo "==> Running tofu init (backend bucket: $BUCKET)"
tofu init -upgrade -reconfigure -backend-config="bucket=${BUCKET}" -backend-config="region=${REGION}"

echo "==> Running tofu apply"
tofu apply -auto-approve "${TOFU_VARS[@]}"
