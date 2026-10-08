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

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "media" {
  bucket = var.media_bucket_name
}

# Private by default: no ACL and no policy can ever make this bucket public.
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
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Source videos are only needed while a job runs; results are kept for a while
# and then expire so the bucket cannot grow without limit.
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    id     = "expire-uploads"
    status = "Enabled"
    filter {
      prefix = "uploads/"
    }
    expiration {
      days = var.upload_expiration_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "expire-job-output"
    status = "Enabled"
    filter {
      prefix = "jobs/"
    }
    expiration {
      days = var.output_expiration_days
    }
  }
}

# The browser uploads straight to S3 with the presigned POST policy, so the
# bucket must answer the CORS preflight from the web origin. Without this the
# upload step fails in the browser even though the API is healthy.
resource "aws_s3_bucket_cors_configuration" "media" {
  count  = var.web_origin == "" ? 0 : 1
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_headers = ["Content-Type", "Content-Length"]
    allowed_methods = ["POST", "PUT", "GET"]
    allowed_origins = [var.web_origin]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------

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
    maxReceiveCount     = var.max_receive_count
  })
}

# ---------------------------------------------------------------------------
# IAM - roles only, never long-lived access keys
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  dynamic "statement" {
    for_each = var.iam_trusted_services
    content {
      actions = ["sts:AssumeRole"]
      principals {
        type        = "Service"
        identifiers = [statement.value]
      }
    }
  }
}

data "aws_iam_policy_document" "api" {
  # Issue presigned uploads for new source files.
  statement {
    sid       = "WriteUploads"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.media.arn}/uploads/*"]
  }

  # Find the uploaded file before enqueueing, and read job status / sign the
  # finished video for download.
  statement {
    sid       = "ReadStatusAndOutput"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.media.arn}/jobs/*"]
  }

  statement {
    sid       = "ListUploads"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["uploads/*"]
    }
  }

  # Only enqueue; the API never reads or deletes jobs.
  statement {
    sid       = "EnqueueJob"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "DownloadSource"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.media.arn}/uploads/*"]
  }

  statement {
    sid       = "WriteJobOutput"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.media.arn}/jobs/*"]
  }

  statement {
    sid = "ConsumeJobs"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
}

resource "aws_iam_role" "api" {
  name               = "${var.project_name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "api" {
  name   = "${var.project_name}-api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role" "worker" {
  name               = "${var.project_name}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "worker" {
  name   = "${var.project_name}-worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# Free, and only useful when the worker runs on EC2 instead of ECS.
resource "aws_iam_instance_profile" "worker" {
  count = contains(var.iam_trusted_services, "ec2.amazonaws.com") ? 1 : 0
  name  = "${var.project_name}-worker"
  role  = aws_iam_role.worker.name
}

# ---------------------------------------------------------------------------
# Logs (log group only - no alarm or dashboard, so nothing extra is billed)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/recap/${var.project_name}-worker"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "media_bucket" {
  value = aws_s3_bucket.media.bucket
}

output "jobs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "jobs_queue_arn" {
  value = aws_sqs_queue.jobs.arn
}

output "dead_letter_queue_url" {
  value       = aws_sqs_queue.dead_letter.url
  description = "Check this queue when a job keeps failing; a message here was retried max_receive_count times."
}

output "api_role_arn" {
  value       = aws_iam_role.api.arn
  description = "Attach to the API task/service (RECAP_MEDIA_BUCKET and RECAP_SQS_QUEUE_URL are still needed)."
}

output "worker_role_arn" {
  value       = aws_iam_role.worker.arn
  description = "Attach to the worker task/instance (GEMINI_API_KEY and RECAP_SQS_QUEUE_URL are still needed)."
}

output "worker_instance_profile" {
  value       = try(aws_iam_instance_profile.worker[0].name, "")
  description = "Empty unless ec2.amazonaws.com is in iam_trusted_services."
}
