# ---------------------------------------------------------------------------
# Stable report links — Function URL that redirects to a fresh presigned URL.
#
# Presigned URLs die with the credentials that signed them, and the reporter
# signs with its role's temporary STS session (hours), so Slack links were
# expiring the same day. This function re-signs on every click instead; see
# link_handler.py. Its own role can only read reports/ and the secret bundle.
# ---------------------------------------------------------------------------
locals {
  links_name = "${local.full_name}-links"
}

# Zipped outside build/: build.sh wipes that directory during apply, after
# this archive has been produced.
data "archive_file" "links_zip" {
  type        = "zip"
  source_file = "${path.module}/../link_handler.py"
  output_path = "${path.module}/.build/links.zip"
}

resource "aws_iam_role" "links" {
  name = "${local.links_name}-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "links_basic" {
  role       = aws_iam_role.links.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "links_runtime" {
  name = "${local.links_name}-runtime"
  role = aws_iam_role.links.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadReports"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${local.full_name}/reports/*"
      },
      {
        Sid      = "SSMReadSecrets"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.secrets.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "links" {
  name              = "/aws/lambda/${local.links_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "links" {
  function_name    = local.links_name
  role             = aws_iam_role.links.arn
  package_type     = "Zip"
  runtime          = "python3.12"
  handler          = "link_handler.handler"
  filename         = data.archive_file.links_zip.output_path
  source_code_hash = data.archive_file.links_zip.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      S3_BUCKET         = local.full_name
      SECRETS_SSM_PARAM = aws_ssm_parameter.secrets.name
    }
  }

  depends_on = [
    aws_iam_role_policy.links_runtime,
    aws_cloudwatch_log_group.links,
  ]
}

# Public by design: the HMAC token in each link is the access control, same
# bearer model as the presigned URLs it replaces — just without the expiry.
resource "aws_lambda_function_url" "links" {
  function_name      = aws_lambda_function.links.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "links_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.links.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# AWS now also requires lambda:InvokeFunction for NONE-auth URLs. Provider
# 5.x can't scope it with lambda:InvokedViaFunctionUrl, so it is unscoped;
# a direct invoke gets exactly what the URL gives — nothing without a valid
# token.
resource "aws_lambda_permission" "links_invoke" {
  statement_id  = "AllowPublicInvokeForFunctionUrl"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.links.function_name
  principal     = "*"
}
