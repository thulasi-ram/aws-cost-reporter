# ---------------------------------------------------------------------------
# IAM execution role
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = "${local.full_name}-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "runtime" {
  name = "${local.full_name}-runtime"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CostExplorer"
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage"]
        Resource = "*"
      },
      {
        Sid      = "OrganizationsRead"
        Effect   = "Allow"
        Action   = ["organizations:ListAccounts"]
        Resource = "*"
      },
      {
        Sid      = "S3Reports"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "arn:aws:s3:::${local.full_name}/*"
      },
      {
        Sid    = "DynamoHistory"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:BatchWriteItem",
        ]
        Resource = aws_dynamodb_table.history.arn
      },
      {
        Sid      = "SSMReadWebhook"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.slack_webhook.arn
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda function (container image)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.full_name}"
  retention_in_days = var.log_retention_days
}

# Rebuilds build/function.zip whenever source files or the build script change.
# Runs before the Lambda resource so the zip is always fresh at apply time.
resource "null_resource" "build_zip" {
  triggers = {
    cost_reporter  = filesha256("${path.module}/../cost_reporter.py")
    lambda_handler = filesha256("${path.module}/../lambda_handler.py")
    build_script   = filesha256("${path.module}/../scripts/build.sh")
    lockfile       = filesha256("${path.module}/../uv.lock")
  }

  provisioner "local-exec" {
    command     = "./scripts/build.sh"
    working_dir = "${path.module}/.."
  }
}

resource "aws_s3_object" "lambda_zip" {
  bucket = local.full_name
  key    = "lambda/function.zip"
  source = "${path.module}/../build/function.zip"

  # Hash of the inputs that drive the build, NOT the built zip itself —
  # filemd5(zip) is unstable between plan and apply because null_resource
  # regenerates the zip during apply.
  source_hash = sha256(join("", [
    filesha256("${path.module}/../cost_reporter.py"),
    filesha256("${path.module}/../lambda_handler.py"),
    filesha256("${path.module}/../uv.lock"),
    filesha256("${path.module}/../scripts/build.sh"),
  ]))

  depends_on = [null_resource.build_zip]
}

resource "aws_lambda_function" "reporter" {
  function_name = local.full_name
  role          = aws_iam_role.lambda.arn
  package_type  = "Zip"
  runtime       = "python3.12"
  handler       = "lambda_handler.handler"

  # Upload via S3 — direct API upload is capped at ~70 MB per request, which
  # polars + its Rust runtime exceeds. S3 avoids that limit.
  s3_bucket = aws_s3_object.lambda_zip.bucket
  s3_key    = aws_s3_object.lambda_zip.key

  # Hash of source files rather than the zip — plan never needs the zip to
  # exist; null_resource.build_zip always produces a fresh zip before apply.
  source_code_hash = sha256(join("", [
    filesha256("${path.module}/../cost_reporter.py"),
    filesha256("${path.module}/../lambda_handler.py"),
  ]))

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds

  environment {
    variables = {
      S3_BUCKET               = local.full_name
      DYNAMODB_TABLE          = aws_dynamodb_table.history.name
      SLACK_WEBHOOK_SSM_PARAM = aws_ssm_parameter.slack_webhook.name
      ENVIRONMENT             = var.environment
      PRESIGNED_URL_TTL_DAYS  = "7"
    }
  }

  depends_on = [
    aws_iam_role_policy.runtime,
    aws_cloudwatch_log_group.lambda,
    aws_s3_object.lambda_zip,
  ]
}

# ---------------------------------------------------------------------------
# EventBridge schedule — 03:30 UTC daily = 09:00 IST
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${local.full_name}-daily"
  description         = "Trigger cost reporter daily at 03:30 UTC (09:00 IST)"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "lambda"
  arn       = aws_lambda_function.reporter.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reporter.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}

# ---------------------------------------------------------------------------
# CloudWatch alarm — fires if the Lambda has ANY errors in a 24h window
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.full_name}-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 86400
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Cost reporter Lambda failed at least once in the last 24h"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.reporter.function_name
  }
}
