output "lambda_function_name" {
  description = "Name of the Lambda function (for manual invoke / logs)"
  value       = aws_lambda_function.reporter.function_name
}

output "s3_bucket_name" {
  description = "Bucket holding tofu state, lambda zip, and markdown reports"
  value       = local.full_name
}

output "dynamodb_table_name" {
  description = "History + run-marker table"
  value       = aws_dynamodb_table.history.name
}

output "secrets_ssm_parameter" {
  description = <<-EOT
    Consolidated JSON secret + config bundle. Set with:
      aws ssm put-parameter --name <name> \
        --value '{"slack_webhook_url":"https://hooks.slack.com/...","gemini_api_key":"AIza...","gemini_model":"gemini-2.5-flash"}' \
        --type SecureString --overwrite
  EOT
  value       = aws_ssm_parameter.secrets.name
}

output "schedule" {
  description = "Effective EventBridge schedule expression"
  value       = aws_cloudwatch_event_rule.daily.schedule_expression
}

output "cloudwatch_log_group" {
  description = "Tail with: aws logs tail <name> --follow"
  value       = aws_cloudwatch_log_group.lambda.name
}
