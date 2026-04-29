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
# SSM SecureString — consolidated JSON secret bundle for this app.
# One parameter holds every secret the Lambda needs (Slack webhook,
# Gemini API key, etc.) so we can add new keys without provisioning new
# resources or expanding IAM.
#
# Expected JSON shape:
#   {
#     "slack_webhook_url": "https://hooks.slack.com/services/...",
#     "gemini_api_key":    "AIza...",        // optional — enables AI Analysis tab
#     "gemini_model":      "gemini-2.5-flash" // optional — override default model
#   }
#
# Tofu creates the parameter shell with a placeholder. Set the real value
# out-of-band (so it never lives in state files / Tofu plans):
#   aws ssm put-parameter --name <name> \
#       --value '{"slack_webhook_url":"...","gemini_api_key":"..."}' \
#       --type SecureString --overwrite
# The `ignore_changes` below keeps Tofu from clobbering it on apply.
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "secrets" {
  name        = "/${local.full_name}/secrets"
  description = "JSON secret bundle (slack_webhook_url, gemini_api_key, ...). Set via AWS CLI after apply."
  type        = "SecureString"
  value = jsonencode({
    slack_webhook_url = "PLACEHOLDER"
    gemini_api_key    = ""
    gemini_model      = "gemini-2.5-flash"
  })

  lifecycle {
    ignore_changes = [value]
  }
}
