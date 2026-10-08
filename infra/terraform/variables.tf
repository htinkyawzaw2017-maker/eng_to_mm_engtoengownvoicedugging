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
