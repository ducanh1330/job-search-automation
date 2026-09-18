"""AWS Lambda entry point: run the Seek pipeline on a schedule.

Reuses the same three stages as the CLI - scrape, enrich, export - and writes
the workbook plus the tracking state to S3. State lives in S3 rather than the
function's filesystem, because a Lambda container is disposable: without it
every run would report every job as "New".

Environment:
    SEEK_SEARCH_URL   Seek search URL to scrape (narrow keyword search).
    OUTPUT_BUCKET     S3 bucket for the workbook and state file.
    FIRECRAWL_API_KEY Firecrawl key (injected from SSM by Terraform).
    JOBSEARCH_TMP_DIR Scratch dir; set to /tmp, the only writable path here.
"""

import argparse
import datetime as dt
import json
import os
import sys

TMP = os.environ.setdefault("JOBSEARCH_TMP_DIR", "/tmp/jobsearch")
os.makedirs(TMP, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))

import boto3  # noqa: E402

import enrich_job_details  # noqa: E402
import export_jobs_excel  # noqa: E402
import scrape_seek_jobs  # noqa: E402

BUCKET = os.environ["OUTPUT_BUCKET"]
SEARCH_URL = os.environ.get(
    "SEEK_SEARCH_URL",
    "https://au.seek.com/it-internship-jobs/in-Melbourne-VIC-3000",
)
STATE_KEY = "state/seen_jobs.json"
s3 = boto3.client("s3")


def _download_state(path: str) -> None:
    """Pull the previous run's state file, if there is one."""
    try:
        s3.download_file(BUCKET, STATE_KEY, path)
    except Exception:  # first run, or the object was deleted
        pass


def handler(event, context):
    raw = os.path.join(TMP, "seek_raw.json")
    enriched = os.path.join(TMP, "seek_jobs.json")
    state = os.path.join(TMP, "seen_jobs.json")
    workbook = os.path.join(TMP, "seek_jobs.xlsx")
    _download_state(state)

    scrape_seek_jobs.run(argparse.Namespace(
        classification="6281", where="All Melbourne VIC", url=SEARCH_URL,
        html=None, out=raw, firecrawl=True, delay=None,
        max_partitions=None, no_cache=False,
    ))
    enrich_job_details.run(argparse.Namespace(
        input=[raw], out=enriched, all=False, limit=None, delay=1.0,
    ))
    result = export_jobs_excel.run(argparse.Namespace(
        input=[enriched], out=workbook, state=state,
    ))

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    s3.upload_file(workbook, BUCKET, f"reports/seek-jobs-{stamp}.xlsx")
    s3.upload_file(workbook, BUCKET, "reports/seek-jobs-latest.xlsx")
    s3.upload_file(state, BUCKET, STATE_KEY)
    return {"statusCode": 200, "body": json.dumps({"stamp": stamp, "result": str(result)[:500]})}
