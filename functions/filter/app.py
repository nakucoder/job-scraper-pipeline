import json
import os
import boto3
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
S3_BUCKET = os.environ.get('S3_BUCKET', 'job-scraper-pipeline-juan')
TODAY = datetime.utcnow().strftime('%Y/%m/%d')
SENDER_EMAIL = 'juanakuspinelli@gmail.com'
RECIPIENT_EMAIL = 'juanakuspinelli@gmail.com'

def lambda_handler(event, context):
    s3 = boto3.client('s3', region_name='us-east-1')
    ses = boto3.client('ses', region_name='us-east-1')
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE', 'job-pipeline-metadata'))

    # ── Find today's jobs file via prefix scan ───────────────────────────────
    prefix = f"jobs/{TODAY}/"
    try:
        response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        contents = response.get('Contents', [])
        if not contents:
            print(f"No jobs file found under prefix: {prefix}")
            send_heartbeat_email(ses, "No jobs file was written to S3 today — the scraper may not have run.")
            return {'statusCode': 200, 'body': 'No jobs file found today'}
        latest = max(contents, key=lambda obj: obj['LastModified'])
        obj = s3.get_object(Bucket=S3_BUCKET, Key=latest['Key'])
        jobs = json.loads(obj['Body'].read())
        print(f"Reading {latest['Key']}")
    except Exception as e:
        print(f"Error reading S3: {e}")
        return {'statusCode': 500, 'body': str(e)}

    if not jobs:
        print("No new jobs today")
        send_heartbeat_email(ses, "All job listings found today were already seen in previous runs.")
        return {'statusCode': 200, 'body': 'No new jobs today'}

    # ── Sort by match score ──────────────────────────────────────────────────
    jobs = sorted(jobs, key=lambda x: x.get('match_score_percent', 0), reverse=True)

    # ── Build email ──────────────────────────────────────────────────────────
    email_body = build_email_html(jobs)

    # ── Send via SES ─────────────────────────────────────────────────────────
    try:
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': [RECIPIENT_EMAIL]},
            Message={
                'Subject': {
                    'Data': f'🚀 Job Digest — {len(jobs)} New Listings ({TODAY})',
                    'Charset': 'UTF-8'
                },
                'Body': {
                    'Html': {
                        'Data': email_body,
                        'Charset': 'UTF-8'
                    }
                }
            }
        )
        print(f"Email sent with {len(jobs)} jobs")
    except Exception as e:
        print(f"Error sending email: {e}")
        return {'statusCode': 500, 'body': str(e)}

    # ── Log run to DynamoDB ───────────────────────────────────────────────────
    try:
        table.put_item(Item={
            'run_id': f"filter-{TODAY}",
            'jobs_sent': len(jobs),
            'run_date': TODAY,
            'status': 'success'
        })
    except Exception as e:
        print(f"Error logging to DynamoDB: {e}")

    return {'statusCode': 200, 'body': f'Sent {len(jobs)} jobs'}

# ── Heartbeat Email ───────────────────────────────────────────────────────────
def send_heartbeat_email(ses, reason):
    try:
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={'ToAddresses': [RECIPIENT_EMAIL]},
            Message={
                'Subject': {
                    'Data': f'🟢 Pipeline Heartbeat — 0 New Listings ({TODAY})',
                    'Charset': 'UTF-8'
                },
                'Body': {
                    'Html': {
                        'Data': f'<p style="font-family:sans-serif;color:#374151;">{reason}</p>',
                        'Charset': 'UTF-8'
                    }
                }
            }
        )
        print(f"Heartbeat email sent: {reason}")
    except Exception as e:
        print(f"Error sending heartbeat email: {e}")

# ── Email HTML Builder ────────────────────────────────────────────────────────
def build_email_html(jobs):
    rows = ''
    for job in jobs:
        score = job.get('match_score_percent', 0)
        violations = job.get('constraint_violations', [])
        upskills = job.get('upskill_recommendations', [])
        salary_min = job.get('estimated_salary_min', 0)
        salary_max = job.get('estimated_salary_max', 0)

        # Score color
        if score >= 75:
            score_color = '#22c55e'
        elif score >= 50:
            score_color = '#f59e0b'
        else:
            score_color = '#ef4444'

        salary_str = f'${salary_min:,} - ${salary_max:,}' if salary_min and salary_max else 'Not specified'
        violations_str = '<br>'.join([f'⚠️ {v}' for v in violations]) if violations else '✅ No violations'
        upskills_str = ', '.join(upskills) if upskills else 'None'
        remote_str = '✅ Remote' if job.get('is_remote') else '🏢 On-site'

        rows += f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin-bottom:16px;font-family:sans-serif;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <h2 style="margin:0;font-size:18px;color:#111827;">{job.get('title', 'Unknown')}</h2>
                <span style="background:{score_color};color:white;padding:4px 12px;border-radius:20px;font-weight:bold;">{score}% match</span>
            </div>
            <p style="margin:4px 0;color:#6b7280;">{job.get('company', 'Unknown')} — {job.get('location', 'Unknown')} — {remote_str}</p>
            <p style="margin:4px 0;color:#6b7280;">💰 {salary_str} | ☁️ {job.get('primary_cloud', 'Not specified')} | 📌 {job.get('source', 'Unknown')}</p>
            <hr style="border:none;border-top:1px solid #e5e7eb;margin:12px 0;">
            <p style="margin:4px 0;font-size:14px;">{violations_str}</p>
            <p style="margin:4px 0;font-size:14px;color:#6b7280;">📚 Upskill: {upskills_str}</p>
            <a href="{job.get('url', '#')}" style="display:inline-block;margin-top:12px;background:#2563eb;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:14px;">View Job →</a>
        </div>
        """

    return f"""
    <html>
    <body style="background:#f9fafb;padding:24px;font-family:sans-serif;">
        <div style="max-width:700px;margin:0 auto;">
            <h1 style="color:#111827;">🚀 Your Daily Job Digest</h1>
            <p style="color:#6b7280;">Found <strong>{len(jobs)}</strong> new listings today, sorted by match score.</p>
            {rows}
            <p style="color:#9ca3af;font-size:12px;margin-top:24px;">Powered by your Multi-Cloud Job Scraper Pipeline ⚡</p>
        </div>
    </body>
    </html>
    """
