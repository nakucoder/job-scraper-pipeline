import json
import os
import re
import time
import boto3
import requests
from datetime import datetime
from groq import Groq
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
        r = requests.get(
            'https://data.usajobs.gov/api/search',
            headers=headers,
            params={'Keyword': keyword, 'ResultsPerPage': 3}
        )
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

def fetch_remotive_swe():
    # Indeed RSS permanently blocked by Cloudflare (HTTP 403) — replaced with
    # a second Remotive query targeting software engineering roles.
    r = requests.get('https://remotive.com/api/remote-jobs?category=software-dev&limit=5')
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

def fetch_themuse():
    # Free public API — no key required.
    # Note: The Muse's public API has sparse data. "Entry Level" filter returns
    # 0 results; querying without a level filter and using the categories that
    # actually have listings ("Software Engineer", "Data Science").
    # Contents field is HTML — strip tags before storing.
    jobs = []
    for category in ('Software Engineer', 'Data Science'):
        r = requests.get(
            'https://www.themuse.com/api/public/jobs',
            params={'category': category, 'page': 0}
        )
        if r.status_code != 200:
            continue
        for item in r.json().get('results', []):
            locations = item.get('locations', [])
            location = locations[0]['name'] if locations else 'Not specified'
            html = item.get('contents', '')
            description = re.sub(r'<[^>]+>', ' ', html).strip()[:500]
            jobs.append({
                'id': f"themuse-{item['id']}",
                'title': item['name'],
                'company': item.get('company', {}).get('name', 'Unknown'),
                'location': location,
                'url': item.get('refs', {}).get('landing_page', ''),
                'description': description,
                'source': 'TheMuse'
            })
    return jobs

# ── Gemini Enrichment ────────────────────────────────────────────────────────
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
    return json.loads(response.choices[0].message.content)

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
    all_jobs.extend(fetch_themuse())

    print(f"Fetched {len(all_jobs)} total jobs from all sources")

    # Process each job — no Gemini enrichment for now
    # Process each job with Gemini enrichment
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
            time.sleep(4)  # 15 req/min free tier = 1 req per 4 seconds
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
