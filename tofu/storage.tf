# ---------------------------------------------------------------------------
# S3 bucket — report markdown + per-account chart PNGs
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "reports" {
  bucket_prefix = "${local.name}-"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    id     = "expire-old-reports"
    status = "Enabled"

    # Empty filter = rule applies to every object in the bucket.
    filter {}

    expiration {
      days = 400
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# ---------------------------------------------------------------------------
# DynamoDB — daily history (pk=account_id, sk=date#service)
# and run markers (pk="run", sk=date) for idempotency.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "history" {
  name         = "${local.name}-history"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# SSM SecureString — Slack incoming webhook URL.
# Tofu creates the parameter shell. Set the real value out-of-band:
#   aws ssm put-parameter --name <name> --value <url> \
#       --type SecureString --overwrite
# The `ignore_changes` below prevents Tofu from clobbering the real value
# on subsequent applies.
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "slack_webhook" {
  name        = "/${local.name}/slack-webhook-url"
  description = "Slack incoming webhook URL. Set the real value via AWS CLI after apply."
  type        = "SecureString"
  value       = "PLACEHOLDER-set-via-aws-ssm-put-parameter"

  lifecycle {
    ignore_changes = [value]
  }
}
