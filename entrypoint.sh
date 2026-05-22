#!/bin/sh
set -e

echo "Waiting for MinIO..."
until python - <<'EOF'
import boto3, os, sys
try:
    boto3.client(
        's3',
        endpoint_url=os.environ['MINIO_ENDPOINT_URL'],
        aws_access_key_id=os.environ['MINIO_ACCESS_KEY'],
        aws_secret_access_key=os.environ['MINIO_SECRET_KEY'],
    ).list_buckets()
    sys.exit(0)
except Exception:
    sys.exit(1)
EOF
do
    sleep 2
done

echo "Running migrations..."
python manage.py migrate --noinput

echo "Ensuring bucket exists and is public..."
python - <<'EOF'
import boto3, json, os

s3 = boto3.client(
    's3',
    endpoint_url=os.environ['MINIO_ENDPOINT_URL'],
    aws_access_key_id=os.environ['MINIO_ACCESS_KEY'],
    aws_secret_access_key=os.environ['MINIO_SECRET_KEY'],
)
bucket = os.environ['MINIO_BUCKET']

try:
    s3.create_bucket(Bucket=bucket)
    print(f"Created bucket: {bucket}")
except s3.exceptions.BucketAlreadyOwnedByYou:
    print(f"Bucket already exists: {bucket}")
except Exception as e:
    print(f"Bucket setup note: {e}")

policy = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": ["*"]},
        "Action": ["s3:GetObject"],
        "Resource": [f"arn:aws:s3:::{bucket}/*"],
    }],
})
s3.put_bucket_policy(Bucket=bucket, Policy=policy)
print("Bucket policy set to public read.")
EOF

echo "Starting gunicorn..."
exec gunicorn pecker_picker.wsgi:application \
    --bind 0.0.0.0:3425 \
    --workers 2 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
