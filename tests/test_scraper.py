import json
import sys
import os
import boto3
import requests
from azure.cosmos import CosmosClient

# ── Setup ────────────────────────────────────────────────────────────────────
SSM = boto3.client('ssm', region_name='us-east-1')

def get_ssm(name):
    return SSM.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']

passed = 0
failed = 0

def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✅ PASS — {name}")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAIL — {name}: {e}")
        failed += 1

# ── Tests ────────────────────────────────────────────────────────────────────

def test_ssm_usajobs_key():
    key = get_ssm('/job-pipeline/usajobs-api-key')
    assert key and len(key) > 10, "USAJobs key is empty or too short"

def test_ssm_gemini_key():
    key = get_ssm('/job-pipeline/gemini-api-key')
    assert key and len(key) > 10, "Gemini key is empty or too short"

def test_ssm_cosmos_connection():
    conn = get_ssm('/job-pipeline/cosmos-connection-string')
    assert 'cosmos' in conn.lower(), "Cosmos connection string looks wrong"

def test_s3_bucket_exists():
    s3 = boto3.client('s3', region_name='us-east-1')
    response = s3.list_buckets()
    buckets = [b['Name'] for b in response['Buckets']]
    assert 'job-scraper-pipeline-juan' in buckets, "S3 bucket not found"

def test_s3_profile_exists():
    s3 = boto3.client('s3', region_name='us-east-1')
    obj = s3.get_object(Bucket='job-scraper-pipeline-juan', Key='config/candidate_profile.json')
    profile = json.loads(obj['Body'].read())
    assert 'meta' in profile, "Profile missing meta key"
    assert 'hard_filters' in profile, "Profile missing hard_filters key"
    assert 'soft_preferences' in profile, "Profile missing soft_preferences key"

def test_candidate_profile_structure():
    s3 = boto3.client('s3', region_name='us-east-1')
    obj = s3.get_object(Bucket='job-scraper-pipeline-juan', Key='config/candidate_profile.json')
    profile = json.loads(obj['Body'].read())
    assert len(profile['meta']['target_roles']) > 0, "No target roles defined"
    assert len(profile['hard_filters']['locations']) > 0, "No locations defined"
    assert profile['soft_preferences']['compensation']['minimum_soft_floor_usd'] > 0, "Salary floor is 0"

def test_remotive_api():
    r = requests.get('https://remotive.com/api/remote-jobs?category=data&limit=1', timeout=10)
    assert r.status_code == 200, f"Remotive returned {r.status_code}"
    data = r.json()
    assert 'jobs' in data, "Remotive response missing jobs key"
    assert len(data['jobs']) > 0, "Remotive returned 0 jobs"

def test_usajobs_api():
    api_key = get_ssm('/job-pipeline/usajobs-api-key')
    headers = {
        'Authorization-Key': api_key,
        'User-Agent': 'juanakuspinelli@gmail.com'
    }
    r = requests.get(
        'https://data.usajobs.gov/api/search?Keyword=Data+Engineer&ResultsPerPage=1',
        headers=headers,
        timeout=10
    )
    assert r.status_code == 200, f"USAJobs returned {r.status_code}"
    data = r.json()
    assert 'SearchResult' in data, "USAJobs response missing SearchResult"

def test_cosmos_db_connection():
    conn = get_ssm('/job-pipeline/cosmos-connection-string')
    client = CosmosClient.from_connection_string(conn)
    db = client.get_database_client('job-scraper-db')
    container = db.get_container_client('seen-jobs')
    # Just verify we can read from it
    list(container.read_all_items(max_item_count=1))

def test_dynamodb_table_exists():
    dynamodb = boto3.client('dynamodb', region_name='us-east-1')
    response = dynamodb.list_tables()
    assert 'job-pipeline-metadata' in response['TableNames'], "DynamoDB table not found"

def test_api_endpoint():
    r = requests.get(
        'https://q0xo68b302.execute-api.us-east-1.amazonaws.com/Prod/jobs',
        timeout=10
    )
    assert r.status_code == 200, f"API returned {r.status_code}"
    data = r.json()
    assert 'total' in data, "API response missing total key"
    assert 'jobs' in data, "API response missing jobs key"

# ── Run All Tests ─────────────────────────────────────────────────────────────
print("\n🧪 Running Job Scraper Pipeline Tests\n")

test("SSM — USAJobs API key exists", test_ssm_usajobs_key)
test("SSM — Gemini API key exists", test_ssm_gemini_key)
test("SSM — Cosmos DB connection string exists", test_ssm_cosmos_connection)
test("S3 — bucket exists", test_s3_bucket_exists)
test("S3 — candidate_profile.json exists", test_s3_profile_exists)
test("S3 — candidate profile structure is valid", test_candidate_profile_structure)
test("API — Remotive returns jobs", test_remotive_api)
test("API — USAJobs returns jobs", test_usajobs_api)
test("Azure — Cosmos DB connection works", test_cosmos_db_connection)
test("AWS — DynamoDB table exists", test_dynamodb_table_exists)
test("AWS — API Gateway endpoint responds", test_api_endpoint)

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
print(f"{'='*50}\n")

if failed > 0:
    sys.exit(1)
