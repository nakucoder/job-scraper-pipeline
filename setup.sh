#!/bin/bash
# setup.sh — GCP manual infrastructure setup
#
# WHY THIS FILE EXISTS:
# The GCP Cloud Scheduler job that triggers the daily scraper was created
# manually in the GCP Console and is NOT defined in template.yaml (which only
# covers AWS infrastructure via SAM) or anywhere else in this repo.
# This script is the single source of truth for reproducing that manual step.
#
# Run this once after deploying the Cloud Function (see README Deployment section).

set -euo pipefail

PROJECT=job-scraper-pipeline-496721
SCHEDULER_JOB=job-scraper-daily
REGION=us-central1

echo "==> Checking if Cloud Scheduler job '${SCHEDULER_JOB}' already exists..."

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
     --project="${PROJECT}" \
     --location="${REGION}" \
     &>/dev/null; then
  echo "    Job already exists — skipping creation."
else
  echo "==> Creating Cloud Scheduler job..."
  gcloud scheduler jobs create http "${SCHEDULER_JOB}" \
    --schedule="0 11 * * *" \
    --time-zone="America/New_York" \
    --uri="https://${REGION}-${PROJECT}.cloudfunctions.net/job-scraper" \
    --http-method=POST \
    --project="${PROJECT}" \
    --location="${REGION}"
  echo "    Job created."
fi

echo ""
echo "==> Verifying — current Cloud Scheduler jobs in project ${PROJECT}:"
gcloud scheduler jobs list --project="${PROJECT}" --location="${REGION}"
