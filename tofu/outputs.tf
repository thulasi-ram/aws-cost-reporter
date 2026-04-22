output "lambda_function_name" {
  description = "Name of the Lambda function (for manual invoke / logs)"
  value       = aws_lambda_function.reporter.function_name
}

output "s3_bucket_name" {
  description = "Bucket holding markdown reports and chart PNGs"
  value       = aws_s3_bucket.reports.id
}

output "dynamodb_table_name" {
  description = "History + run-marker table"
  value       = aws_dynamodb_table.history.name
}

output "slack_webhook_ssm_parameter" {
  description = "Set the real webhook URL with: aws ssm put-parameter --name <name> --value <url> --type SecureString --overwrite"
  value       = aws_ssm_parameter.slack_webhook.name
}

output "schedule" {
  description = "Effective EventBridge schedule expression"
  value       = aws_cloudwatch_event_rule.daily.schedule_expression
}

output "cloudwatch_log_group" {
  description = "Tail with: aws logs tail <name> --follow"
  value       = aws_cloudwatch_log_group.lambda.name
}
