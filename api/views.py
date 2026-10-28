import io
import json
import logging
import random
import string
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

logger = logging.getLogger(__name__)

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from PIL import Image
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Case, Count, F, Max, OuterRef, Prefetch, Q, Subquery, When
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
from .models import User, Persona, PersonaMember, PersonaPhoto, DiscoverEvent, Friend, ProfileView, ProfileImage, Album, AlbumGrant, Conversation, ConversationMember, DeviceToken, Message, MessageAttachment, MessageReaction, SavedPhrase, LocationShareGrant, Block, Report, FaceVerification, Kink, Interest, PasskeyCredential, WebAuthnChallenge, OTPVerification, Lifeline
from .sms import SmsUnavailable, send_otp_sms, send_verify_sms, check_verify_sms
from .serializers import (
    AccountSerializer, PersonaSummarySerializer, PersonaSerializer,
    GalleryImageSerializer, DiscoverPersonaSerializer, PersonaProfileSerializer,
    AlbumSerializer, AlbumGrantSerializer, LocationShareGrantSerializer, ConversationSerializer, MessageSerializer,
    GroupMemberSerializer, InvitationSerializer, LifelineSerializer, persona_verified,
)
from . import areas, discover, geo
from .membership import (
    ACCEPTED, group_locked, in_use, in_use_q, is_editor, membership, owner_plus_q, persona_user_ids, personas_for,
)
from .geocoding import reverse_geocode, nearby_label
from .tagging import auto_tag
from .account import AccountDeletionError, delete_account
from .media import strip_image_metadata
from .lifeline import end as end_lifeline
from .push import notify_new_message, notify_persona_invite
from .realtime import broadcast, mark_receipt, member_user_ids, send_to_users
from django.conf import settings


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """Liveness for uptime monitors: 503 unless the database and cache both answer."""
    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"
    try:
        cache.set("health_check", 1, 10)
        checks["cache"] = "ok" if cache.get("health_check") == 1 else "error"
    except Exception:
        checks["cache"] = "error"
    healthy = all(v == "ok" for v in checks.values())
    return Response(
        {"status": "ok" if healthy else "error", **checks},
        status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class AuthThrottle(AnonRateThrottle):
    """Per-IP limit on the signed-out auth endpoints."""
    scope = 'auth'


class PhoneThrottle(SimpleRateThrottle):
    """Per-phone-number limit, so one number can't be flooded with codes or guesses."""

    def get_cache_key(self, request, view):
        phone = (request.data.get('phone_number') or '').strip()
        return self.cache_format % {'scope': self.scope, 'ident': phone} if phone else None


class OtpSendThrottle(PhoneThrottle):
    scope = 'otp_send'


class OtpVerifyThrottle(PhoneThrottle):
    scope = 'otp_verify'


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle])
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
@throttle_classes([AuthThrottle])
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
@throttle_classes([AuthThrottle])
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
@throttle_classes([AuthThrottle])
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
    """Raises SmsUnavailable if no code went out."""
    if settings.TWILIO_ENABLED:
        send_verify_sms(phone_number)
        return
    code = _generate_otp()
    with transaction.atomic():
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
@throttle_classes([AuthThrottle, OtpSendThrottle])
def otp_request(request):
    phone = (request.data.get('phone_number') or '').strip()
    if not phone:
        return Response({'detail': 'phone_number required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _create_and_send_otp(phone)
    except SmsUnavailable as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'detail': 'Code sent.'})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthThrottle, OtpVerifyThrottle])
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
@throttle_classes([OtpSendThrottle])
def link_phone_request(request):
    phone = (request.data.get('phone_number') or '').strip()
    if not phone:
        return Response({'detail': 'phone_number required.'}, status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(phone_number=phone).exclude(pk=request.user.pk).exists():
        return Response({'detail': 'Phone number already in use.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _create_and_send_otp(phone)
    except SmsUnavailable as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'detail': 'Code sent.'})


@api_view(["POST"])
@throttle_classes([OtpVerifyThrottle])
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


@api_view(["GET", "PATCH", "DELETE"])
def me(request):
    if request.method == "DELETE":
        try:
            delete_account(request.user)
        except AccountDeletionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(status=status.HTTP_204_NO_CONTENT)
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
        if user.latitude is not None and user.longitude is not None and areas.available():
            areas.assign(user)
            user.save(update_fields=areas.AREA_FIELDS)
        elif user.latitude is not None and user.longitude is not None:
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


def _group_members_prefetch():
    """Accepted group members with what's needed to show their shared info."""
    return Prefetch('group_members', queryset=PersonaMember.objects.filter(status=ACCEPTED).select_related(
        'user', 'user__verification', 'represent_persona',
    ).prefetch_related(Prefetch('represent_persona__photos', queryset=PersonaPhoto.objects.select_related('image'))))


def _persona_qs():
    return Persona.objects.select_related(
        'user', 'user__verification', 'last_active_user',
    ).prefetch_related(_photos_prefetch(), _group_members_prefetch())


def _hidden_persona_q(user):
    """
    Personas hidden from `user`: ones they can use themselves, ones behind an
    account block either way (any member of a group counts), and groups they blocked.
    """
    blocked_by_me, blocked_me = _block_exclusions(user)
    members = PersonaMember.objects.filter(status=ACCEPTED)
    # Demo accounts are only for testers (or everyone, during the beta).
    demo = Q() if user.is_tester or settings.DEMO_PROFILES_FOR_EVERYONE else Q(user__demo_handle__isnull=False)
    return (
        demo
        | Q(pk__in=personas_for(user).values('pk'))
        | Q(user__in=blocked_by_me) | Q(user__in=blocked_me)
        | Q(pk__in=members.filter(Q(user__in=blocked_by_me) | Q(user__in=blocked_me)).values('persona_id'))
        | Q(pk__in=Block.objects.filter(blocker=user, blocked__isnull=True).values('blocked_persona_id'))
    )


def _visible_persona(request, pk):
    """Another account's persona the caller may interact with (active account, no block either way)."""
    try:
        return _persona_qs().filter(user__is_active=True).exclude(_hidden_persona_q(request.user)).get(pk=pk)
    except (Persona.DoesNotExist, TypeError, ValueError):
        return None


def _my_persona(request, pk, editing=False):
    """A persona the caller can use (or, with `editing`, edit), else 404/403 response."""
    persona = get_object_or_404(
        personas_for(request.user).select_related('user').prefetch_related(_photos_prefetch(), _group_members_prefetch()),
        pk=pk,
    )
    if editing:
        if not is_editor(request.user, persona):
            return None, Response({'error': 'Only members allowed to edit can change this profile.'},
                                  status=status.HTTP_403_FORBIDDEN)
        if group_locked(persona):
            return None, Response({'error': 'This group profile is locked until its owner renews Plus.',
                                   'requires_tier': User.TIER_PLUS}, status=status.HTTP_403_FORBIDDEN)
    return persona, None


def _unread_counts(user) -> dict[int, int]:
    """Unread incoming messages per persona `user` can use."""
    counts = dict.fromkeys(personas_for(user).values_list('pk', flat=True), 0)
    rows = (
        ConversationMember.objects
        .filter(persona_id__in=list(counts), left_at__isnull=True)
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
    # Own personas in the user's order, then groups they've joined.
    qs = (
        personas_for(request.user).select_related('user')
        .prefetch_related(_photos_prefetch(), _group_members_prefetch())
        .order_by(Case(When(user=request.user, then=0), default=1), 'sort_order', 'created_at')
    )
    return Response(
        PersonaSummarySerializer(qs, many=True, context=_persona_ctx(request)).data,
        status=status_code,
    )


def _image_ids_param(request, max_count=None, also_allowed=()):
    """
    Validate `image_ids` as a list of the caller's library image ids (or ids in
    `also_allowed`). Returns (ids, error_response).
    """
    ids = request.data.get('image_ids')
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids) or len(set(ids)) != len(ids):
        return None, Response({'error': 'image_ids must be a list of unique integers.'}, status=status.HTTP_400_BAD_REQUEST)
    if max_count is not None and len(ids) > max_count:
        return None, Response({'error': f'At most {max_count} photos allowed.'}, status=status.HTTP_400_BAD_REQUEST)
    own = [i for i in ids if i not in also_allowed]
    if request.user.gallery_images.filter(pk__in=own).count() != len(own):
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

    group_type = request.data.get('group_type') or ''
    if group_type not in dict(Persona.GROUP_TYPE_CHOICES):
        return Response({'error': 'Unknown group_type.'}, status=status.HTTP_400_BAD_REQUEST)
    if group_type and not user.has_tier(User.TIER_PLUS):
        return Response(
            {'error': 'Group profiles require Plus.', 'requires_tier': User.TIER_PLUS},
            status=status.HTTP_403_FORBIDDEN,
        )

    source = None
    if request.data.get('duplicate_from') is not None:
        source = user.personas.filter(pk=request.data.get('duplicate_from')).first()
        if source is None:
            return Response({'error': 'Profile not found.'}, status=status.HTTP_404_NOT_FOUND)

    persona = Persona(user=user, sort_order=user.personas.count(), group_type=group_type)
    if source:
        for field in Persona.PROFILE_FIELDS:
            setattr(persona, field, getattr(source, field))
        persona.color_start, persona.color_end = source.color_start, source.color_end
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
        if group_type:
            PersonaMember.objects.create(
                persona=persona, user=user, role=PersonaMember.ROLE_OWNER, status=ACCEPTED,
                can_edit=True, responded_at=timezone.now(),
                represent_persona=user.personas.filter(is_primary=True).first(),
            )
    ctx['unlocked_ids'] = user.unlocked_persona_ids()
    return Response(PersonaSerializer(persona, context=ctx).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
def persona_detail(request, pk):
    user = request.user
    persona, err = _my_persona(request, pk, editing=request.method == 'PATCH')
    if err:
        return err
    ctx = _persona_ctx(request)

    if request.method == "GET":
        return Response(PersonaSerializer(persona, context=ctx).data)

    if request.method == "PATCH":
        data = request.data
        if persona.user_id != user.pk:
            # Display order is the owner's.
            data = {k: v for k, v in request.data.items() if k != 'sort_order'}
        serializer = PersonaSerializer(persona, data=data, partial=True, context=ctx)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    # DELETE
    if persona.user_id != user.pk:
        return Response({'error': 'Only the owner can delete a group profile. Leave it instead.'},
                        status=status.HTTP_403_FORBIDDEN)
    if persona.is_primary:
        return Response({'error': 'The primary profile cannot be deleted.'}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        if user.active_persona_id == persona.pk:
            primary = user.personas.get(is_primary=True)
            User.objects.filter(pk=user.pk).update(active_persona=primary)
        # Chats belong to the persona; remove them for both sides.
        Conversation.objects.filter(participants=persona).delete()
        Block.objects.filter(blocked__isnull=True, blocked_persona=persona).delete()
        member_ids = persona_user_ids([persona.pk]) - {user.pk}
        persona.delete()
    if member_ids:
        transaction.on_commit(lambda: send_to_users(member_ids, {'type': 'persona.members', 'persona_id': pk}))
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def persona_activate(request, pk):
    user = request.user
    persona = get_object_or_404(personas_for(user), pk=pk)
    if persona.pk not in user.unlocked_persona_ids():
        return Response(
            {'error': 'This profile is locked. Upgrade to use more profiles.',
             'requires_tier': User.TIER_PLUS},
            status=status.HTTP_403_FORBIDDEN,
        )
    Persona.objects.filter(pk=persona.pk).update(last_active_at=timezone.now(), last_active_user=user)
    User.objects.filter(pk=user.pk).update(active_persona=persona)
    user.active_persona = persona
    return _persona_list_response(request)


@api_view(["PUT"])
def persona_photos(request, pk):
    persona, err = _my_persona(request, pk, editing=True)
    if err:
        return err
    # Group editors add from their own library and may keep photos other members added.
    current = set(persona.photos.values_list('image_id', flat=True))
    ids, err = _image_ids_param(request, settings.MAX_GALLERY_IMAGES, also_allowed=current)
    if err:
        return err
    with transaction.atomic():
        persona.photos.all().delete()
        PersonaPhoto.objects.bulk_create(
            PersonaPhoto(persona=persona, image_id=image_id, order=i) for i, image_id in enumerate(ids)
        )
    persona, _ = _my_persona(request, pk)
    return Response(PersonaSerializer(persona, context=_persona_ctx(request)).data)


# ── Group personas ────────────────────────────────────────────────────────────

def _notify_group(persona_id, extra_user_ids=()):
    """Tell every member's devices (and `extra_user_ids`) to reload the group's members."""
    user_ids = persona_user_ids([persona_id]) | set(extra_user_ids)
    payload = {'type': 'persona.members', 'persona_id': persona_id}
    transaction.on_commit(lambda: send_to_users(user_ids, payload))


def _leave_group(member):
    """Remove `member` from their group and undo what their membership put there."""
    persona, user = member.persona, member.user
    with transaction.atomic():
        member.delete()
        # Photos from the member's own library leave with them.
        persona.photos.filter(image__user=user).delete()
        User.objects.filter(pk=user.pk, active_persona=persona).update(active_persona=None)
        Persona.objects.filter(pk=persona.pk, last_active_user=user).update(last_active_user=None)
    _notify_group(persona.pk, extra_user_ids=[user.pk])


@api_view(["GET", "POST"])
def persona_members(request, pk):
    persona, err = _my_persona(request, pk)
    if err:
        return err
    if not persona.group_type:
        return Response({'error': 'Not a group profile.'}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "GET":
        members = PersonaMember.objects.filter(persona=persona).all()
        if persona.user_id != request.user.pk:
            members = members.exclude(status=PersonaMember.STATUS_DECLINED)
        members = (
            members.select_related('user', 'represent_persona', 'invited_as')
            .prefetch_related(
                Prefetch('represent_persona__photos', queryset=PersonaPhoto.objects.select_related('image')),
                Prefetch('invited_as__photos', queryset=PersonaPhoto.objects.select_related('image')),
            )
        )
        return Response(GroupMemberSerializer(members, many=True, context={'request': request}).data)

    # POST — the owner invites the account behind a persona they can see.
    if persona.user_id != request.user.pk:
        return Response({'error': 'Only the owner can invite people.'}, status=status.HTTP_403_FORBIDDEN)
    locked = _locked_group_response(persona)
    if locked:
        return locked
    target = _visible_persona(request, request.data.get('persona_id'))
    if target is None or target.group_type:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    invitee = target.user
    member_ids = persona_user_ids([persona.pk])
    if Block.objects.filter(
        Q(blocker=invitee, blocked__in=member_ids) | Q(blocker__in=member_ids, blocked=invitee)
    ).exists():
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    existing = PersonaMember.objects.filter(persona=persona).filter(user=invitee).first()
    if existing is not None:
        message = {
            PersonaMember.STATUS_INVITED: 'Already invited.',
            PersonaMember.STATUS_ACCEPTED: 'Already a member.',
            PersonaMember.STATUS_DECLINED: 'They declined this invitation.',
        }[existing.status]
        return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)
    member = PersonaMember.objects.create(
        persona=persona, user=invitee, invited_by=request.user,
        invited_via=request.user.get_active_persona(), invited_as=target,
    )
    transaction.on_commit(lambda: send_to_users({invitee.pk}, {'type': 'persona.invite', 'invitation_id': member.pk}))
    notify_persona_invite(invitee.pk)
    _notify_group(persona.pk)
    return Response(GroupMemberSerializer(member, context={'request': request}).data, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def persona_member_detail(request, pk, member_id):
    persona, err = _my_persona(request, pk)
    if err:
        return err
    member = get_object_or_404(PersonaMember.objects.filter(persona=persona).select_related('user', 'persona'), pk=member_id)
    is_owner = persona.user_id == request.user.pk
    is_self = member.user_id == request.user.pk

    if request.method == "DELETE":
        if member.role == PersonaMember.ROLE_OWNER:
            return Response({'error': 'The owner can delete the group profile instead.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not (is_owner or is_self):
            return Response({'error': 'Only the owner can remove members.'}, status=status.HTTP_403_FORBIDDEN)
        _leave_group(member)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # PATCH — the owner sets edit rights; each member sets what of themself is shared.
    data = request.data
    fields = []
    if 'can_edit' in data:
        if not is_owner or member.role == PersonaMember.ROLE_OWNER:
            return Response({'error': 'Only the owner sets who can edit.'}, status=status.HTTP_403_FORBIDDEN)
        if not isinstance(data['can_edit'], bool):
            return Response({'error': 'can_edit must be a boolean.'}, status=status.HTTP_400_BAD_REQUEST)
        member.can_edit = data['can_edit']
        fields.append('can_edit')
    personal = [f for f in PersonaMember.SHARE_FIELDS + ['represent_persona_id'] if f in data]
    if personal and not is_self:
        return Response({'error': 'Only that member can change what they share.'}, status=status.HTTP_403_FORBIDDEN)
    err = _apply_personal(member, data)
    if err:
        return err
    member.save(update_fields=fields + [f.removesuffix('_id') for f in personal])
    _notify_group(persona.pk)
    return Response(GroupMemberSerializer(member, context={'request': request}).data)


def _apply_personal(member, data):
    """Set the member's own share flags / represent persona from `data`; returns an error response or None."""
    for name in PersonaMember.SHARE_FIELDS:
        if name in data:
            if not isinstance(data[name], bool):
                return Response({'error': f'{name} must be a boolean.'}, status=status.HTTP_400_BAD_REQUEST)
            setattr(member, name, data[name])
    if 'represent_persona_id' in data:
        rep_id = data['represent_persona_id']
        rep = None if rep_id is None else member.user.personas.filter(pk=rep_id, group_type='').first()
        if rep_id is not None and rep is None:
            return Response({'error': 'Choose one of your own profiles.'}, status=status.HTTP_400_BAD_REQUEST)
        member.represent_persona = rep
    return None


def _invitations_qs(user):
    return (
        PersonaMember.objects.filter(user=user, status=PersonaMember.STATUS_INVITED)
        .select_related('persona', 'invited_via')
        .prefetch_related(
            Prefetch('persona__photos', queryset=PersonaPhoto.objects.select_related('image')),
            Prefetch('invited_via__photos', queryset=PersonaPhoto.objects.select_related('image')),
            Prefetch('persona__group_members', queryset=PersonaMember.objects.filter(status=ACCEPTED)),
        )
    )


@api_view(["GET"])
def invitations(request):
    return Response(InvitationSerializer(_invitations_qs(request.user), many=True, context={'request': request}).data)


@api_view(["POST"])
def invitation_respond(request, pk, action):
    """Accept (with `represent_persona_id` and share flags) or decline. Any tier may join."""
    if action not in ('accept', 'decline'):
        return Response(status=status.HTTP_404_NOT_FOUND)
    member = get_object_or_404(_invitations_qs(request.user).select_related('user'), pk=pk)
    if action == 'decline':
        member.status = PersonaMember.STATUS_DECLINED
        member.responded_at = timezone.now()
        member.save(update_fields=['status', 'responded_at'])
        _notify_group(member.persona_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    member_ids = persona_user_ids([member.persona_id])
    if Block.objects.filter(
        Q(blocker=request.user, blocked__in=member_ids) | Q(blocker__in=member_ids, blocked=request.user)
    ).exists():
        return Response({'error': 'This invitation is no longer available.'}, status=status.HTTP_404_NOT_FOUND)
    err = _apply_personal(member, request.data)
    if err:
        return err
    member.status = ACCEPTED
    member.responded_at = timezone.now()
    member.save()
    _notify_group(member.persona_id)
    return _persona_list_response(request)


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
    # Group-only blocks have no user; a NULL here would make NOT IN exclude everything.
    blocked_by_me = Block.objects.filter(blocker=user, blocked__isnull=False).values_list('blocked_id', flat=True)
    blocked_me = Block.objects.filter(blocked=user).values_list('blocker_id', flat=True)
    return blocked_by_me, blocked_me


def _discoverable_qs(user):
    """Personas `user` may find: someone is using it; groups also need a Plus owner."""
    return (
        _persona_qs()
        .filter(in_use_q(), user__is_active=True)
        .filter(Q(group_type='') | owner_plus_q())
        .exclude(display_name='')
        .exclude(_hidden_persona_q(user))
    )


class DiscoverThrottle(UserRateThrottle):
    scope = 'discover'


def _feed_response(request, ranked, ids, token, offset):
    page = ranked[:discover.PAGE_SIZE]
    end = offset + discover.PAGE_SIZE
    ctx = {
        'request': request,
        'ranked': {r.persona.pk: r for r in page},
        'ranks': {pk: offset + i for i, pk in enumerate(ids[offset:end])},
    }
    return Response({
        'results': DiscoverPersonaSerializer([r.persona for r in page], many=True, context=ctx).data,
        'next_cursor': f'{token}:{end}' if end < len(ids) else None,
    })


@api_view(["GET"])
@throttle_classes([DiscoverThrottle])
def discover_feed(request):
    """
    The ranked feed, a page at a time. Without `cursor` (or with an expired one) it ranks
    afresh for `sort` / `q` and snapshots the order; with `cursor` it continues that snapshot.
    """
    user = request.user
    me = user.get_active_persona()
    filters = discover.effective_filters(me, user.has_tier(User.TIER_PLUS))
    snapshot = discover.load_snapshot(me.pk, request.query_params.get('cursor'))
    if snapshot is None:
        sort = request.query_params.get('sort')
        sort = sort if sort in discover.SORTS else 'recommended'
        qs = _discoverable_qs(user)
        query = (request.query_params.get('q') or '').strip()
        if query:
            qs = qs.filter(display_name__icontains=query)
        ranked = discover.rank(qs, user, me, sort=sort, filters=filters, verified=persona_verified)
        ids = [r.persona.pk for r in ranked]
        return _feed_response(request, ranked, ids, discover.save_snapshot(me.pk, ids), 0)

    token, ids, offset = snapshot
    page_ids = ids[offset:offset + discover.PAGE_SIZE]
    # Load the page fresh, dropping anyone blocked, hidden or gone quiet since the snapshot.
    fresh = {r.persona.pk: r for r in discover.rank(
        _discoverable_qs(user).filter(pk__in=page_ids), user, me, filters=filters, verified=persona_verified,
    )}
    return _feed_response(request, [fresh[pk] for pk in page_ids if pk in fresh], ids, token, offset)


@api_view(["GET"])
@throttle_classes([DiscoverThrottle])
def discover_map(request):
    """
    Best-ranked pins inside `bbox=min_lat,min_lng,max_lat,max_lng`, at the positions viewers may see.
    With `group=neighbourhood|locality|county`, General-location profiles come grouped under
    that area (or the nearest coarser one they have) instead of at fuzzed positions:
    {"pins": [...], "areas": [{id, kind, name, latitude, longitude, profiles: [...]}]}.
    """
    group = request.query_params.get('group')
    if group is not None and group not in areas.KINDS:
        return Response({'error': f'group must be one of {", ".join(areas.KINDS)}.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        bbox = [float(v) for v in request.query_params['bbox'].split(',')]
        if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError
    except (KeyError, ValueError):
        return Response({'error': 'bbox=min_lat,min_lng,max_lat,max_lng is required.'},
                        status=status.HTTP_400_BAD_REQUEST)
    user = request.user
    me = user.get_active_persona()
    filters = discover.effective_filters(me, user.has_tier(User.TIER_PLUS))
    use_areas = group is not None and areas.available()

    def area_of(persona, level):
        """The area a General-location persona is drawn under, if any."""
        return areas.area_for(geo.located_user(persona), group) if use_areas and level == 1 else None

    def shown_at(persona, level):
        # Grouped people stay while their area is on screen, wherever their fuzzed position falls.
        area = area_of(persona, level)
        return (area.lat, area.lng) if area else None

    ranked = discover.rank(_discoverable_qs(user), user, me, filters=filters, bbox=bbox,
                           verified=persona_verified, shown_at=shown_at)
    # Off-location personas have no map position to show.
    pins = [r for r in ranked if r.level >= 1][:discover.MAP_PIN_LIMIT]
    ctx = {'request': request, 'ranked': {r.persona.pk: r for r in pins}}
    if group is None:
        return Response(DiscoverPersonaSerializer([r.persona for r in pins], many=True, context=ctx).data)

    loose, grouped = [], {}   # grouped: Area → personas, best-ranked area first
    for r in pins:
        area = area_of(r.persona, r.level)
        if area is None:
            loose.append(r.persona)
        else:
            grouped.setdefault(area, []).append(r.persona)

    def serialize(personas):
        return DiscoverPersonaSerializer(personas, many=True, context=ctx).data

    out_areas = []
    for area, members in grouped.items():
        profiles = serialize(members)
        for p in profiles:
            # The area stands in for where they are.
            p['latitude'] = p['longitude'] = None
        out_areas.append({'id': area.id, 'kind': area.kind, 'name': area.name,
                          'latitude': area.lat, 'longitude': area.lng, 'profiles': profiles})
    return Response({'pins': serialize(loose), 'areas': out_areas})


@api_view(["GET"])
def profiles_list(request):
    """Unranked list for app versions before the discover feed; applies the same visibility windows."""
    now = timezone.now()
    qs = _discoverable_qs(request.user).filter(discover.recent_q(now))

    # Bounding-box filter: only return users within ~50 km of the viewer.
    # 1 degree of latitude ≈ 111 km; longitude varies but this is a fast approximation.
    try:
        viewer_lat = float(request.query_params['lat'])
        viewer_lng = float(request.query_params['lng'])
        radius_deg = 50.0 / 111.0  # ~0.45°
        # A group sits wherever the member who last used it is.
        qs = qs.annotate(
            lat=Coalesce('last_active_user__latitude', 'user__latitude'),
            lng=Coalesce('last_active_user__longitude', 'user__longitude'),
        ).filter(
            lat__range=(viewer_lat - radius_deg, viewer_lat + radius_deg),
            lng__range=(viewer_lng - radius_deg, viewer_lng + radius_deg),
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

    personas = [p for p in qs if discover.is_visible(p, now)]
    serializer = DiscoverPersonaSerializer(personas, many=True, context={'request': request})
    return Response(serializer.data)


def _log_discover_event(request, viewer, target, kind):
    """Record an action on a profile opened from the discover feed (the client passes its `rank`)."""
    params = {**request.query_params.dict(), **(request.data if isinstance(request.data, dict) else {})}
    try:
        rank = int(params.get('rank'))
    except (TypeError, ValueError):
        return
    if rank >= 0:
        DiscoverEvent.objects.create(
            viewer_persona=viewer, target_persona=target, kind=kind, rank=rank,
            sort=str(params.get('sort') or '')[:12],
        )


@api_view(["GET"])
def profile_detail(request, pk):
    persona = _visible_persona(request, pk)
    me = request.user.get_active_persona()
    # Non-active personas are only viewable by people already chatting with them,
    # so an account's other profiles can't be enumerated.
    if persona is not None and not in_use(persona) and not (
        Conversation.objects.filter(participants=persona).filter(participants=me).exists()
    ):
        persona = None
    if persona is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    _log_discover_event(request, me, persona, DiscoverEvent.KIND_OPENED)
    if me is not None:
        ProfileView.objects.update_or_create(viewer=me, viewed=persona, defaults={'viewed_at': timezone.now()})
    ctx = {'request': request, 'viewer_persona': me}
    return Response(PersonaProfileSerializer(persona, context=ctx).data)


@api_view(["POST", "DELETE"])
def profile_friend(request, pk):
    """Friend (or unfriend) a profile as the active persona."""
    me = request.user.get_active_persona()
    if request.method == "DELETE":
        Friend.objects.filter(persona=me, friend_id=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    target = _visible_persona(request, pk)
    if target is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    Friend.objects.get_or_create(persona=me, friend=target)
    return Response(status=status.HTTP_204_NO_CONTENT)


# How far back "who viewed you" looks.
PROFILE_VIEW_WINDOW = timedelta(days=7)


def _connected_ids(me):
    """Personas `me` has a two-way 1:1 chat with, mapped to when that chat last changed."""
    spoken = Message.objects.filter(deleted_at__isnull=True).exclude(kind='system')
    one_to_one = Conversation.objects.annotate(n=Count('members')).filter(n=2).values('pk')
    mine = spoken.filter(sender=me, conversation__in=one_to_one).values('conversation')
    replies = spoken.filter(conversation__in=mine).exclude(sender=me)
    return dict(replies.order_by().values_list('sender').annotate(at=Max('conversation__updated_at')))


def _connection_entries(request, me, stamps, serialize=True):
    """
    The personas in `stamps` (id → when) the caller may see and open, newest first.
    As with profile_detail, a persona nobody's using is listed only if it already chats with `me`.
    """
    chatting = ConversationMember.objects.filter(conversation__members__persona=me).values('persona_id')
    personas = sorted(
        _persona_qs().filter(pk__in=stamps, user__is_active=True)
        .exclude(_hidden_persona_q(request.user))
        .filter(in_use_q() | Q(pk__in=chatting)),
        key=lambda p: stamps[p.pk], reverse=True,
    )
    if not serialize:
        return personas
    data = DiscoverPersonaSerializer(personas, many=True, context={'request': request}).data
    return [{**d, 'at': stamps[p.pk].isoformat()} for p, d in zip(personas, data)]


@api_view(["GET"])
def connections(request):
    """The active persona's recent viewers (listed for Plus only; others get the count), friends, and connections."""
    me = request.user.get_active_persona()
    since = timezone.now() - PROFILE_VIEW_WINDOW
    views = dict(ProfileView.objects.filter(viewed=me, viewed_at__gte=since).values_list('viewer_id', 'viewed_at'))
    is_plus = request.user.has_tier(User.TIER_PLUS)
    viewers = _connection_entries(request, me, views, serialize=is_plus)
    return Response({
        'viewers': viewers if is_plus else None,
        'viewer_count': len(viewers),
        'friends': _connection_entries(request, me, dict(me.friends_added.values_list('friend_id', 'created_at'))),
        'connected': _connection_entries(request, me, _connected_ids(me)),
    })


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


# ── Lifeline ──────────────────────────────────────────────────────────────────

LIFELINE_MINUTES = (15, 12 * 60)
LIFELINE_SNOOZES = (15, 30, 60)
LIFELINE_MAX_RECIPIENTS = 10


def _open_lifeline(user):
    return Lifeline.objects.filter(user=user, status__in=Lifeline.OPEN).prefetch_related('recipients').first()


@api_view(['GET'])
def lifeline_current(request):
    """My open lifeline (or null), and who the last one went to, to prefill the picker."""
    lifeline = _open_lifeline(request.user)
    last = Lifeline.objects.filter(user=request.user).order_by('-started_at').first()
    return Response({
        'lifeline': LifelineSerializer(lifeline).data if lifeline else None,
        'last_recipient_ids': list(last.recipients.values_list('pk', flat=True)) if last else [],
    })


@api_view(['POST'])
def lifelines(request):
    """Start a lifeline from my active persona to people it already chats with."""
    me = request.user.get_active_persona()
    try:
        minutes = int(request.data.get('duration_minutes'))
        if not LIFELINE_MINUTES[0] <= minutes <= LIFELINE_MINUTES[1]:
            raise ValueError
    except (TypeError, ValueError):
        return Response({'error': 'duration_minutes must be between 15 and 720.'}, status=status.HTTP_400_BAD_REQUEST)

    ids = request.data.get('recipient_persona_ids')
    if not isinstance(ids, list) or not 1 <= len(set(ids)) <= LIFELINE_MAX_RECIPIENTS:
        return Response({'error': f'Pick 1 to {LIFELINE_MAX_RECIPIENTS} people.'}, status=status.HTTP_400_BAD_REQUEST)
    recipients = []
    for pk in set(ids):
        other = _visible_persona(request, pk)
        if other is None or not Conversation.objects.filter(participants=me).filter(participants=other).exists():
            return Response({'error': 'You can only pick people you chat with.'}, status=status.HTTP_400_BAD_REQUEST)
        recipients.append(other)

    try:
        with transaction.atomic():
            lifeline = Lifeline.objects.create(
                user=request.user, persona=me, expires_at=timezone.now() + timedelta(minutes=minutes),
            )
            lifeline.recipients.set(recipients)
    except IntegrityError:
        return Response({'error': 'You already have a lifeline running.'}, status=status.HTTP_409_CONFLICT)
    return Response(LifelineSerializer(_open_lifeline(request.user)).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
def lifeline_snooze(request, pk):
    try:
        minutes = int(request.data.get('minutes'))
        if minutes not in LIFELINE_SNOOZES:
            raise ValueError
    except (TypeError, ValueError):
        return Response({'error': 'minutes must be 15, 30 or 60.'}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        lifeline = get_object_or_404(Lifeline.objects.select_for_update(), pk=pk, user=request.user)
        if lifeline.status != Lifeline.STATUS_ACTIVE:
            return Response({'error': 'This lifeline is no longer running.'}, status=status.HTTP_409_CONFLICT)
        lifeline.expires_at = min(
            max(lifeline.expires_at, timezone.now()) + timedelta(minutes=minutes),
            lifeline.started_at + Lifeline.MAX_DURATION,
        )
        lifeline.warned_at = None
        lifeline.save(update_fields=['expires_at', 'warned_at'])
    return Response(LifelineSerializer(_open_lifeline(request.user)).data)


@api_view(['POST'])
def lifeline_end(request, pk):
    """I'm home or safe. After an alert this also tells the recipients."""
    lifeline = get_object_or_404(Lifeline, pk=pk, user=request.user)
    reason = request.data.get('reason', 'manual')
    if reason not in dict(Lifeline.END_REASON_CHOICES):
        return Response({'error': 'reason must be manual or arrived.'}, status=status.HTTP_400_BAD_REQUEST)
    lifeline = end_lifeline(lifeline, reason)
    return Response(LifelineSerializer(lifeline).data)


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
    members = ConversationMember.objects.select_related('persona__user', 'persona__last_active_user').prefetch_related(
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
    return (
        Message.objects.exclude(hidden_for=me)
        .select_related('reply_to', 'sender')
        .prefetch_related('reactions', 'attachments')
    )


def _my_membership(request, pk):
    me = request.user.get_active_persona()
    return get_object_or_404(
        ConversationMember.objects.select_related('conversation'),
        conversation_id=pk, persona=me, left_at__isnull=True,
    )


def _locked_group_response(persona):
    if group_locked(persona):
        return Response({'error': 'This group profile is locked until its owner renews Plus.',
                         'requires_tier': User.TIER_PLUS}, status=status.HTTP_403_FORBIDDEN)
    return None


def _broadcast_message(event_type, msg):
    """Broadcast a message event. Only a sending group's own members learn which of them sent it."""
    def payload(context=None):
        data = MessageSerializer(msg, context=context or {}).data
        return {'type': event_type, 'conversation_id': msg.conversation_id, 'message': data}

    if not msg.sender.group_type:
        broadcast(msg.conversation_id, payload())
        return
    insiders = persona_user_ids([msg.sender_id])
    everyone = member_user_ids(msg.conversation_id)
    inside, outside = payload({'viewer_persona_id': msg.sender_id}), payload()

    def send():
        send_to_users(everyone & insiders, inside)
        send_to_users(everyone - insiders, outside)
    transaction.on_commit(send)


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
    locked = _locked_group_response(me)
    if locked:
        return locked
    other = _visible_persona(request, request.data.get('persona_id'))
    if other is None:
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    conv, created = Conversation.get_or_create_between(me, other)
    if created:
        _log_discover_event(request, me, other, DiscoverEvent.KIND_MESSAGED)

    return Response(
        ConversationSerializer(_conversation_qs(me).get(pk=conv.pk), context=ctx).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['GET', 'POST'])
@throttle_classes([MessageSendThrottle])
def conversation_messages(request, pk):
    member = _my_membership(request, pk)
    me, conv = member.persona, member.conversation
    ctx = {'viewer_persona_id': me.pk}

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
            return Response(MessageSerializer(hits.order_by('-id')[:limit], many=True, context=ctx).data)
        if around is not None:
            older = list(qs.filter(id__lte=around).order_by('-id')[:limit // 2 + 1])[::-1]
            newer = list(qs.filter(id__gt=around).order_by('id')[:limit // 2])
            return Response(MessageSerializer(older + newer, many=True, context=ctx).data)
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
        return Response(MessageSerializer(msgs, many=True, context=ctx).data)

    # POST — send a message
    locked = _locked_group_response(me)
    if locked:
        return locked
    client_id = request.data.get('client_id')
    if client_id is not None:
        try:
            client_id = uuid.UUID(str(client_id))
        except ValueError:
            return Response({'error': 'Invalid client_id.'}, status=status.HTTP_400_BAD_REQUEST)
        existing = Message.objects.filter(sender=me, client_id=client_id).first()
        if existing is not None:
            return Response(MessageSerializer(existing, context=ctx).data, status=status.HTTP_200_OK)

    kind = request.data.get('kind') or Message.KIND_TEXT
    if kind not in (Message.KIND_TEXT, Message.KIND_IMAGE):
        return Response({'error': 'Unsupported message kind.'}, status=status.HTTP_400_BAD_REQUEST)
    body = (request.data.get('body') or '').strip()
    if kind == Message.KIND_TEXT and not body:
        return Response({'error': 'Message body is required.'}, status=status.HTTP_400_BAD_REQUEST)
    view_once = kind == Message.KIND_IMAGE and str(request.data.get('view_once')).lower() == 'true'

    reply_to = None
    if request.data.get('reply_to_id') is not None:
        try:
            reply_to = conv.messages.get(pk=request.data.get('reply_to_id'))
        except (Message.DoesNotExist, TypeError, ValueError):
            return Response({'error': 'Unknown reply_to_id.'}, status=status.HTTP_400_BAD_REQUEST)

    others = list(conv.members.filter(left_at__isnull=True).exclude(persona=me).values_list('persona_id', flat=True))
    other_users = persona_user_ids(others)
    if Block.objects.filter(
        Q(blocker=request.user, blocked__in=other_users) | Q(blocker__in=other_users, blocked=request.user)
        | Q(blocker=request.user, blocked__isnull=True, blocked_persona__in=others)
    ).exists():
        return Response({'error': 'You can no longer message this user.'}, status=status.HTTP_403_FORBIDDEN)

    images = []
    if kind == Message.KIND_IMAGE:
        images, err = _chat_images(request)
        if err:
            return err
        if view_once and len(images) != 1:
            return Response({'error': 'View-once messages hold a single photo.'}, status=status.HTTP_400_BAD_REQUEST)

    metadata = {'forwarded': True} if request.data.get('forwarded') is True else {}
    try:
        with transaction.atomic():
            msg = Message.objects.create(
                conversation=conv, sender=me, sender_user=request.user, kind=kind, body=body,
                client_id=client_id, reply_to=reply_to, metadata=metadata,
            )
            for order, image in enumerate(images):
                MessageAttachment.objects.create(message=msg, order=order, view_once=view_once, **image)
    except IntegrityError:
        # A concurrent retry with the same client_id won the race.
        return Response(MessageSerializer(Message.objects.get(sender=me, client_id=client_id), context=ctx).data)

    Conversation.objects.filter(pk=conv.pk).update(updated_at=timezone.now())
    # Your own messages are read by you; a new message brings the chat back out of the archive.
    ConversationMember.objects.filter(pk=member.pk).update(last_read_message=msg, last_delivered_message=msg)
    conv.members.exclude(pk=member.pk).filter(archived=True).update(archived=False)
    msg = _message_qs(me).get(pk=msg.pk)
    _broadcast_message('message.new', msg)
    notify_new_message(msg)
    return Response(MessageSerializer(msg, context=ctx).data, status=status.HTTP_201_CREATED)


def _chat_images(request):
    """
    Validate and prepare the `files` of an image message: metadata stripped,
    dimensions read and explicit content tagged before anyone can see them.
    Returns (list of MessageAttachment field dicts, error_response).
    """
    files = request.FILES.getlist('files')
    if not 1 <= len(files) <= settings.MAX_CHAT_IMAGES:
        return None, Response(
            {'error': f'Send 1–{settings.MAX_CHAT_IMAGES} photos.'}, status=status.HTTP_400_BAD_REQUEST,
        )
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    prepared = []
    for upload in files:
        if upload.size > max_bytes:
            return None, Response(
                {'error': f'Photos must be under {settings.MAX_UPLOAD_SIZE_MB}MB.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        clean = strip_image_metadata(upload)
        if clean is None:
            return None, Response({'error': 'Unsupported image format.'}, status=status.HTTP_400_BAD_REQUEST)
        data = clean.read()
        clean.seek(0)
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            mime = Image.MIME.get(img.format, 'image/jpeg')
        prepared.append({'file': clean, 'mime': mime, 'size': len(data), 'width': width, 'height': height})
    # Tag in parallel; Rekognition is a network call per photo.
    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        tags = list(pool.map(lambda p: auto_tag(p['file'].read()), prepared))
    for image, tag in zip(prepared, tags):
        image['file'].seek(0)
        image['tag_explicit'] = tag.get('tag_explicit', False)
    return prepared, None


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
        for attachment in msg.attachments.all():
            attachment.file.delete(save=False)
        msg.attachments.all().delete()
    else:
        body = (request.data.get('body') or '').strip()
        if msg.kind != Message.KIND_TEXT or not body or len(body) > 2000:
            return Response({'error': 'A text body of 1–2000 characters is required.'}, status=status.HTTP_400_BAD_REQUEST)
        msg.body = body
        msg.edited_at = timezone.now()
        msg.save(update_fields=['body', 'edited_at'])

    msg = _message_qs(member.persona).get(pk=msg.pk)
    _broadcast_message('message.updated', msg)
    return Response(MessageSerializer(msg, context={'viewer_persona_id': member.persona_id}).data)


@api_view(['POST'])
def attachment_view(request, pk, message_id, attachment_id):
    """
    Open a view-once photo: returns the image bytes once, then deletes the
    file so no URL to it ever exists. The sender can't open it.
    """
    member, msg = _member_message(request, pk, message_id)
    attachment = get_object_or_404(msg.attachments, pk=attachment_id, view_once=True)
    if msg.sender_id == member.persona_id:
        return Response({'error': 'You sent this photo.'}, status=status.HTTP_403_FORBIDDEN)
    # Claim the single view atomically so concurrent requests can't both succeed.
    claimed = MessageAttachment.objects.filter(pk=attachment.pk, viewed_at__isnull=True).update(
        viewed_at=timezone.now(),
    )
    if not claimed or not attachment.file:
        return Response({'error': 'This photo was already opened.'}, status=status.HTTP_410_GONE)
    with attachment.file.open('rb') as f:
        data = f.read()
    attachment.file.delete(save=False)
    MessageAttachment.objects.filter(pk=attachment.pk).update(file='')
    _broadcast_message('message.updated', _message_qs(member.persona).get(pk=msg.pk))
    return HttpResponse(data, content_type=attachment.mime)


@api_view(['POST'])
def message_hide(request, pk, message_id):
    """Delete a message for me only (for a group persona, for the whole group)."""
    member, msg = _member_message(request, pk, message_id)
    msg.hidden_for.add(member.persona)
    payload = {'type': 'message.hidden', 'conversation_id': msg.conversation_id, 'message_id': msg.pk}
    user_ids = persona_user_ids([member.persona_id])
    transaction.on_commit(lambda: send_to_users(user_ids, payload))
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
            .exclude(pk__in=personas_for(request.user).values('pk'))
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
    if target.group_type:
        # Hides just this group; its members' own profiles stay visible.
        Block.objects.get_or_create(blocker=request.user, blocked=None, blocked_persona=target)
    else:
        # Blocks are account-wide: every persona on both sides is hidden.
        Block.objects.update_or_create(
            blocker=request.user, blocked=target.user, defaults={'blocked_persona': target},
        )
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
def block_delete(request, pk):
    """`pk` is the blocked persona's id."""
    Block.objects.filter(
        Q(blocked__personas__pk=pk) | Q(blocked__isnull=True, blocked_persona_id=pk), blocker=request.user,
    ).delete()
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
            message = (
                Message.objects.select_related('conversation')
                .filter(conversation__members__persona__in=personas_for(request.user)).distinct()
                .get(pk=request.data.get('message_id'), sender=target)
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
