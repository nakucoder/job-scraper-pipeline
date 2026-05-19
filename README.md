# 🤖 Multi-Cloud AI Job Scraper Pipeline

A fully automated, serverless, multi-cloud job scraper that runs daily, enriches listings with AI scoring, and delivers a personalized digest to your inbox.

## 🏗️ Architecture
GCP Cloud Scheduler → Cloud Function → Vertex AI (Gemini)
→ Azure Cosmos DB (dedup)
→ AWS S3 (data lake)
→ AWS Lambda (filter + email)
→ AWS SES (email digest)
→ AWS Athena (analytics)
→ AWS API Gateway (/jobs endpoint)


## ☁️ Multi-Cloud Strategy

| Cloud | Service | Purpose | Cost |
|-------|---------|---------|------|
| GCP | Cloud Scheduler | Daily cron trigger | Free |
| GCP | Cloud Functions | Scraper + AI enrichment | Free tier |
| GCP | Gemini Flash Lite | AI job scoring | Free tier |
| Azure | Cosmos DB | Deduplication store | Free forever |
| AWS | S3 | Data lake (YYYY/MM/DD partitions) | Free tier |
| AWS | Lambda ×2 | Filter + API | Free tier |
| AWS | SES | Email digest | Free tier |
| AWS | API Gateway | /jobs endpoint | Free tier |
| AWS | Athena | SQL analytics on S3 | ~$0 at this scale |
| AWS | DynamoDB | Pipeline metadata | Free tier |
| AWS | CloudWatch | Logs + alerts | Free tier |

**Total monthly cost: $0.00**

## 🧠 AI Enrichment

Each job posting is passed through Gemini Flash Lite which returns:

```json
{
  "match_score_percent": 85,
  "is_remote": true,
  "estimated_salary_min": 75000,
  "estimated_salary_max": 95000,
  "primary_cloud": "AWS",
  "constraint_violations": [],
  "upskill_recommendations": ["Spark", "Kafka"]
}
```

## 📋 Job Sources

- **USAJobs API** — Federal government listings
- **Remotive API** — Remote tech jobs
- **Stack Overflow RSS** — Technical roles
- **Indeed RSS** — Local Miami listings

## 🗂️ Project Structure

```
job-scraper-pipeline/
├── template.yaml              # SAM template — all AWS infrastructure
├── config/
│   └── candidate_profile.json # Runtime config — skills, salary, locations
└── functions/
    ├── scraper/               # GCP Cloud Function
    │   ├── main.py
    │   └── requirements.txt
    ├── filter/                # AWS Lambda — email digest
    │   ├── app.py
    │   └── requirements.txt
    └── api/                   # AWS Lambda — /jobs endpoint
        ├── app.py
        └── requirements.txt
```


## 🔐 Security

- All secrets stored in AWS SSM Parameter Store as SecureString
- No credentials in code or `.env` files committed to GitHub
- IAM policies scoped per Lambda (least privilege)
- GCP Service Account with minimum permissions
- S3 bucket blocks all public access

## 🚀 Deployment

### AWS (SAM)
```bash
sam deploy
```

### GCP (Cloud Function)
```bash
gcloud functions deploy job-scraper \
  --gen2 \
  --runtime=python312 \
  --region=us-central1 \
  --source=functions/scraper \
  --entry-point=scraper \
  --trigger-http \
  --timeout=540s \
  --memory=512MB
```

## 📊 API Endpoint


GET https://q0xo68b302.execute-api.us-east-1.amazonaws.com/Prod/jobs


### Query Parameters
| Parameter | Type | Description | Example |
|-----------|------|-------------|---------|
| days | int | Days of history to query | ?days=7 |
| min_score | int | Minimum match score | ?min_score=70 |
| remote_only | bool | Remote jobs only | ?remote_only=true |
| source | string | Filter by source | ?source=USAJobs |

## 🗣️ Interview Talking Points

- **Multi-cloud by design** — each provider's strongest free tier for each task
- **AI as ETL** — Gemini converts unstructured job text into structured, queryable data
- **Config over code** — candidate_profile.json in S3 means zero redeployment to update preferences
- **Least privilege IAM** — each Lambda has only the permissions it needs
- **Observability** — CloudWatch logs + SNS alerts on every run

## 👨‍💻 Author

Juan Spinelli — Miami, FL
