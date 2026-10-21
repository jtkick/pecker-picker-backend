from storages.backends.s3boto3 import S3Boto3Storage

from .media import presigned_url


class MediaStorage(S3Boto3Storage):
    """
    Stores files in MinIO (or S3 when the time comes).

    boto3 talks to MINIO_ENDPOINT_URL internally; the url() method returns an
    expiring presigned URL on the public endpoint so Flutter clients can load
    images directly from the (private) bucket.
    Swapping to real S3 later means only changing env vars — no code changes.
    """

    def url(self, name, parameters=None, expire=None, http_method=None):
        return presigned_url(name)
