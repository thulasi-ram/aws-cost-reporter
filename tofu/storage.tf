# ---------------------------------------------------------------------------
# DynamoDB — daily history (pk=account_id, sk=date#service)
# and run markers (pk="run", sk=date) for idempotency.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "history" {
  name         = "${local.full_name}-history"
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
  name        = "/${local.full_name}/slack-webhook-url"
  description = "Slack incoming webhook URL. Set the real value via AWS CLI after apply."
  type        = "SecureString"
  value       = "PLACEHOLDER-set-via-aws-ssm-put-parameter"

  lifecycle {
    ignore_changes = [value]
  }
}
