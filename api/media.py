"""
Media helpers: presigned (expiring) object URLs and upload metadata stripping.
"""
import hashlib
import hmac
import io
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presigned_url(key: str, now: float | None = None) -> str:
    """
    SigV4 presigned GET URL for `key` on the public media endpoint.

    The signing time is rounded down to half the expiry, so the URL for an object
    stays identical within that window and clients can cache images by URL.
    Every URL remains valid for at least half of MEDIA_URL_EXPIRY_SECONDS.
    """
    expires = settings.MEDIA_URL_EXPIRY_SECONDS
    window = max(expires // 2, 1)
    now = time.time() if now is None else now
    signed_at = datetime.fromtimestamp(int(now) // window * window, tz=timezone.utc)
    amz_date = signed_at.strftime('%Y%m%dT%H%M%SZ')
    datestamp = amz_date[:8]
    region = settings.AWS_S3_REGION_NAME
    scope = f'{datestamp}/{region}/s3/aws4_request'

    public = urlsplit(settings.MEDIA_PUBLIC_URL)
    path = f'{public.path.rstrip("/")}/{settings.AWS_STORAGE_BUCKET_NAME}/{key}'
    canonical_uri = quote(path, safe='/~')
    params = {
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': f'{settings.AWS_ACCESS_KEY_ID}/{scope}',
        'X-Amz-Date': amz_date,
        'X-Amz-Expires': str(expires),
        'X-Amz-SignedHeaders': 'host',
    }
    query = '&'.join(
        f'{quote(k, safe="-_.~")}={quote(v, safe="-_.~")}' for k, v in sorted(params.items())
    )
    canonical_request = '\n'.join([
        'GET', canonical_uri, query, f'host:{public.netloc}\n', 'host', 'UNSIGNED-PAYLOAD',
    ])
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key_date = _hmac(f'AWS4{settings.AWS_SECRET_ACCESS_KEY}'.encode(), datestamp)
    signing_key = _hmac(_hmac(_hmac(key_date, region), 's3'), 'aws4_request')
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f'{public.scheme}://{public.netloc}{canonical_uri}?{query}&X-Amz-Signature={signature}'


def strip_image_metadata(upload) -> ContentFile | None:
    """
    Re-encode an uploaded image without EXIF/XMP (GPS, device, timestamps),
    applying the EXIF orientation first. Returns None if it isn't a readable image.
    """
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError):
        return None
    fmt = img.format if img.format in ('JPEG', 'PNG', 'WEBP') else 'JPEG'
    img = ImageOps.exif_transpose(img)
    if fmt == 'JPEG' and img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    out = io.BytesIO()
    save_kwargs = {'quality': 90} if fmt in ('JPEG', 'WEBP') else {}
    img.save(out, format=fmt, **save_kwargs)
    ext = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}[fmt]
    stem = (upload.name or 'image').rsplit('.', 1)[0]
    return ContentFile(out.getvalue(), name=f'{stem}.{ext}')
