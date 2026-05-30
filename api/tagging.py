"""
Automatic image tagging via AWS Rekognition.

Set AWS_REKOGNITION_REGION in the environment to enable.  When it is blank
auto_tag() returns an empty dict and tagging is silently skipped, so callers
can fall back to manually specified tags.

Required IAM permissions:
    rekognition:DetectFaces
    rekognition:DetectLabels
    rekognition:DetectModerationLabels
"""

import logging
import boto3
from django.conf import settings

logger = logging.getLogger(__name__)

# Rekognition label names that indicate a body/torso-focused image.
_TORSO_LABELS = {
    'Human Body', 'Chest', 'Barechested Male', 'Barechested Person',
    'Torso', 'Back', 'Abdomen', 'Underwear', 'Lingerie', 'Swimwear',
    'Bikini', 'Bodybuilding', 'Shirtless',
}

# Moderation label names / parent names we treat as explicit.
_EXPLICIT_LABELS = {
    'Explicit Nudity', 'Nudity', 'Graphic Male Nudity', 'Graphic Female Nudity',
    'Sexual Activity', 'Illustrated Explicit Nudity', 'Adult Nudity',
}


def _client():
    region = getattr(settings, 'AWS_REKOGNITION_REGION', '')
    if not region:
        return None
    key    = getattr(settings, 'AWS_REKOGNITION_ACCESS_KEY_ID', '') or None
    secret = getattr(settings, 'AWS_REKOGNITION_SECRET_ACCESS_KEY', '') or None
    return boto3.client(
        'rekognition',
        region_name=region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
    )


def auto_tag(image_bytes: bytes) -> dict:
    """
    Analyse image_bytes with Rekognition and return a dict ready to be
    unpacked into ProfileImage field kwargs:
        {'tag_face': bool, 'tag_torso': bool, 'tag_explicit': bool, 'tag_other': bool}

    Returns an empty dict if Rekognition is not configured or the call fails,
    allowing the caller to skip the update or fall back to manual tags.

    tag_other is True when none of the other three tags are set — any image
    with no recognised person/face/explicit content.
    """
    client = _client()
    if client is None:
        return {}

    img = {'Bytes': image_bytes}
    tag_face = tag_torso = tag_explicit = False

    try:
        resp = client.detect_faces(Image=img, Attributes=['DEFAULT'])
        tag_face = any(
            f['Confidence'] >= 80
            for f in resp.get('FaceDetails', [])
        )
    except Exception as exc:
        logger.warning("Rekognition detect_faces failed: %s", exc)

    try:
        resp = client.detect_labels(Image=img, MinConfidence=65)
        tag_torso = any(
            label['Name'] in _TORSO_LABELS
            for label in resp.get('Labels', [])
        )
    except Exception as exc:
        logger.warning("Rekognition detect_labels failed: %s", exc)

    try:
        resp = client.detect_moderation_labels(Image=img, MinConfidence=60)
        tag_explicit = any(
            label['Name'] in _EXPLICIT_LABELS
            or label.get('ParentName', '') in _EXPLICIT_LABELS
            for label in resp.get('ModerationLabels', [])
        )
    except Exception as exc:
        logger.warning("Rekognition detect_moderation_labels failed: %s", exc)

    return {
        'tag_face':     tag_face,
        'tag_torso':    tag_torso,
        'tag_explicit': tag_explicit,
        'tag_other':    not (tag_face or tag_torso or tag_explicit),
    }
