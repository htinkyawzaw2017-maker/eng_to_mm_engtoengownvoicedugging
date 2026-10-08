terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "media" {
  bucket = var.media_bucket_name
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    id     = "expire-temporary-media"
    status = "Enabled"
    filter { prefix = "uploads/" }
    expiration { days = var.upload_expiration_days }
  }
  rule {
    id     = "expire-job-output"
    status = "Enabled"
    filter { prefix = "jobs/" }
    expiration { days = var.output_expiration_days }
  }
}

resource "aws_sqs_queue" "dead_letter" {
  name                      = "${var.project_name}-dead-letter"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.project_name}-jobs"
  visibility_timeout_seconds = var.worker_visibility_timeout_seconds
  receive_wait_time_seconds  = 20
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter.arn
    maxReceiveCount     = 3
  })
}

output "media_bucket" { value = aws_s3_bucket.media.bucket }
output "jobs_queue_url" { value = aws_sqs_queue.jobs.url }
output "jobs_queue_arn" { value = aws_sqs_queue.jobs.arn }
