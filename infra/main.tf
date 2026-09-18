# Seek job-search pipeline on AWS: a scheduled Lambda that scrapes, enriches and
# exports listings to S3. Sized to stay inside the AWS Free Tier - one short run
# per day is a few thousand GB-seconds a month against a 400,000 GB-second grant.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-southeast-2" # Sydney
}

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "job-search-automation"
}

variable "firecrawl_api_key" {
  description = "Firecrawl API key. Passed at apply time; never commit it."
  type        = string
  sensitive   = true
}

variable "seek_search_url" {
  description = "Seek search URL to scrape each run."
  type        = string
  default     = "https://au.seek.com/it-internship-jobs/in-Melbourne-VIC-3000"
}

variable "schedule_expression" {
  description = "When to run. Default: 08:00 Melbourne (22:00 UTC), daily."
  type        = string
  default     = "cron(0 22 * * ? *)"
}

resource "random_id" "suffix" {
  byte_length = 4
}

# ---------------------------------------------------------------- storage ----

resource "aws_s3_bucket" "reports" {
  bucket = "${var.project}-${random_id.suffix.hex}"
}

# The workbook and the state file are private: the state file holds the full
# job snapshot, and neither needs to be reachable from the internet.
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

# Dated reports pile up forever otherwise; the latest copy is a fixed key.
resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "expire-dated-reports"
    status = "Enabled"
    filter {
      prefix = "reports/seek-jobs-2"
    }
    expiration {
      days = 90
    }
  }
}

# ------------------------------------------------------------------- iam ----

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Least privilege: object access is scoped to this bucket only, and logging is
# the managed basic-execution policy rather than a wildcard of our own.
data "aws_iam_policy_document" "bucket_access" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.reports.arn}/*"]
  }
}

resource "aws_iam_role_policy" "bucket_access" {
  name   = "${var.project}-s3"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.bucket_access.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---------------------------------------------------------------- lambda ----

resource "aws_lambda_function" "pipeline" {
  function_name    = var.project
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_handler.handler"
  runtime          = "python3.12"
  filename         = "${path.module}/build/function.zip"
  source_code_hash = filebase64sha256("${path.module}/build/function.zip")
  timeout          = 600 # a full scrape paces itself between requests
  memory_size      = 512

  environment {
    variables = {
      OUTPUT_BUCKET     = aws_s3_bucket.reports.id
      SEEK_SEARCH_URL   = var.seek_search_url
      FIRECRAWL_API_KEY = var.firecrawl_api_key
      JOBSEARCH_TMP_DIR = "/tmp/jobsearch"
    }
  }
}

# Without an explicit group, Lambda creates one with no expiry - logs then grow
# unbounded and eventually cost money.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${aws_lambda_function.pipeline.function_name}"
  retention_in_days = 14
}

# -------------------------------------------------------------- schedule ----

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.project}-daily"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.daily.name
  arn  = aws_lambda_function.pipeline.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pipeline.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}

# --------------------------------------------------------------- outputs ----

output "bucket" {
  description = "Where the workbooks land."
  value       = aws_s3_bucket.reports.id
}

output "function_name" {
  value = aws_lambda_function.pipeline.function_name
}

output "download_latest" {
  description = "Fetch the most recent workbook."
  value       = "aws s3 cp s3://${aws_s3_bucket.reports.id}/reports/seek-jobs-latest.xlsx ."
}
