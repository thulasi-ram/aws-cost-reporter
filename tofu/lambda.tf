# ---------------------------------------------------------------------------
# ECR repository — hosts the Lambda container image.
# Must be created (and an image pushed) before the Lambda function exists.
# See scripts/deploy.sh for the build + push workflow.
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "this" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# IAM execution role
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = "${local.name}-exec"

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
  name = "${local.name}-runtime"
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
        Resource = "${aws_s3_bucket.reports.arn}/*"
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
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "reporter" {
  function_name = local.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_seconds

  environment {
    variables = {
      S3_BUCKET               = aws_s3_bucket.reports.id
      DYNAMODB_TABLE          = aws_dynamodb_table.history.name
      SLACK_WEBHOOK_SSM_PARAM = aws_ssm_parameter.slack_webhook.name
      ENVIRONMENT             = var.environment
      PRESIGNED_URL_TTL_DAYS  = "7"
    }
  }

  depends_on = [
    aws_iam_role_policy.runtime,
    aws_cloudwatch_log_group.lambda,
  ]
}

# ---------------------------------------------------------------------------
# EventBridge schedule — 03:30 UTC daily = 09:00 IST
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${local.name}-daily"
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
  alarm_name          = "${local.name}-errors"
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
