import json
import os
import time
import boto3
import requests
import feedparser
from datetime import datetime
from google import genai
from google.genai import types
from azure.cosmos import CosmosClient

# ── Config ──────────────────────────────────────────────────────────────────
SSM = boto3.client('ssm', region_name='us-east-1')
TODAY = datetime.utcnow().strftime('%Y/%m/%d')
S3_BUCKET = os.environ.get('S3_BUCKET', 'job-scraper-pipeline-juan')
PROFILE_KEY = 'config/candidate_profile.json'

def get_ssm(name):
    return SSM.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

# ── Job Sources (total ~17 jobs) ─────────────────────────────────────────────
def fetch_usajobs(profile):
    # 3 roles x 2 jobs = 6 jobs
    api_key = get_ssm('/job-pipeline/usajobs-api-key')
    headers = {
        'Authorization-Key': api_key,
        'User-Agent': 'juanakuspinelli@gmail.com'
    }
    jobs = []
    for role in profile['meta']['target_roles'][:3]:
        url = f'https://data.usajobs.gov/api/search?Keyword={role}&LocationName=Miami&ResultsPerPage=2'
        r = requests.get(url, headers=headers)
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
    # 5 jobs
    r = requests.get('https://remotive.com/api/remote-jobs?category=data&limit=5')
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

def fetch_rss_feeds():
    # 3 jobs each = 6 jobs
    feeds = [
        ('https://www.indeed.com/rss?q=junior+data+engineer&l=Miami%2C+FL', 'Indeed'),
    ]
    jobs = []
    for url, source in feeds:
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:
            jobs.append({
                'id': entry.get('id', entry.link),
                'title': entry.title,
                'company': entry.get('author', 'Unknown'),
                'location': 'Miami',
                'url': entry.link,
                'description': entry.get('summary', '')[:500],
                'source': source
            })
    return jobs

# ── Gemini Enrichment ────────────────────────────────────────────────────────
def enrich_with_gemini(job, profile, gemini_client):
    schema = {
        'type': 'OBJECT',
        'properties': {
            'match_score_percent': {'type': 'INTEGER'},
            'is_remote': {'type': 'BOOLEAN'},
            'estimated_salary_min': {'type': 'INTEGER'},
            'estimated_salary_max': {'type': 'INTEGER'},
            'primary_cloud': {'type': 'STRING'},
            'constraint_violations': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
            'upskill_recommendations': {'type': 'ARRAY', 'items': {'type': 'STRING'}}
        },
        'required': ['match_score_percent', 'is_remote', 'constraint_violations', 'upskill_recommendations']
    }
    response = gemini_client.models.generate_content(
        model='gemini-2.0-flash-lite',
        contents=f"Analyze this job: {json.dumps(job)}",
        config=types.GenerateContentConfig(
            system_instruction=f"""You are a job matching engine. Score this job against the candidate profile.
Profile: {json.dumps(profile)}
Return structured JSON only. For constraint_violations list any mismatches with hard_filters or soft_preferences.
For upskill_recommendations list skills in the job not in the candidate profile.""",
            response_mime_type='application/json',
            response_schema=schema
        )
    )
    return json.loads(response.text)

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
    s3 = boto3.client('s3', region_name='us-east-1')

    # Load candidate profile from S3
    obj = s3.get_object(Bucket=S3_BUCKET, Key=PROFILE_KEY)
    profile = json.loads(obj['Body'].read())

    # Init Gemini
    gemini_api_key = get_ssm('/job-pipeline/gemini-api-key')
    gemini_client = genai.Client(api_key=gemini_api_key)

    # Init Cosmos DB
    cosmos_conn = get_ssm('/job-pipeline/cosmos-connection-string')
    cosmos_client = CosmosClient.from_connection_string(cosmos_conn)
    container = cosmos_client.get_database_client('job-scraper-db').get_container_client('seen-jobs')

    # Fetch all jobs
    all_jobs = []
    all_jobs.extend(fetch_usajobs(profile))
    all_jobs.extend(fetch_remotive())
    all_jobs.extend(fetch_rss_feeds())

    print(f"Fetched {len(all_jobs)} total jobs from all sources")

    # Process each job
    new_jobs = []
    for job in all_jobs:
        job_id = str(job['id'])
        if not is_new_job(job_id, container):
            print(f"Already seen: {job_id}, skipping")
            continue
        try:
            enrichment = enrich_with_gemini(job, profile, gemini_client)
            job.update(enrichment)
            job['processed_date'] = TODAY
            new_jobs.append(job)
            mark_job_seen(job_id, container)
            print(f"Processed: {job['title']} — score: {job.get('match_score_percent')}%")
            time.sleep(4)  # prevent Gemini rate limit
        except Exception as e:
            print(f"Error enriching job {job_id}: {e}")
            if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                print("Gemini rate limit hit — stopping enrichment, saving processed jobs")
                break
            continue

    print(f"Found {len(new_jobs)} new jobs after dedup")

    # Save to S3
    if new_jobs:
        key = f"jobs/{TODAY}/enriched_jobs.json"
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(new_jobs, indent=2),
            ContentType='application/json'
        )
        print(f"Saved to s3://{S3_BUCKET}/{key}")

    return json.dumps({'status': 'success', 'new_jobs': len(new_jobs), 'jobs': new_jobs})
