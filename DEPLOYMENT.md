# AWS deployment

How to put the recap pipeline on AWS: S3 + SQS + one container that runs either
the API or the worker.

> **Cost first.** Nothing here is free-tier-only. A running worker on EC2 costs
> money every hour it is up, and a video job uses CPU, S3 storage and data
> transfer. Do [step 9 (budget alert)](#9-budget-alert-do-this-before-the-first-job)
> *before* the first job, and stop the worker when you are not testing.
> Start with **one CPU worker** and short videos. A GPU worker is only worth it
> once Whisper is measurably the bottleneck — see
> [scaling to a GPU worker](#11-scaling-to-a-gpu-worker-later).

---

## 0. Prerequisites

| | |
|---|---|
| AWS CLI v2 | configured with `aws configure` (or SSO). **Never** put keys in this repo |
| Terraform | >= 1.6 |
| Docker | for the image build |
| A Gemini API key | server side only, for the worker |

```bash
aws sts get-caller-identity          # confirm the account you are about to bill
```

## 1. Terraform init

```bash
cd infra/terraform
terraform init
```

Use a remote state backend (S3 + DynamoDB lock) for anything shared; the local
`terraform.tfstate` contains ARNs and should never be committed (it is in
`.gitignore`).

## 2. Terraform plan

```bash
terraform plan \
  -var 'media_bucket_name=recap-media-<something-globally-unique>' \
  -var 'web_origin=https://your-upload-page.example.com' \
  -out tfplan
```

`web_origin` creates the S3 CORS rule the browser upload needs. Leave it empty
only if you upload from the same origin through a proxy.

Read the plan: it should create **one S3 bucket, two SQS queues, two IAM roles
and one log group** — no EC2 instance, no IAM user, no access key.

## 3. Terraform apply

```bash
terraform apply tfplan
```

## 4. S3 bucket output

```bash
terraform output media_bucket
# recap-media-your-unique-name
```

The bucket is private (all four public-access-block flags), encrypted with
SSE-S3, expires `uploads/` after 3 days and `jobs/` after 14 days, and answers
CORS only for `web_origin`.

## 5. SQS queue URL output

```bash
terraform output jobs_queue_url
# https://sqs.ap-southeast-1.amazonaws.com/<account>/recap-jobs

terraform output dead_letter_queue_url
# https://sqs.ap-southeast-1.amazonaws.com/<account>/recap-dead-letter
```

`recap-jobs` redrives to `recap-dead-letter` after **3** receives
(`var.max_receive_count`). Check the dead-letter queue when a job disappears:

```bash
aws sqs get-queue-attributes \
  --queue-url "$(terraform output -raw dead_letter_queue_url)" \
  --attribute-names ApproximateNumberOfMessages
```

## 6. IAM role output

```bash
terraform output api_role_arn        # arn:aws:iam::<account>:role/recap-api
terraform output worker_role_arn     # arn:aws:iam::<account>:role/recap-worker
terraform output worker_instance_profile
```

Both roles are scoped to this bucket and this queue only. The API role can
write `uploads/*`, read `jobs/*`, list `uploads/*` and send to the queue — it
**cannot** receive or delete jobs. The worker role can read `uploads/*`, write
`jobs/*` and consume the queue. No `aws_iam_user` and no `aws_iam_access_key`
exist anywhere in this configuration; `tests/test_terraform.py` fails the build
if one is ever added.

## 7. API environment variables

| Variable | Example | Notes |
|---|---|---|
| `AWS_REGION` | `ap-southeast-1` | required; the API refuses to start without it |
| `RECAP_MEDIA_BUCKET` | `recap-media-...` | from `terraform output media_bucket` |
| `RECAP_SQS_QUEUE_URL` | `https://sqs.../recap-jobs` | from `terraform output jobs_queue_url` |
| `RECAP_ALLOWED_ORIGINS` | `https://your-page.example.com` | comma separated; default `*` |
| `RECAP_MAX_UPLOAD_BYTES` | `1073741824` | also enforced by S3 through the POST policy |
| `RECAP_UPLOAD_TTL_SECONDS` | `900` | presigned upload validity |
| `RECAP_DOWNLOAD_TTL_SECONDS` | `3600` | presigned download validity |

Credentials come from the task/instance role. No `AWS_ACCESS_KEY_ID` anywhere.

Run:

```bash
uvicorn cloud.api:app --host 0.0.0.0 --port 8000
```

## 8. Worker environment variables

| Variable | Example | Notes |
|---|---|---|
| `AWS_REGION` | `ap-southeast-1` | required |
| `RECAP_SQS_QUEUE_URL` | `https://sqs.../recap-jobs` | required |
| `GEMINI_API_KEY` | *(from Secrets Manager)* | server side only |
| `RECAP_VISIBILITY_TIMEOUT` | `3600` | extended by a heartbeat while a job runs |
| `PYTHON` | `python` | interpreter used for the planner/render subprocesses |
| `LOG_LEVEL` | `INFO` | |
| `HF_HOME` | `/var/tmp/hf` | where faster-whisper caches its weights |

The key must **not** be baked into the image or the queue message. On ECS put
it in Secrets Manager and reference it from the task definition; on EC2 put it
in a file readable only by the worker user.

Run:

```bash
python cloud/aws_worker.py
```

## 9. Budget alert — do this before the first job

```bash
aws budgets create-budget --account-id <account-id> --budget '{
  "BudgetName": "recap-monthly",
  "BudgetLimit": {"Amount": "20", "Unit": "USD"},
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST",
  "NotificationsWithSubscribers": [{
    "Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
                     "Threshold": 80, "ThresholdType": "PERCENTAGE"},
    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "you@example.com"}]
  }]
}'
```

Also worth doing: an S3 Storage Lens or CloudWatch `BucketSizeBytes` alarm, and
turning the worker instance/service off between test runs.

## 10. Docker build, ECR push, run

```bash
# build
docker build -t recap:latest .

# push
aws ecr create-repository --repository-name recap --image-scanning-configuration scanOnPush=true
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker tag recap:latest <account>.dkr.ecr.<region>.amazonaws.com/recap:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/recap:latest
```

The same image runs both commands:

```bash
# API
docker run --rm -p 8000:8000 \
  -e AWS_REGION=ap-southeast-1 \
  -e RECAP_MEDIA_BUCKET=... -e RECAP_SQS_QUEUE_URL=... \
  recap:latest

# worker
docker run --rm \
  -e AWS_REGION=ap-southeast-1 \
  -e RECAP_SQS_QUEUE_URL=... -e GEMINI_API_KEY=... \
  recap:latest python cloud/aws_worker.py
```

The image runs as the non-root user `recap`, installs `ffmpeg` and `libgomp1`,
and downloads no model at build time — faster-whisper fetches its weights on
the first job, so the first run is slower and needs outbound HTTPS to
Hugging Face (or pre-seed `HF_HOME`).

### ECS (Fargate) — recommended

Two services from one image:

* **api** — command `uvicorn cloud.api:app --host 0.0.0.0 --port 8000`, task
  role `recap-api`, behind an ALB with a TLS listener.
* **worker** — command `python cloud/aws_worker.py`, task role `recap-worker`,
  `desiredCount = 1` to start. Scale on
  `ApproximateNumberOfMessagesVisible` of `recap-jobs`.

Fargate needs at least 2 vCPU / 4 GB for CPU Whisper; 4 vCPU / 8 GB is more
comfortable. `secrets` from Secrets Manager for `GEMINI_API_KEY`, `HF_HOME` on
an ephemeral volume.

### EC2 (cheapest way to test)

```bash
aws ec2 run-instances --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --instance-type m7i.large --iam-instance-profile Name=recap-worker \
  --user-data file://cloud/start-worker.sh --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=recap-worker}]'
```

`m7i.large` on demand is roughly a few cents per hour in ap-southeast-1 — check
the current price and **terminate it when done**:

```bash
aws ec2 terminate-instances --instance-ids <id>
```

Install Docker on the instance (or use ECS Anywhere) before
`cloud/start-worker.sh` will do anything useful; the script assumes `python` and
the dependencies are present.

## 11. HTTPS setup

The API speaks plain HTTP. Put TLS in front of it — never expose port 8000:

* **ALB + ACM** — request a certificate in ACM for your domain, add an HTTPS:443
  listener on the ALB that targets the API target group, and redirect HTTP to
  HTTPS.
* **API Gateway** — a REST or HTTP API with the API as an HTTP integration;
  gives you a default `*.execute-api` HTTPS name and request throttling.

Then set `RECAP_ALLOWED_ORIGINS` and the Terraform `web_origin` to the real
https origin of `web/index.html`, and serve that page from S3 + CloudFront or
any static host.

Add authentication before this is reachable by strangers: Cognito/ALB OIDC in
front of the API, or API Gateway with an authorizer. Without it, anyone can
enqueue jobs and spend your money.

## 12. CloudWatch logs

The Terraform creates the log group `/recap/recap-worker` with a 14-day
retention. On ECS, point the task's `awslogs` driver at it:

```json
"logConfiguration": {
  "logDriver": "awslogs",
  "options": {
    "awslogs-group": "/recap/recap-worker",
    "awslogs-region": "ap-southeast-1",
    "awslogs-stream-prefix": "worker"
  }
}
```

Create a second group `/recap/recap-api` the same way for the API service.
Each job also uploads its own `jobs/<job_id>/run.log` to S3, which is the first
place to look when a job fails — `status.json` carries the stage and the last
lines of that log.

Useful queries in Logs Insights:

```
fields @timestamp, @message
| filter @message like /job .* failed/
| sort @timestamp desc
```

## 13. Scaling to a GPU worker (later)

Do not create this until CPU Whisper is actually the bottleneck. When it is,
add a **separate** queue and a separate worker deployment rather than changing
this one:

1. a `recap-jobs-gpu` SQS queue with its own dead-letter queue,
2. a GPU instance type (`g5.xlarge` and up) or an ECS GPU task with
   `resourceRequirements: [{type: GPU, value: 1}]`,
3. the CUDA libraries in a *separate* image (they are large and the CPU worker
   does not need them),
4. the API routes a job to the GPU queue only when the job asks for it.

A GPU instance is roughly an order of magnitude more expensive per hour than a
CPU one. `g5.xlarge` on demand is around a dollar an hour in most regions —
check the current price, use a Spot or on-demand capacity that you terminate,
and keep the CPU worker as the default.

---

## Verification checklist

| Check | Command |
|---|---|
| Unit tests (61) | `python -m unittest discover -s tests` |
| Infrastructure rules | included above (`tests/test_terraform.py`) |
| Formatting of the HCL | `terraform fmt -check -recursive infra/terraform` |
| Infrastructure validity | `terraform validate` (after `terraform init`) |
| Image builds | `docker build -t recap:latest .` |
| Local end-to-end (18 steps, no AWS) | `python scripts/e2e_local.py` |
| API health | `curl https://<api>/health` |
| Real job | upload a **1 minute** clip in `web/index.html` first |

Start with a one-to-five-minute video: it exercises Whisper, Gemini, Edge TTS
and the render for a few cents instead of a few dollars.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Browser upload fails, API is healthy | S3 CORS: set `web_origin` and re-apply Terraform |
| `503 the service is still being configured` | `RECAP_MEDIA_BUCKET` / `RECAP_SQS_QUEUE_URL` missing |
| `RuntimeError: AWS_REGION ... must be set` | set `AWS_REGION` on the task |
| Job stays `queued` forever | no worker running, or the worker cannot reach the queue |
| Job `failed` at `planner` | usually the Gemini key; read `jobs/<id>/run.log` |
| Job `failed` at `render` | usually FFmpeg; check the image and `run.log` |
| Job appears in the dead-letter queue | it failed 3 times; read `status.json` and `run.log`, fix, re-enqueue |
| `AccessDenied` from the worker | the worker is not running with the `recap-worker` role |
