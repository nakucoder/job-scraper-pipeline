import json
import os
import boto3
from datetime import datetime, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
S3_BUCKET = os.environ.get('S3_BUCKET', 'job-scraper-pipeline-juan')

def lambda_handler(event, context):
    # ── CORS headers ─────────────────────────────────────────────────────────
    headers = {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'GET,OPTIONS'
    }

    # Handle preflight
    if event.get('httpMethod') == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': ''}

    # ── Query params ──────────────────────────────────────────────────────────
    params = event.get('queryStringParameters') or {}
    days = int(params.get('days', 1))
    min_score = int(params.get('min_score', 0))
    remote_only = params.get('remote_only', 'false').lower() == 'true'
    source_filter = params.get('source', None)

    # ── Fetch jobs from S3 ────────────────────────────────────────────────────
    s3 = boto3.client('s3', region_name='us-east-1')
    all_jobs = []

    for i in range(days):
        date = (datetime.utcnow() - timedelta(days=i)).strftime('%Y/%m/%d')
        key = f"jobs/{date}/enriched_jobs.json"
        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            jobs = json.loads(obj['Body'].read())
            all_jobs.extend(jobs)
        except Exception:
            continue

    # ── Apply filters ─────────────────────────────────────────────────────────
    if min_score > 0:
        all_jobs = [j for j in all_jobs if j.get('match_score_percent', 0) >= min_score]

    if remote_only:
        all_jobs = [j for j in all_jobs if j.get('is_remote', False)]

    if source_filter:
        all_jobs = [j for j in all_jobs if j.get('source', '').lower() == source_filter.lower()]

    # ── Sort by match score ───────────────────────────────────────────────────
    all_jobs = sorted(all_jobs, key=lambda x: x.get('match_score_percent', 0), reverse=True)

    # ── Response ──────────────────────────────────────────────────────────────
    return {
        'statusCode': 200,
        'headers': headers,
        'body': json.dumps({
            'total': len(all_jobs),
            'days_queried': days,
            'filters': {
                'min_score': min_score,
                'remote_only': remote_only,
                'source': source_filter
            },
            'jobs': all_jobs
        })
    }
