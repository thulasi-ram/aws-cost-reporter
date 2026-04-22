terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "OpenTofu"
    }
  }
}

variable "region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix for all resource names"
  type        = string
  default     = "aws-cost-reporter"
}

variable "environment" {
  description = "Environment label (prod / dev / staging)"
  type        = string
  default     = "prod"
}

variable "schedule_expression" {
  description = "EventBridge cron. Default 03:30 UTC = 09:00 IST, Mon-Sun"
  type        = string
  default     = "cron(30 3 * * ? *)"
}

variable "lambda_memory_mb" {
  description = "Lambda memory. polars + matplotlib comfortably fit in 1536"
  type        = number
  default     = 1536
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. CE call + 10 charts + uploads finishes well inside this"
  type        = number
  default     = 300
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda"
  type        = number
  default     = 30
}

locals {
  name = "${var.project_name}-${var.environment}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
