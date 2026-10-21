import json
import logging
import random
import string
import threading
import uuid
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from django.db import IntegrityError, transaction
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce

from rest_framework_simplejwt.exceptions import TokenError
from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
    base64url_to_bytes,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorAttachment,
    ResidentKeyRequirement,
    UserVerificationRequirement,
    AttestationConveyancePreference,
)
from .models import User, Persona, PersonaPhoto, ProfileImage, Album, AlbumGrant, Conversation, ConversationMember, DeviceToken, Message, MessageReaction, SavedPhrase, LocationShareGrant, Block, Report, FaceVerification, Kink, Interest, PasskeyCredential, WebAuthnChallenge, OTPVerification
from .sms import send_otp_sms, send_verify_sms, check_verify_sms
from .serializers import (
    AccountSerializer, PersonaSummarySerializer, PersonaSerializer,
    GalleryImageSerializer, DiscoverPersonaSerializer, PersonaProfileSerializer,
    AlbumSerializer, AlbumGrantSerializer, LocationShareGrantSerializer, ConversationSerializer, MessageSerializer,
)
from .geocoding import reverse_geocode, nearby_label
from .tagging import auto_tag
from .media import strip_image_metadata
from .push import notify_new_message
from .realtime import broadcast, mark_receipt, send_to_users
from django.conf import settings


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


@api_view(["POST"])
@permission_classes([AllowAny])
def passkey_register_begin(request):
    user_handle = uuid.uuid4().bytes

    options = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_id=user_handle,
        user_name="user",
        user_display_name="",
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        timeout=60000,
    )

    from webauthn.helpers import bytes_to_base64url
    session_id = uuid.uuid4()
    WebAuthnChallenge.objects.create(
        session_id=session_id,
        challenge=bytes_to_base64url(options.challenge),
        action='register',
        expires_at=timezone.now() + timedelta(minutes=5),
    )

    response_data = json.loads(options_to_json(options))
    response_data['session_id'] = str(session_id)
    return Response(response_data)


@api_view(["POST"])
@permission_classes([AllowAny])
def passkey_register_complete(request):
    session_id = request.data.get('session_id')
    credential_data = request.data.get('credential')

    if not session_id or not credential_data:
        return Response({'detail': 'Missing session_id or credential.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        challenge = WebAuthnChallenge.objects.get(
            session_id=session_id, action='register', is_used=False,
        )
    except WebAuthnChallenge.DoesNotExist:
        return Response({'detail': 'Invalid or expired session.'}, status=status.HTTP_400_BAD_REQUEST)

    if challenge.is_expired():
        return Response({'detail': 'Session expired.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        verification = verify_registration_response(
            credential=credential_data,
            expected_challenge=base64url_to_bytes(challenge.challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGINS,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning("Passkey registration verification failed: %s", exc)
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if PasskeyCredential.objects.filter(credential_id=verification.credential_id).exists():
        return Response({'detail': 'Credential already registered.'}, status=status.HTTP_400_BAD_REQUEST)

    challenge.is_used = True
    challenge.save()

    user = User.objects.create_user()
    transports = (credential_data.get('response') or {}).get('transports', [])
    PasskeyCredential.objects.create(
        user=user,
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=transports,
    )

    refresh = RefreshToken.for_user(user)
    return Response(
        {'access': str(refresh.access_token), 'refresh': str(refresh), 'is_new_user': True},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def passkey_authenticate_begin(request):
    options = generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=60000,
    )

    from webauthn.helpers import bytes_to_base64url
    session_id = uuid.uuid4()
    WebAuthnChallenge.objects.create(
        session_id=session_id,
        challenge=bytes_to_base64url(options.challenge),
        action='authenticate',
        expires_at=timezone.now() + timedelta(minutes=5),
    )

    response_data = json.loads(options_to_json(options))
    response_data['session_id'] = str(session_id)
    return Response(response_data)


@api_view(["POST"])
@permission_classes([AllowAny])
def passkey_authenticate_complete(request):
    session_id = request.data.get('session_id')
    credential_data = request.data.get('credential')

    if not session_id or not credential_data:
        return Response({'detail': 'Missing session_id or credential.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        challenge = WebAuthnChallenge.objects.get(
            session_id=session_id, action='authenticate', is_used=False,
        )
    except WebAuthnChallenge.DoesNotExist:
        return Response({'detail': 'Invalid or expired session.'}, status=status.HTTP_400_BAD_REQUEST)

    if challenge.is_expired():
        return Response({'detail': 'Session expired.'}, status=status.HTTP_400_BAD_REQUEST)

    cred_id_b64 = credential_data.get('id')
    if not cred_id_b64:
        return Response({'detail': 'Missing credential id.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        stored = PasskeyCredential.objects.select_related('user').get(
            credential_id=base64url_to_bytes(cred_id_b64),
        )
    except PasskeyCredential.DoesNotExist:
        return Response({'detail': 'Unknown credential.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        verification = verify_authentication_response(
            credential=credential_data,
            expected_challenge=base64url_to_bytes(challenge.challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGINS,
            credential_public_key=bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning("Passkey authentication verification failed: %s", exc)
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    challenge.is_used = True
    challenge.save()

    stored.sign_count = verification.new_sign_count
    stored.save(update_fields=['sign_count'])

    refresh = RefreshToken.for_user(stored.user)
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'is_new_user': False,
    })


@api_view(["POST"])
def logout(request):
    refresh_token = request.data.get("refresh")
    if not refresh_token:
        return Response({"detail": "Refresh token required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        token = RefreshToken(refresh_token)
        token.blacklist()
    except TokenError:
        pass  # Already invalid or blacklisted — logout is still successful
    return Response(status=status.HTTP_204_NO_CONTENT)


def _generate_otp() -> str:
    return ''.join(random.choices(string.digits, k=6))


def _create_and_send_otp(phone_number: str) -> None:
    if settings.TWILIO_ENABLED:
        send_verify_sms(phone_number)
        return
    code = _generate_otp()
    OTPVerification.objects.create(
        phone_number=phone_number,
        code=code,
        expires_at=timezone.now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
    )
    send_otp_sms(phone_number, code)


def _verify_otp_code(phone_number: str, code: str):
    """Returns the OTPVerification if valid (dev), or None after Twilio approval, else raises ValueError."""
    if settings.TWILIO_ENABLED:
        if not check_verify_sms(phone_number, code):
            raise ValueError('Invalid or expired code.')
        return None
    try:
        otp = OTPVerification.objects.filter(
            phone_number=phone_number, code=code, is_used=False,
        ).latest('created_at')
    except OTPVerification.DoesNotExist:
        raise ValueError('Invalid code.')
    if not otp.is_valid():
        raise ValueError('Code expired.')
    otp.is_used = True
    otp.save()
    return otp


@api_view(["POST"])
@permission_classes([AllowAny])
def otp_request(request):
    phone = (request.data.get('phone_number') or '').strip()
    if not phone:
        return Response({'detail': 'phone_number required.'}, status=status.HTTP_400_BAD_REQUEST)
    _create_and_send_otp(phone)
    return Response({'detail': 'Code sent.'})


@api_view(["POST"])
@permission_classes([AllowAny])
def otp_verify(request):
    phone = (request.data.get('phone_number') or '').strip()
    code = (request.data.get('code') or '').strip()
    if not phone or not code:
        return Response({'detail': 'phone_number and code required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _verify_otp_code(phone, code)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    user, created = User.objects.get_or_create(
        phone_number=phone,
        defaults={'phone_verified': True},
    )
    if not created and not user.phone_verified:
        user.phone_verified = True
        user.save(update_fields=['phone_verified'])

    refresh = RefreshToken.for_user(user)
    return Response(
        {'access': str(refresh.access_token), 'refresh': str(refresh), 'is_new_user': created},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["POST"])
def link_phone_request(request):
    phone = (request.data.get('phone_number') or '').strip()
    if not phone:
        return Response({'detail': 'phone_number required.'}, status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(phone_number=phone).exclude(pk=request.user.pk).exists():
        return Response({'detail': 'Phone number already in use.'}, status=status.HTTP_400_BAD_REQUEST)
    _create_and_send_otp(phone)
    return Response({'detail': 'Code sent.'})


@api_view(["POST"])
def link_phone_verify(request):
    phone = (request.data.get('phone_number') or '').strip()
    code = (request.data.get('code') or '').strip()
    if not phone or not code:
        return Response({'detail': 'phone_number and code required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _verify_otp_code(phone, code)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(phone_number=phone).exclude(pk=request.user.pk).exists():
        return Response({'detail': 'Phone number already in use.'}, status=status.HTTP_400_BAD_REQUEST)
    request.user.phone_number = phone
    request.user.phone_verified = True
    request.user.save(update_fields=['phone_number', 'phone_verified'])
    return Response({'detail': 'Phone linked.'})


@api_view(["DELETE"])
def unlink_phone(request):
    if not request.user.phone_number:
        return Response({'detail': 'No phone linked.'}, status=status.HTTP_400_BAD_REQUEST)
    if not request.user.passkey_credentials.exists():
        return Response(
            {'detail': 'Cannot remove phone number without a passkey linked first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    request.user.phone_number = None
    request.user.phone_verified = False
    request.user.save(update_fields=['phone_number', 'phone_verified'])
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def link_passkey_begin(request):
    user_handle = str(request.user.pk).encode()
    options = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_id=user_handle,
        user_name=str(request.user.pk),
        user_display_name=request.user.personas.filter(is_primary=True).values_list('display_name', flat=True).first() or '',
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        timeout=60000,
    )

    from webauthn.helpers import bytes_to_base64url
    session_id = uuid.uuid4()
    WebAuthnChallenge.objects.create(
        session_id=session_id,
        challenge=bytes_to_base64url(options.challenge),
        action='link_passkey',
        expires_at=timezone.now() + timedelta(minutes=5),
    )

    response_data = json.loads(options_to_json(options))
    response_data['session_id'] = str(session_id)
    return Response(response_data)


@api_view(["POST"])
def link_passkey_complete(request):
    session_id = request.data.get('session_id')
    credential_data = request.data.get('credential')
    if not session_id or not credential_data:
        return Response({'detail': 'Missing session_id or credential.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        challenge = WebAuthnChallenge.objects.get(
            session_id=session_id, action='link_passkey', is_used=False,
        )
    except WebAuthnChallenge.DoesNotExist:
        return Response({'detail': 'Invalid or expired session.'}, status=status.HTTP_400_BAD_REQUEST)

    if challenge.is_expired():
        return Response({'detail': 'Session expired.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        verification = verify_registration_response(
            credential=credential_data,
            expected_challenge=base64url_to_bytes(challenge.challenge),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGINS,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning("Passkey link verification failed: %s", exc)
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if PasskeyCredential.objects.filter(credential_id=verification.credential_id).exists():
        return Response({'detail': 'Credential already registered.'}, status=status.HTTP_400_BAD_REQUEST)

    challenge.is_used = True
    challenge.save()

    transports = (credential_data.get('response') or {}).get('transports', [])
    PasskeyCredential.objects.create(
        user=request.user,
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=transports,
    )
    return Response({'detail': 'Passkey linked.'}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def unlink_passkey(request, pk):
    try:
        cred = request.user.passkey_credentials.get(pk=pk)
    except PasskeyCredential.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    remaining = request.user.passkey_credentials.exclude(pk=pk).count()
    if remaining == 0 and not request.user.phone_number:
        return Response(
            {'detail': 'Cannot remove last credential.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cred.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
def credentials(request):
    passkeys = list(request.user.passkey_credentials.values('id', 'created_at', 'transports'))
    return Response({
        'phone_number': request.user.phone_number,
        'phone_verified': request.user.phone_verified,
        'passkeys': passkeys,
    })


@api_view(["GET", "PATCH"])
def me(request):
    ctx = {'request': request}
    request.user.get_active_persona()
    if request.method == "GET":
        return Response(AccountSerializer(request.user, context=ctx).data)
    serializer = AccountSerializer(request.user, data=request.data, partial=True, context=ctx)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    if 'latitude' in request.data or 'longitude' in request.data:
        user = request.user
        User.objects.filter(pk=user.pk).update(location_updated_at=timezone.now())
        if user.latitude is not None and user.longitude is not None:
            lat, lng, pk = user.latitude, user.longitude, user.pk
            threading.Thread(
                target=lambda: User.objects.filter(pk=pk).update(
                    neighborhood=reverse_geocode(lat, lng)
                ),
                daemon=True,
            ).start()
    return Response(serializer.data)


# ── Personas ──────────────────────────────────────────────────────────────────

def _photos_prefetch():
    return Prefetch('photos', queryset=PersonaPhoto.objects.select_related('image'))


def _persona_qs():
    return Persona.objects.select_related('user', 'user__verification').prefetch_related(_photos_prefetch())


def _visible_persona(request, pk):
    """Another account's persona the caller may interact with (active account, no block either way)."""
    try:
        persona = _persona_qs().filter(user__is_active=True).exclude(user=request.user).get(pk=pk)
    except (Persona.DoesNotExist, TypeError, ValueError):
        return None
    if Block.objects.filter(
        Q(blocker=request.user, blocked=persona.user) | Q(blocker=persona.user, blocked=request.user)
    ).exists():
        return None
    return persona


def _unread_counts(user) -> dict[int, int]:
    """Unread incoming messages per persona owned by `user`."""
    counts = dict.fromkeys(user.personas.values_list('pk', flat=True), 0)
    rows = (
        ConversationMember.objects
        .filter(persona__user=user, left_at__isnull=True)
        .annotate(unread=Count(
            'conversation__messages',
            filter=Q(conversation__messages__id__gt=Coalesce(F('last_read_message_id'), 0))
            & ~Q(conversation__messages__sender_id=F('persona_id'))
            & Q(conversation__messages__deleted_at__isnull=True),
        ))
        .values_list('persona_id', 'unread')
    )
    for persona_id, unread in rows:
        counts[persona_id] += unread
    return counts


def _persona_ctx(request):
    return {
        'request': request,
        'unlocked_ids': request.user.unlocked_persona_ids(),
        'unread_counts': _unread_counts(request.user),
    }


def _persona_list_response(request, status_code=status.HTTP_200_OK):
    qs = request.user.personas.prefetch_related(_photos_prefetch())
    return Response(
        PersonaSummarySerializer(qs, many=True, context=_persona_ctx(request)).data,
        status=status_code,
    )


def _image_ids_param(request, max_count=None):
    """Validate `image_ids` as a list of the caller's library image ids. Returns (ids, error_response)."""
    ids = request.data.get('image_ids')
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids) or len(set(ids)) != len(ids):
        return None, Response({'error': 'image_ids must be a list of unique integers.'}, status=status.HTTP_400_BAD_REQUEST)
    if max_count is not None and len(ids) > max_count:
        return None, Response({'error': f'At most {max_count} photos allowed.'}, status=status.HTTP_400_BAD_REQUEST)
    if request.user.gallery_images.filter(pk__in=ids).count() != len(ids):
        return None, Response({'error': 'Unknown image.'}, status=status.HTTP_400_BAD_REQUEST)
    return ids, None


@api_view(["GET", "POST"])
def personas(request):
    user = request.user
    user.get_active_persona()
    if request.method == "GET":
        return _persona_list_response(request)

    limit = user.persona_limit()
    if user.personas.count() >= limit:
        is_plus = user.has_tier(User.TIER_PLUS)
        return Response(
            {'error': f'Your plan allows up to {limit} profiles.',
             'requires_tier': None if is_plus else User.TIER_PLUS},
            status=status.HTTP_403_FORBIDDEN,
        )

    source = None
    if request.data.get('duplicate_from') is not None:
        source = user.personas.filter(pk=request.data.get('duplicate_from')).first()
        if source is None:
            return Response({'error': 'Profile not found.'}, status=status.HTTP_404_NOT_FOUND)

    persona = Persona(user=user, sort_order=user.personas.count())
    if source:
        for field in Persona.PROFILE_FIELDS:
            setattr(persona, field, getattr(source, field))
        persona.color = source.color
    ctx = _persona_ctx(request)
    serializer = PersonaSerializer(persona, data=request.data, partial=True, context=ctx)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        persona = serializer.save()
        if source:
            PersonaPhoto.objects.bulk_create(
                PersonaPhoto(persona=persona, image_id=pp.image_id, order=pp.order)
                for pp in source.photos.all()
            )
    ctx['unlocked_ids'] = user.unlocked_persona_ids()
    return Response(PersonaSerializer(persona, context=ctx).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
def persona_detail(request, pk):
    user = request.user
    persona = get_object_or_404(user.personas.prefetch_related(_photos_prefetch()), pk=pk)
    ctx = _persona_ctx(request)

    if request.method == "GET":
        return Response(PersonaSerializer(persona, context=ctx).data)

    if request.method == "PATCH":
        serializer = PersonaSerializer(persona, data=request.data, partial=True, context=ctx)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    # DELETE
    if persona.is_primary:
        return Response({'error': 'The primary profile cannot be deleted.'}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        if user.active_persona_id == persona.pk:
            primary = user.personas.get(is_primary=True)
            User.objects.filter(pk=user.pk).update(active_persona=primary)
        # Chats belong to the persona; remove them for both sides.
        Conversation.objects.filter(participants=persona).delete()
        persona.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def persona_activate(request, pk):
    user = request.user
    persona = get_object_or_404(user.personas, pk=pk)
    if persona.pk not in user.unlocked_persona_ids():
        return Response(
            {'error': 'This profile is locked. Upgrade to use more profiles.',
             'requires_tier': User.TIER_PLUS},
            status=status.HTTP_403_FORBIDDEN,
        )
    Persona.objects.filter(pk=persona.pk).update(last_active_at=timezone.now())
    User.objects.filter(pk=user.pk).update(active_persona=persona)
    user.active_persona = persona
    return _persona_list_response(request)


@api_view(["PUT"])
def persona_photos(request, pk):
    persona = get_object_or_404(request.user.personas, pk=pk)
    ids, err = _image_ids_param(request, settings.MAX_GALLERY_IMAGES)
    if err:
        return err
    with transaction.atomic():
        persona.photos.all().delete()
        PersonaPhoto.objects.bulk_create(
            PersonaPhoto(persona=persona, image_id=image_id, order=i) for i, image_id in enumerate(ids)
        )
    persona = request.user.personas.prefetch_related(_photos_prefetch()).get(pk=pk)
    return Response(PersonaSerializer(persona, context=_persona_ctx(request)).data)


# ── Photo library & albums ────────────────────────────────────────────────────

def _validate_image(request):
    image = request.FILES.get('image')
    if not image:
        return None, Response({'detail': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if image.size > max_bytes:
        return None, Response(
            {'detail': f'Image must be under {settings.MAX_UPLOAD_SIZE_MB}MB.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return image, None



def _apply_tags(pk: int, image_bytes: bytes) -> None:
    """Background task: run Rekognition and write tags back to the DB."""
    tags = auto_tag(image_bytes)
    if tags:
        ProfileImage.objects.filter(pk=pk).update(**tags)


@api_view(["GET", "POST"])
def photos(request):
    if request.method == "GET":
        return Response(GalleryImageSerializer(
            request.user.gallery_images.all(), many=True, context={'request': request},
        ).data)

    limit = request.user.library_limit()
    if request.user.gallery_images.count() >= limit:
        return Response(
            {'detail': f'Maximum {limit} photos allowed.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    image, err = _validate_image(request)
    if err:
        return err
    # Re-encode without EXIF so location/device metadata never reaches other users.
    image = strip_image_metadata(image)
    if image is None:
        return Response({'detail': 'Unsupported image format.'}, status=status.HTTP_400_BAD_REQUEST)
    image_bytes = image.read()
    image.seek(0)

    manual_tags = request.data.getlist('tags', [])
    obj = ProfileImage.objects.create(
        user=request.user,
        image=image,
        order=request.user.gallery_images.count(),
        tag_face='face' in manual_tags,
        tag_torso='torso' in manual_tags,
        tag_explicit='explicit' in manual_tags,
        tag_other='other' in manual_tags,
    )

    if not manual_tags:
        # No manual tags supplied — let Rekognition decide in the background.
        threading.Thread(
            target=_apply_tags, args=(obj.pk, image_bytes), daemon=True,
        ).start()

    return Response(
        GalleryImageSerializer(obj, context={'request': request}).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["DELETE"])
def photo_delete(request, pk):
    try:
        obj = request.user.gallery_images.get(pk=pk)
    except ProfileImage.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    obj.image.delete(save=False)
    obj.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


def _album_name_param(request):
    name = (request.data.get('name') or '').strip()
    if not name or len(name) > 50:
        return None, Response({'error': 'name must be 1–50 characters.'}, status=status.HTTP_400_BAD_REQUEST)
    return name, None


@api_view(["GET", "POST"])
def albums(request):
    ctx = {'request': request}
    if request.method == "GET":
        qs = request.user.albums.prefetch_related('images')
        return Response(AlbumSerializer(qs, many=True, context=ctx).data)

    name, err = _album_name_param(request)
    if err:
        return err
    album = Album.objects.create(user=request.user, name=name)
    return Response(AlbumSerializer(album, context=ctx).data, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def album_detail(request, pk):
    album = get_object_or_404(request.user.albums, pk=pk)
    if request.method == "DELETE":
        album.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    name, err = _album_name_param(request)
    if err:
        return err
    album.name = name
    album.save(update_fields=['name'])
    return Response(AlbumSerializer(album, context={'request': request}).data)


@api_view(["PUT"])
def album_photos(request, pk):
    album = get_object_or_404(request.user.albums, pk=pk)
    ids, err = _image_ids_param(request)
    if err:
        return err
    album.images.set(ids)
    return Response(AlbumSerializer(album, context={'request': request}).data)


@api_view(["GET", "POST"])
def album_grants(request):
    me = request.user.get_active_persona()
    if request.method == "GET":
        qs = me.album_grants_given.all()
        grantee_id = request.query_params.get('grantee_id')
        if grantee_id:
            qs = qs.filter(grantee_id=grantee_id)
        return Response(AlbumGrantSerializer(qs, many=True).data)

    try:
        album = request.user.albums.get(pk=request.data.get('album_id'))
    except (Album.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'Album not found.'}, status=status.HTTP_404_NOT_FOUND)
    grantee = _visible_persona(request, request.data.get('grantee_id'))
    if grantee is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    expires_at = request.data.get('expires_at')

    grant, created = AlbumGrant.objects.get_or_create(grantor=me, grantee=grantee, album=album)
    if expires_at is not None or created:
        grant.expires_at = expires_at
        grant.save(update_fields=['expires_at'])
    return Response(AlbumGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def album_grant_delete(request, pk):
    deleted, _ = AlbumGrant.objects.filter(pk=pk, grantor__user=request.user).delete()
    if not deleted:
        return Response(status=status.HTTP_404_NOT_FOUND)
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Discover & profiles ───────────────────────────────────────────────────────

def _block_exclusions(user):
    """Returns two querysets of IDs to exclude: users I blocked and users who blocked me."""
    blocked_by_me = Block.objects.filter(blocker=user).values_list('blocked_id', flat=True)
    blocked_me = Block.objects.filter(blocked=user).values_list('blocker_id', flat=True)
    return blocked_by_me, blocked_me


@api_view(["GET"])
def profiles_list(request):
    blocked_by_me, blocked_me = _block_exclusions(request.user)
    # Only each account's active persona is discoverable.
    qs = (
        _persona_qs()
        .filter(user__active_persona=F('pk'), user__is_active=True)
        .exclude(user=request.user)
        .exclude(display_name='')
        .exclude(user__in=blocked_by_me)
        .exclude(user__in=blocked_me)
    )

    # Bounding-box filter: only return users within ~50 km of the viewer.
    # 1 degree of latitude ≈ 111 km; longitude varies but this is a fast approximation.
    try:
        viewer_lat = float(request.query_params['lat'])
        viewer_lng = float(request.query_params['lng'])
        radius_deg = 50.0 / 111.0  # ~0.45°
        qs = qs.filter(
            user__latitude__isnull=False,
            user__longitude__isnull=False,
            user__latitude__range=(viewer_lat - radius_deg, viewer_lat + radius_deg),
            user__longitude__range=(viewer_lng - radius_deg, viewer_lng + radius_deg),
        )
    except (KeyError, ValueError, TypeError):
        pass  # no position provided — return all users (fallback)

    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(display_name__icontains=search)

    intent_param = request.query_params.get('intent')
    if intent_param is not None:
        try:
            qs = qs.filter(intent=int(intent_param))
        except ValueError:
            pass

    serializer = DiscoverPersonaSerializer(qs, many=True, context={'request': request})
    return Response(serializer.data)


@api_view(["GET"])
def profile_detail(request, pk):
    persona = _visible_persona(request, pk)
    me = request.user.get_active_persona()
    # Non-active personas are only viewable by people already chatting with them,
    # so an account's other profiles can't be enumerated.
    if persona is not None and persona.user.active_persona_id != persona.pk and not (
        Conversation.objects.filter(participants=persona).filter(participants=me).exists()
    ):
        persona = None
    if persona is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    ctx = {'request': request, 'viewer_persona': me}
    return Response(PersonaProfileSerializer(persona, context=ctx).data)


@api_view(["GET", "POST"])
def location_grants(request):
    me = request.user.get_active_persona()
    if request.method == "GET":
        qs = me.location_grants_given.all()
        grantee_id = request.query_params.get('grantee_id')
        if grantee_id:
            qs = qs.filter(grantee_id=grantee_id)
        return Response(LocationShareGrantSerializer(qs, many=True).data)

    if not request.user.has_tier(User.TIER_PLUS):
        return Response(
            {'error': 'Location sharing with specific people requires a Plus subscription.',
             'requires_tier': User.TIER_PLUS},
            status=status.HTTP_403_FORBIDDEN,
        )

    grantee = _visible_persona(request, request.data.get('grantee_id'))
    if grantee is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    level = request.data.get('level', 2)
    try:
        level = int(level)
        if level not in (1, 2):
            raise ValueError
    except (ValueError, TypeError):
        return Response({'error': 'level must be 1 or 2.'}, status=status.HTTP_400_BAD_REQUEST)

    expires_at = request.data.get('expires_at')

    grant, created = LocationShareGrant.objects.get_or_create(
        grantor=me, grantee=grantee,
        defaults={'level': level, 'expires_at': expires_at},
    )
    if not created:
        grant.level = level
        grant.expires_at = expires_at
        grant.save(update_fields=['level', 'expires_at'])
    return Response(LocationShareGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def location_grant_delete(request, pk):
    deleted, _ = LocationShareGrant.objects.filter(pk=pk, grantor__user=request.user).delete()
    if not deleted:
        return Response(status=status.HTTP_404_NOT_FOUND)
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── Conversations ─────────────────────────────────────────────────────────────

def _conversation_qs(me):
    """My active persona's conversations, with members, latest message and unread count."""
    my_last_read = ConversationMember.objects.filter(
        conversation=OuterRef('pk'), persona=me,
    ).values('last_read_message_id')[:1]
    unread = (
        Message.objects
        .filter(conversation=OuterRef('pk'), deleted_at__isnull=True)
        .filter(id__gt=Coalesce(OuterRef('my_last_read'), 0))
        .exclude(sender=me)
        .order_by()
        .values('conversation')
        .annotate(n=Count('id'))
        .values('n')
    )
    my_pinned = ConversationMember.objects.filter(conversation=OuterRef('pk'), persona=me).values('pinned')[:1]
    members = ConversationMember.objects.select_related('persona__user').prefetch_related(
        Prefetch('persona__photos', queryset=PersonaPhoto.objects.select_related('image')),
    )
    return (
        Conversation.objects
        .filter(members__persona=me, members__left_at__isnull=True)
        .annotate(my_last_read=Subquery(my_last_read))
        .annotate(unread=Coalesce(Subquery(unread), 0), my_pinned=Subquery(my_pinned))
        .prefetch_related(
            Prefetch('members', queryset=members),
            Prefetch('messages', queryset=Message.objects.exclude(hidden_for=me).order_by('-id')[:1], to_attr='latest'),
        )
        .order_by('-my_pinned', '-updated_at')
    )


def _message_qs(me):
    """Messages as `me` sees them: without ones I deleted for myself."""
    return Message.objects.exclude(hidden_for=me).select_related('reply_to').prefetch_related('reactions')


def _my_membership(request, pk):
    me = request.user.get_active_persona()
    return get_object_or_404(
        ConversationMember.objects.select_related('conversation'),
        conversation_id=pk, persona=me, left_at__isnull=True,
    )


def _int_param(request, name, default=None, maximum=None):
    try:
        value = int(request.query_params[name])
    except (KeyError, ValueError):
        return default
    return min(value, maximum) if maximum is not None else value


class MessageSendThrottle(UserRateThrottle):
    scope = 'messages'

    def allow_request(self, request, view):
        return request.method != 'POST' or super().allow_request(request, view)


@api_view(['GET', 'POST'])
def conversations(request):
    me = request.user.get_active_persona()
    ctx = {'request': request, 'persona': me}
    if request.method == 'GET':
        return Response(ConversationSerializer(_conversation_qs(me), many=True, context=ctx).data)

    # POST — get or create a 1:1 conversation between my active persona and another persona
    other = _visible_persona(request, request.data.get('persona_id'))
    if other is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    conv = (
        Conversation.objects
        .filter(participants=me)
        .filter(participants=other)
        .first()
    )
    created = conv is None
    if created:
        conv = Conversation.objects.create()
        conv.participants.add(me, other)

    return Response(
        ConversationSerializer(_conversation_qs(me).get(pk=conv.pk), context=ctx).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['GET', 'POST'])
@throttle_classes([MessageSendThrottle])
def conversation_messages(request, pk):
    member = _my_membership(request, pk)
    me, conv = member.persona, member.conversation

    if request.method == 'GET':
        # Cursor pagination, always returned oldest-first:
        #   ?before=<id>  older page (scrolling up)
        #   ?after=<id>   everything newer (catch-up after a reconnect)
        #   neither       the latest page — opening the chat marks it read
        #   ?q=<text>     search, newest-first
        #   ?around=<id>  a page centred on one message (jumping to a search hit)
        limit = max(_int_param(request, 'limit', 50, maximum=100), 1)
        before = _int_param(request, 'before')
        after = _int_param(request, 'after')
        around = _int_param(request, 'around')
        qs = _message_qs(me).filter(conversation=conv)
        query = (request.query_params.get('q') or '').strip()
        if query:
            hits = qs.filter(kind=Message.KIND_TEXT, deleted_at__isnull=True, body__icontains=query)
            return Response(MessageSerializer(hits.order_by('-id')[:limit], many=True).data)
        if around is not None:
            older = list(qs.filter(id__lte=around).order_by('-id')[:limit // 2 + 1])[::-1]
            newer = list(qs.filter(id__gt=around).order_by('id')[:limit // 2])
            return Response(MessageSerializer(older + newer, many=True).data)
        if after is not None:
            msgs = list(qs.filter(id__gt=after).order_by('id')[:limit])
        else:
            if before is not None:
                qs = qs.filter(id__lt=before)
            msgs = list(qs.order_by('-id')[:limit])[::-1]
        if msgs and before is None:
            mark_receipt(member, 'delivered' if after is not None else 'read', msgs[-1].id)
        if before is None and after is None:
            ConversationMember.objects.filter(pk=member.pk, marked_unread=True).update(marked_unread=False)
        return Response(MessageSerializer(msgs, many=True).data)

    # POST — send a message
    client_id = request.data.get('client_id')
    if client_id is not None:
        try:
            client_id = uuid.UUID(str(client_id))
        except ValueError:
            return Response({'error': 'Invalid client_id.'}, status=status.HTTP_400_BAD_REQUEST)
        existing = Message.objects.filter(sender=me, client_id=client_id).first()
        if existing is not None:
            return Response(MessageSerializer(existing).data, status=status.HTTP_200_OK)

    body = (request.data.get('body') or '').strip()
    if not body:
        return Response({'error': 'Message body is required.'}, status=status.HTTP_400_BAD_REQUEST)

    reply_to = None
    if request.data.get('reply_to_id') is not None:
        try:
            reply_to = conv.messages.get(pk=request.data.get('reply_to_id'))
        except (Message.DoesNotExist, TypeError, ValueError):
            return Response({'error': 'Unknown reply_to_id.'}, status=status.HTTP_400_BAD_REQUEST)

    other_users = (
        conv.members.filter(left_at__isnull=True).exclude(persona=me).values_list('persona__user_id', flat=True)
    )
    if Block.objects.filter(
        Q(blocker=request.user, blocked__in=other_users) | Q(blocker__in=other_users, blocked=request.user)
    ).exists():
        return Response({'error': 'You can no longer message this user.'}, status=status.HTTP_403_FORBIDDEN)

    metadata = {'forwarded': True} if request.data.get('forwarded') is True else {}
    try:
        with transaction.atomic():
            msg = Message.objects.create(
                conversation=conv, sender=me, body=body, client_id=client_id, reply_to=reply_to,
                metadata=metadata,
            )
    except IntegrityError:
        # A concurrent retry with the same client_id won the race.
        return Response(MessageSerializer(Message.objects.get(sender=me, client_id=client_id)).data)

    Conversation.objects.filter(pk=conv.pk).update(updated_at=timezone.now())
    # Your own messages are read by you; a new message brings the chat back out of the archive.
    ConversationMember.objects.filter(pk=member.pk).update(last_read_message=msg, last_delivered_message=msg)
    conv.members.exclude(pk=member.pk).filter(archived=True).update(archived=False)
    data = MessageSerializer(msg).data
    broadcast(conv.pk, {'type': 'message.new', 'conversation_id': conv.pk, 'message': data})
    notify_new_message(msg)
    return Response(data, status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
def conversation_detail(request, pk):
    """Update my own settings for a conversation: pinned, archived, muted_until, marked_unread."""
    member = _my_membership(request, pk)
    fields = []
    for name in ('pinned', 'archived', 'marked_unread'):
        if name in request.data:
            value = request.data[name]
            if not isinstance(value, bool):
                return Response({'error': f'{name} must be a boolean.'}, status=status.HTTP_400_BAD_REQUEST)
            setattr(member, name, value)
            fields.append(name)
    if 'muted_until' in request.data:
        raw = request.data['muted_until']
        muted_until = parse_datetime(raw) if isinstance(raw, str) else None
        if raw is not None and muted_until is None:
            return Response({'error': 'muted_until must be an ISO datetime or null.'}, status=status.HTTP_400_BAD_REQUEST)
        member.muted_until = muted_until
        fields.append('muted_until')
    if fields:
        member.save(update_fields=fields)
    ctx = {'request': request, 'persona': member.persona}
    return Response(ConversationSerializer(_conversation_qs(member.persona).get(pk=pk), context=ctx).data)


def _member_message(request, pk, message_id):
    member = _my_membership(request, pk)
    msg = get_object_or_404(_message_qs(member.persona), pk=message_id, conversation_id=pk)
    return member, msg


@api_view(['PATCH', 'DELETE'])
def message_detail(request, pk, message_id):
    """The sender edits (PATCH body) or unsends (DELETE) a message within Message.EDIT_WINDOW."""
    member, msg = _member_message(request, pk, message_id)
    if msg.sender_id != member.persona_id:
        return Response({'error': 'You can only change your own messages.'}, status=status.HTTP_403_FORBIDDEN)
    if msg.deleted_at is not None:
        return Response({'error': 'Message was deleted.'}, status=status.HTTP_400_BAD_REQUEST)
    if timezone.now() - msg.created_at > Message.EDIT_WINDOW:
        return Response({'error': 'Messages can only be changed for 15 minutes.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'DELETE':
        msg.deleted_at = timezone.now()
        msg.body = ''
        msg.metadata = {}
        msg.save(update_fields=['deleted_at', 'body', 'metadata'])
        msg.reactions.all().delete()
    else:
        body = (request.data.get('body') or '').strip()
        if msg.kind != Message.KIND_TEXT or not body or len(body) > 2000:
            return Response({'error': 'A text body of 1–2000 characters is required.'}, status=status.HTTP_400_BAD_REQUEST)
        msg.body = body
        msg.edited_at = timezone.now()
        msg.save(update_fields=['body', 'edited_at'])

    msg = _message_qs(member.persona).get(pk=msg.pk)
    data = MessageSerializer(msg).data
    broadcast(msg.conversation_id, {'type': 'message.updated', 'conversation_id': msg.conversation_id, 'message': data})
    return Response(data)


@api_view(['POST'])
def message_hide(request, pk, message_id):
    """Delete a message for me only."""
    member, msg = _member_message(request, pk, message_id)
    msg.hidden_for.add(member.persona)
    payload = {'type': 'message.hidden', 'conversation_id': msg.conversation_id, 'message_id': msg.pk}
    transaction.on_commit(lambda: send_to_users({request.user.pk}, payload))
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['PUT', 'DELETE'])
def message_reaction(request, pk, message_id):
    """Set (PUT {emoji}) or clear (DELETE) my reaction to a message."""
    member, msg = _member_message(request, pk, message_id)
    if msg.deleted_at is not None:
        return Response({'error': 'Message was deleted.'}, status=status.HTTP_400_BAD_REQUEST)
    emoji = None
    if request.method == 'PUT':
        emoji = request.data.get('emoji')
        if not isinstance(emoji, str) or not emoji.strip() or len(emoji) > 16 or any(c.isspace() for c in emoji):
            return Response({'error': 'emoji is required.'}, status=status.HTTP_400_BAD_REQUEST)
        MessageReaction.objects.update_or_create(message=msg, persona=member.persona, defaults={'emoji': emoji})
    else:
        MessageReaction.objects.filter(message=msg, persona=member.persona).delete()
    broadcast(msg.conversation_id, {
        'type': 'reaction', 'conversation_id': msg.conversation_id, 'message_id': msg.pk,
        'persona_id': member.persona_id, 'emoji': emoji,
    })
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET', 'POST'])
def phrases(request):
    if request.method == 'GET':
        return Response([{'id': p.id, 'text': p.text} for p in request.user.saved_phrases.all()])
    text = (request.data.get('text') or '').strip()
    if not text or len(text) > 300:
        return Response({'error': 'text must be 1–300 characters.'}, status=status.HTTP_400_BAD_REQUEST)
    if request.user.saved_phrases.count() >= settings.MAX_SAVED_PHRASES:
        return Response({'error': f'At most {settings.MAX_SAVED_PHRASES} saved phrases.'}, status=status.HTTP_400_BAD_REQUEST)
    phrase = SavedPhrase.objects.create(user=request.user, text=text)
    return Response({'id': phrase.id, 'text': phrase.text}, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
def phrase_delete(request, pk):
    request.user.saved_phrases.filter(pk=pk).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
def conversation_read(request, pk):
    """Mark the conversation read up to `message_id`."""
    member = _my_membership(request, pk)
    try:
        message_id = int(request.data.get('message_id'))
    except (TypeError, ValueError):
        return Response({'error': 'message_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
    mark_receipt(member, 'read', message_id)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST', 'DELETE'])
def devices(request):
    """Register (POST) or unregister (DELETE) this device's push token."""
    token = (request.data.get('token') or '').strip()
    if not token or len(token) > 512:
        return Response({'error': 'token is required.'}, status=status.HTTP_400_BAD_REQUEST)
    if request.method == 'DELETE':
        DeviceToken.objects.filter(user=request.user, token=token).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    platform = request.data.get('platform')
    if platform not in ('ios', 'android'):
        return Response({'error': 'platform must be ios or android.'}, status=status.HTTP_400_BAD_REQUEST)
    # A token belongs to one device; it moves to whoever signed in on it last.
    DeviceToken.objects.update_or_create(token=token, defaults={'user': request.user, 'platform': platform})
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
def nearby(request):
    try:
        lat = float(request.query_params["lat"])
        lng = float(request.query_params["lng"])
    except (KeyError, ValueError, TypeError):
        return Response({"error": "lat and lng are required."}, status=status.HTTP_400_BAD_REQUEST)
    label = nearby_label(lat, lng)
    return Response({"label": label})


# ── Safety ────────────────────────────────────────────────────────────────────

def _target_persona(request):
    """Another account's persona by `persona_id` — blocks/reports work even if a block already exists."""
    try:
        return (
            Persona.objects.select_related('user')
            .filter(user__is_active=True)
            .exclude(user=request.user)
            .get(pk=request.data.get('persona_id'))
        )
    except (Persona.DoesNotExist, TypeError, ValueError):
        return None


@api_view(["GET", "POST"])
def blocks(request):
    if request.method == "GET":
        qs = (
            Block.objects
            .filter(blocker=request.user)
            .select_related('blocked_persona__user', 'blocked_persona__user__verification')
            .prefetch_related(Prefetch('blocked_persona__photos', queryset=PersonaPhoto.objects.select_related('image')))
        )
        personas = [b.blocked_persona for b in qs if b.blocked_persona is not None]
        return Response(DiscoverPersonaSerializer(personas, many=True, context={'request': request}).data)

    target = _target_persona(request)
    if target is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    # Blocks are account-wide: every persona on both sides is hidden.
    Block.objects.update_or_create(
        blocker=request.user, blocked=target.user, defaults={'blocked_persona': target},
    )
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
def block_delete(request, pk):
    """`pk` is the blocked persona's id."""
    Block.objects.filter(blocker=request.user, blocked__personas__pk=pk).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET", "POST"])
def verification(request):
    try:
        record = request.user.verification
        status_val = record.status
    except FaceVerification.DoesNotExist:
        record = None
        status_val = None

    if request.method == "GET":
        return Response({"status": status_val})

    # POST — submit a selfie for verification
    if not request.user.has_tier(User.TIER_PLUS):
        return Response(
            {"error": "Identity verification requires a Plus subscription.",
             "requires_tier": User.TIER_PLUS},
            status=status.HTTP_403_FORBIDDEN,
        )

    if status_val == FaceVerification.STATUS_VERIFIED:
        return Response({"status": status_val})

    image, err = _validate_image(request)
    if err:
        return err
    selfie_bytes = image.read()

    face_images = list(request.user.gallery_images.filter(tag_face=True))
    if not face_images:
        face_images = list(request.user.gallery_images.all())
    from .face_verification import compare_selfie_to_gallery
    new_status = compare_selfie_to_gallery(selfie_bytes, face_images)

    if record is None:
        FaceVerification.objects.create(user=request.user, status=new_status)
    else:
        record.status = new_status
        record.save(update_fields=["status"])

    return Response({"status": new_status})


@api_view(["POST"])
def reports(request):
    reason = request.data.get('reason', '')
    details = request.data.get('details', '')
    target = _target_persona(request)
    if target is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    valid_reasons = [r[0] for r in Report.REASON_CHOICES]
    if reason not in valid_reasons:
        return Response(
            {'error': f'reason must be one of {valid_reasons}.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    message = None
    if request.data.get('message_id') is not None:
        # Evidence must be a message the reported persona sent to one of my personas.
        try:
            message = Message.objects.select_related('conversation').get(
                pk=request.data.get('message_id'), sender=target,
                conversation__members__persona__user=request.user,
            )
        except (Message.DoesNotExist, TypeError, ValueError):
            return Response({'error': 'Message not found.'}, status=status.HTTP_404_NOT_FOUND)
    Report.objects.create(
        reporter=request.user, reported=target.user, reported_persona=target,
        reason=reason, details=details,
        message=message, conversation=message.conversation if message else None,
    )
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
def kinks_list(request):
    kinks = (
        Kink.objects
        .filter(is_active=True)
        .select_related('parent')
        .order_by('parent__name', 'name')
    )
    data = [
        {'name': k.name, 'parent': k.parent.name if k.parent else None}
        for k in kinks
    ]
    return Response(data)


@api_view(['GET'])
def interests_list(request):
    interests = (
        Interest.objects
        .filter(is_active=True)
        .select_related('parent')
        .order_by('parent__name', 'name')
    )
    data = [
        {'name': i.name, 'parent': i.parent.name if i.parent else None}
        for i in interests
    ]
    return Response(data)
