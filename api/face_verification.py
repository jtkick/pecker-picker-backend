"""
Face verification via AWS Rekognition compare_faces.

Uses the same _client() pattern and configuration as tagging.py.
Required IAM permission: rekognition:CompareFaces
"""

import logging
from .tagging import _client

logger = logging.getLogger(__name__)


def compare_selfie_to_gallery(selfie_bytes: bytes, gallery_images) -> str:
    """
    Compare a selfie against face-tagged gallery images.

    Returns:
        'verified' – a matching face was found in the gallery
        'rejected' – no match found (Rekognition ran successfully)
        'pending'  – Rekognition is not configured; manual review needed
    """
    client = _client()
    if client is None:
        return 'pending'

    for img in gallery_images:
        try:
            img.image.open('rb')
            gallery_bytes = img.image.read()
            img.image.close()
        except Exception as exc:
            logger.warning("Could not read gallery image %s: %s", img.pk, exc)
            continue

        try:
            resp = client.compare_faces(
                SourceImage={'Bytes': selfie_bytes},
                TargetImage={'Bytes': gallery_bytes},
                SimilarityThreshold=90.0,
            )
            if resp.get('FaceMatches'):
                return 'verified'
        except Exception as exc:
            logger.warning("Rekognition compare_faces failed for image %s: %s", img.pk, exc)

    return 'rejected'
