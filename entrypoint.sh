#!/bin/sh
set -e

echo "Waiting for object storage..."
until python - <<'EOF'
import boto3, os, sys
from botocore.exceptions import ClientError
# head_bucket, not list_buckets: an R2 token scoped to one bucket can't list.
try:
    boto3.client(
        's3',
        endpoint_url=os.environ['MINIO_ENDPOINT_URL'],
        aws_access_key_id=os.environ['MINIO_ACCESS_KEY'],
        aws_secret_access_key=os.environ['MINIO_SECRET_KEY'],
        region_name=os.environ.get('MINIO_REGION', 'us-east-1'),
    ).head_bucket(Bucket=os.environ['MINIO_BUCKET'])
    sys.exit(0)
except ClientError as e:
    # Reachable but no bucket yet (fresh MinIO): it's created below.
    sys.exit(0 if e.response['Error']['Code'] in ('404', 'NoSuchBucket') else 1)
except Exception:
    sys.exit(1)
EOF
do
    sleep 2
done

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Ensuring bucket exists and is private..."
python - <<'EOF'
import boto3, os

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

# Media is served only through presigned URLs (api.media.presigned_url),
# so drop any public-read policy left from earlier setups.
try:
    s3.delete_bucket_policy(Bucket=bucket)
    print("Bucket policy removed (private).")
except Exception as e:
    print(f"Bucket policy note: {e}")
EOF

echo "Starting uvicorn..."
exec uvicorn pecker_picker.asgi:application \
    --host 0.0.0.0 \
    --port 3425 \
    --workers 2
