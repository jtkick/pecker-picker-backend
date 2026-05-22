from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings


class MediaStorage(S3Boto3Storage):
    """
    Stores files in MinIO (or S3 when the time comes).

    boto3 talks to MINIO_ENDPOINT_URL internally; the url() method returns
    the public URL so Flutter clients can load images directly from MinIO.
    Swapping to real S3 later means only changing env vars — no code changes.
    """

    def url(self, name, parameters=None, expire=None, http_method=None):
        return f"{settings.MEDIA_PUBLIC_URL}/{settings.AWS_STORAGE_BUCKET_NAME}/{name}"
