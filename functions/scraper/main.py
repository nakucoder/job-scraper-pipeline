import json
import os
import re
import time
import traceback
import boto3
import requests
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ValidationError
from typing import List
from groq import Groq
from azure.cosmos import CosmosClient

# ── Config ──────────────────────────────────────────────────────────────────
SSM = boto3.client('ssm', region_name='us-east-1')
TODAY = datetime.utcnow().strftime('%Y/%m/%d')
S3_BUCKET = os.environ.get('S3_BUCKET', 'job-scraper-pipeline-juan')
PROFILE_KEY = 'config/candidate_profile.json'

class JobEnrichment(BaseModel):
    match_score_percent: int = Field(ge=0, le=100)
    is_remote: bool
    estimated_salary_min: int = 0
    estimated_salary_max: int = 0
    primary_cloud: str = "Unknown"
    constraint_violations: List[str] = []
    upskill_recommendations: List[str] = []

def get_ssm(name):
    return SSM.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

# ── Job Sources ──────────────────────────────────────────────────────────────
def fetch_usajobs(profile):
    api_key = get_ssm('/job-pipeline/usajobs-api-key')
    headers = {
        'Authorization-Key': api_key,
        'User-Agent': 'juanakuspinelli@gmail.com'
    }
    # Federal job titles use GS grade levels, not "Junior"/"Associate" prefixes.
    # Querying with those prefixes returns 0 results. Strip them and deduplicate.
    prefixes = ('Junior ', 'Associate ', 'SRE / ')
    seen_keywords, keywords = set(), []
    for role in profile['meta']['target_roles']:
        normalized = role
        for p in prefixes:
            normalized = normalized.replace(p, '')
        if normalized not in seen_keywords:
            seen_keywords.add(normalized)
            keywords.append(normalized)

    jobs = []
    for keyword in keywords[:4]:
        # No LocationName filter — federal postings are nationwide/remote and
        # Miami rarely has matching listings, which caused 0 results since May 19.
        try:
            r = requests.get(
                'https://data.usajobs.gov/api/search',
                headers=headers,
                params={'Keyword': keyword, 'ResultsPerPage': 3},
                timeout=10
            )
        except requests.exceptions.Timeout:
            print(f"USAJobs timed out for keyword '{keyword}', skipping")
            continue
        except requests.exceptions.RequestException as e:
            print(f"USAJobs request error for keyword '{keyword}': {e}")
            continue
        if r.status_code == 200:
            for item in r.json().get('SearchResult', {}).get('SearchResultItems', []):
                d = item['MatchedObjectDescriptor']
                jobs.append({
                    'id': d['PositionID'],
                    'title': d['PositionTitle'],
                    'company': d['OrganizationName'],
                    'location': d['PositionLocationDisplay'],
                    'url': d['PositionURI'],
                    'description': d.get('QualificationSummary', '')[:500],
                    'source': 'USAJobs'
                })
    return jobs

def fetch_remotive():
    try:
        r = requests.get(
            'https://remotive.com/api/remote-jobs?category=data&limit=5',
            timeout=10
        )
    except requests.exceptions.Timeout:
        print("Remotive (data) timed out, skipping")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Remotive (data) request error: {e}")
        return []
    jobs = []
    if r.status_code == 200:
        for item in r.json().get('jobs', []):
            jobs.append({
                'id': f"remotive-{item['id']}",
                'title': item['title'],
                'company': item['company_name'],
                'location': item.get('candidate_required_location', 'Remote'),
                'url': item['url'],
                'description': item.get('description', '')[:500],
                'source': 'Remotive'
            })
    return jobs

def fetch_remotive_swe():
    # Indeed RSS permanently blocked by Cloudflare (HTTP 403) — replaced with
    # a second Remotive query targeting software engineering roles.
    try:
        r = requests.get(
            'https://remotive.com/api/remote-jobs?category=software-dev&limit=5',
            timeout=10
        )
    except requests.exceptions.Timeout:
        print("Remotive (software-dev) timed out, skipping")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Remotive (software-dev) request error: {e}")
        return []
    jobs = []
    if r.status_code == 200:
        for item in r.json().get('jobs', []):
            jobs.append({
                'id': f"remotive-swe-{item['id']}",
                'title': item['title'],
                'company': item['company_name'],
                'location': item.get('candidate_required_location', 'Remote'),
                'url': item['url'],
                'description': item.get('description', '')[:500],
                'source': 'Remotive'
            })
    return jobs

def fetch_jobicy():
    # Free public API — no key required.
    # Tags must use spaces, not hyphens ('data engineer', not 'data-engineer').
    # jobDescription field is HTML — strip tags before storing.
    jobs = []
    for tag in ('data engineer', 'software engineer'):
        try:
            r = requests.get(
                'https://jobicy.com/api/v2/remote-jobs',
                params={'count': 20, 'tag': tag},
                timeout=10
            )
        except requests.exceptions.Timeout:
            print(f"Jobicy timed out for tag '{tag}', skipping")
            continue
        except requests.exceptions.RequestException as e:
            print(f"Jobicy request error for tag '{tag}': {e}")
            continue
        if r.status_code != 200 or not r.json().get('success'):
            continue
        for item in r.json().get('jobs', []):
            html = item.get('jobDescription', '')
            description = re.sub(r'<[^>]+>', ' ', html).strip()[:500]
            jobs.append({
                'id': f"jobicy-{item['id']}",
                'title': item['jobTitle'],
                'company': item['companyName'],
                'location': item.get('jobGeo', 'Remote'),
                'url': item['url'],
                'description': description,
                'source': 'Jobicy'
            })
    return jobs

# ── Groq Enrichment ──────────────────────────────────────────────────────────
def enrich_with_gemini(job, profile, gemini_client):
    prompt = f"""You are a job matching engine. Analyze this job posting against the candidate profile and return ONLY a JSON object with no extra text.

Candidate Profile: {json.dumps(profile)}
Job Posting: {json.dumps(job)}

Return this exact JSON structure:
{{
    "match_score_percent": <integer 0-100>,
    "is_remote": <true or false>,
    "estimated_salary_min": <integer or 0 if unknown>,
    "estimated_salary_max": <integer or 0 if unknown>,
    "primary_cloud": "<AWS, Azure, GCP, Hybrid, or Unknown>",
    "constraint_violations": [<list of strings describing mismatches>],
    "upskill_recommendations": [<list of skills in job not in candidate profile>]
}}"""

    response = gemini_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.1
    )
    raw = json.loads(response.choices[0].message.content)
    validated = JobEnrichment(**raw)
    return validated.model_dump()

# ── Dedup via Cosmos DB ──────────────────────────────────────────────────────
def is_new_job(job_id, cosmos_container):
    try:
        cosmos_container.read_item(item=job_id, partition_key=job_id)
        return False
    except:
        return True

def mark_job_seen(job_id, cosmos_container):
    cosmos_container.upsert_item({'id': job_id, 'job_id': job_id, 'seen_at': TODAY})

# ── Main Handler ─────────────────────────────────────────────────────────────
def scraper(request):
    EXECUTED_AT = datetime.utcnow().strftime('%H%M%S')
    started_at = datetime.now(timezone.utc).isoformat()

    s3 = boto3.client('s3', region_name='us-east-1')
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE', 'job-pipeline-metadata'))

    table.put_item(Item={
        'run_id': f"scraper-{TODAY}",
        'status': 'STARTED',
        'started_at': started_at,
        'run_date': TODAY
    })

    jobs_fetched = 0
    jobs_skipped = 0
    jobs_new = 0
    groq_errors = 0

    try:
        # Load candidate profile from S3
        obj = s3.get_object(Bucket=S3_BUCKET, Key=PROFILE_KEY)
        profile = json.loads(obj['Body'].read())

        # Init Groq
        groq_api_key = get_ssm('/job-pipeline/groq-api-key')
        gemini_client = Groq(api_key=groq_api_key)

        # Init Cosmos DB
        cosmos_conn = get_ssm('/job-pipeline/cosmos-connection-string')
        cosmos_client = CosmosClient.from_connection_string(cosmos_conn)
        container = cosmos_client.get_database_client('job-scraper-db').get_container_client('seen-jobs')

        # Fetch all jobs
        all_jobs = []
        all_jobs.extend(fetch_usajobs(profile))
        all_jobs.extend(fetch_remotive())
        all_jobs.extend(fetch_remotive_swe())
        all_jobs.extend(fetch_jobicy())
        jobs_fetched = len(all_jobs)

        print(f"Fetched {jobs_fetched} total jobs from all sources")

        # Same-run dedup — catches duplicate IDs across sources within one execution
        seen_this_run = set()

        # Process each job with Groq enrichment
        new_jobs = []
        for job in all_jobs:
            job_id = str(job['id'])

            if job_id in seen_this_run:
                print(f"Duplicate in this run: {job_id}, skipping")
                jobs_skipped += 1
                continue

            if not is_new_job(job_id, container):
                print(f"Already seen: {job_id}, skipping")
                jobs_skipped += 1
                continue

            seen_this_run.add(job_id)

            try:
                enrichment = enrich_with_gemini(job, profile, gemini_client)
                job.update(enrichment)
                job['processed_date'] = TODAY
                new_jobs.append(job)
                mark_job_seen(job_id, container)
                print(f"Processed: {job['title']} — score: {job.get('match_score_percent')}%")
                time.sleep(4)  # 15 req/min free tier = 1 req per 4 seconds
            except ValidationError as e:
                print(f"Groq output validation failed for {job_id}: {e}")
                groq_errors += 1
                continue  # Do NOT mark seen in Cosmos — job will retry tomorrow
            except Exception as e:
                print(f"Error enriching job {job_id}: {e}")
                if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                    print("Rate limit hit — stopping enrichment, saving processed jobs")
                    break
                continue

        jobs_new = len(new_jobs)
        print(f"Found {jobs_new} new jobs after dedup")

        # Save to S3
        if new_jobs:
            key = f"jobs/{TODAY}/enriched_jobs_{EXECUTED_AT}.json"
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=json.dumps(new_jobs, indent=2),
                ContentType='application/json'
            )
            print(f"Saved to s3://{S3_BUCKET}/{key}")

        finished_at = datetime.now(timezone.utc).isoformat()
        table.put_item(Item={
            'run_id': f"scraper-{TODAY}",
            'status': 'SUCCESS',
            'started_at': started_at,
            'finished_at': finished_at,
            'run_date': TODAY,
            'jobs_fetched': jobs_fetched,
            'jobs_skipped': jobs_skipped,
            'jobs_new': jobs_new,
            'groq_errors': groq_errors
        })

        return json.dumps({'status': 'success', 'new_jobs': jobs_new, 'jobs': new_jobs})

    except Exception as e:
        finished_at = datetime.now(timezone.utc).isoformat()
        tb = traceback.format_exc()[:2000]
        table.put_item(Item={
            'run_id': f"scraper-{TODAY}",
            'status': 'FAILED',
            'started_at': started_at,
            'finished_at': finished_at,
            'run_date': TODAY,
            'jobs_fetched': jobs_fetched,
            'jobs_skipped': jobs_skipped,
            'jobs_new': jobs_new,
            'groq_errors': groq_errors,
            'error': str(e),
            'traceback': tb
        })
        print(f"Pipeline failed: {e}\n{tb}")
        raise
