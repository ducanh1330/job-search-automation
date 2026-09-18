# Deploying the pipeline to AWS

Runs the scrape → enrich → export pipeline on a schedule in AWS Lambda, writing
the workbook to S3. Infrastructure is Terraform; nothing is created by hand.

```
EventBridge (daily cron) ─► Lambda (Python 3.12) ─► S3
                                   │                 ├─ reports/seek-jobs-<date>.xlsx
                                   │                 ├─ reports/seek-jobs-latest.xlsx
                                   └─ Firecrawl API  └─ state/seen_jobs.json
```

## Why state lives in S3

A Lambda container is disposable, so `seen_jobs.json` cannot stay on its
filesystem: every run would start empty and report every listing as **New**,
which is exactly the signal the workbook exists to give. The handler pulls the
state file from S3 before the run and writes it back after.

## Cost

Designed to sit inside the AWS Free Tier: one run a day at 512 MB for a few
minutes is a small fraction of the 400,000 GB-second monthly grant, and the
workbook is a few hundred KB against 5 GB of S3. The realistic bill is $0, but
**set a billing alarm anyway** — free tier is a grant, not a spending cap.

CloudWatch log retention is capped at 14 days and dated reports expire after 90,
so neither grows unbounded.

## Deploy

```bash
# 1. Build the deployment package (handler + tools + pure-Python deps)
./infra/build.sh

# 2. Deploy
cd infra
terraform init
terraform apply -var="firecrawl_api_key=fc-..."
```

Pass the Firecrawl key at apply time, or export it as `TF_VAR_firecrawl_api_key`.
Never commit it: `terraform.tfvars` and `*.tfstate` are gitignored, and state
holds variable values in plain text.

## Run it now, without waiting for the schedule

```bash
aws lambda invoke --function-name job-search-automation /dev/stdout
aws s3 cp s3://$(terraform output -raw bucket)/reports/seek-jobs-latest.xlsx .
```

## Variables

| Variable | Default | Notes |
|---|---|---|
| `region` | `ap-southeast-2` | Sydney |
| `seek_search_url` | Melbourne IT internships | Any Seek search URL |
| `schedule_expression` | `cron(0 22 * * ? *)` | 08:00 Melbourne; UTC, so it shifts an hour with daylight saving |
| `firecrawl_api_key` | — | Required, sensitive |

## Teardown

```bash
terraform destroy
```

Empty the bucket first if it still holds objects.
