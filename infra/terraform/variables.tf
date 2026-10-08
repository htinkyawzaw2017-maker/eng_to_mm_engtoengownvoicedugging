variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project_name" {
  type    = string
  default = "recap"
}

variable "media_bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name."
}

variable "web_origin" {
  type        = string
  default     = ""
  description = "Origin of the upload page (https://example.com). Empty disables the bucket CORS rule."
}

variable "upload_expiration_days" {
  type    = number
  default = 3
}

variable "output_expiration_days" {
  type    = number
  default = 14
}

variable "worker_visibility_timeout_seconds" {
  type    = number
  default = 3600
}

variable "max_receive_count" {
  type        = number
  default     = 3
  description = "After this many failed receives SQS moves the job to the dead-letter queue."
}

variable "iam_trusted_services" {
  type        = list(string)
  default     = ["ecs-tasks.amazonaws.com", "ec2.amazonaws.com"]
  description = "Who may assume the API and worker roles. No IAM user or access key is created."
}

variable "log_retention_days" {
  type    = number
  default = 14
}
