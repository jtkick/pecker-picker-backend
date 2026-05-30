import random
import string
import threading
from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.db.models import Prefetch

from rest_framework_simplejwt.exceptions import TokenError
from .models import OTPVerification, User, ProfileImage, Conversation, Message, ImageShareGrant, LocationShareGrant, Block, Report, FaceVerification, Kink, Interest
from .serializers import (
    RequestOTPSerializer, VerifyOTPSerializer, ProfileSerializer,
    GalleryImageSerializer, DiscoverUserSerializer, UserProfileSerializer,
    ImageShareGrantSerializer, LocationShareGrantSerializer, ConversationSerializer, MessageSerializer,
)
from .geocoding import reverse_geocode, nearby_label
from .sms import send_otp_sms
from .tagging import auto_tag
from django.conf import settings


def _generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


@api_view(["POST"])
@permission_classes([AllowAny])
def request_otp(request):
    serializer = RequestOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    phone_number = serializer.validated_data["phone_number"]

    # Invalidate any pending OTPs for this number
    OTPVerification.objects.filter(phone_number=phone_number, is_used=False).update(is_used=True)

    code = _generate_otp()
    OTPVerification.objects.create(
        phone_number=phone_number,
        code=code,
        expires_at=timezone.now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
    )

    send_otp_sms(phone_number, code)

    return Response({"detail": "Verification code sent."}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_otp(request):
    serializer = VerifyOTPSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    phone_number = serializer.validated_data["phone_number"]
    code = serializer.validated_data["code"]

    try:
        otp = OTPVerification.objects.filter(
            phone_number=phone_number,
            code=code,
            is_used=False,
        ).latest("created_at")
    except OTPVerification.DoesNotExist:
        return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)

    if not otp.is_valid():
        return Response({"detail": "Code has expired."}, status=status.HTTP_400_BAD_REQUEST)

    otp.is_used = True
    otp.save()

    user, is_new = User.objects.get_or_create(phone_number=phone_number)

    refresh = RefreshToken.for_user(user)
    return Response(
        {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "is_new_user": is_new,
        },
        status=status.HTTP_201_CREATED if is_new else status.HTTP_200_OK,
    )


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


@api_view(["GET", "PATCH"])
def me(request):
    ctx = {'request': request}
    if request.method == "GET":
        return Response(ProfileSerializer(request.user, context=ctx).data)
    serializer = ProfileSerializer(request.user, data=request.data, partial=True, context=ctx)
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


@api_view(["POST"])
def gallery_add(request):
    if request.user.gallery_images.count() >= settings.MAX_GALLERY_IMAGES:
        return Response(
            {'detail': f'Maximum {settings.MAX_GALLERY_IMAGES} images allowed.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    image, err = _validate_image(request)
    if err:
        return err

    # Read bytes before the storage backend moves the file pointer.
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


def _block_exclusions(user):
    """Returns two querysets of IDs to exclude: users I blocked and users who blocked me."""
    blocked_by_me = Block.objects.filter(blocker=user).values_list('blocked_id', flat=True)
    blocked_me = Block.objects.filter(blocked=user).values_list('blocker_id', flat=True)
    return blocked_by_me, blocked_me


@api_view(["GET"])
def users_list(request):
    blocked_by_me, blocked_me = _block_exclusions(request.user)
    qs = (
        User.objects
        .exclude(pk=request.user.pk)
        .filter(is_active=True)
        .exclude(display_name='')
        .exclude(pk__in=blocked_by_me)
        .exclude(pk__in=blocked_me)
        .select_related('verification')
        .prefetch_related('gallery_images')
    )

    # Bounding-box filter: only return users within ~50 km of the viewer.
    # 1 degree of latitude ≈ 111 km; longitude varies but this is a fast approximation.
    try:
        viewer_lat = float(request.query_params['lat'])
        viewer_lng = float(request.query_params['lng'])
        radius_deg = 50.0 / 111.0  # ~0.45°
        qs = qs.filter(
            latitude__isnull=False,
            longitude__isnull=False,
            latitude__range=(viewer_lat - radius_deg, viewer_lat + radius_deg),
            longitude__range=(viewer_lng - radius_deg, viewer_lng + radius_deg),
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

    serializer = DiscoverUserSerializer(qs, many=True, context={'request': request})
    return Response(serializer.data)


@api_view(['GET', 'POST'])
def conversations(request):
    if request.method == 'GET':
        qs = (
            Conversation.objects
            .filter(participants=request.user)
            .prefetch_related(
                Prefetch('participants', queryset=User.objects.prefetch_related('gallery_images')),
                Prefetch('messages', queryset=Message.objects.order_by('-created_at'), to_attr='prefetched_messages'),
            )
            .order_by('-updated_at')
        )
        return Response(ConversationSerializer(qs, many=True, context={'request': request}).data)

    # POST — get or create a 1:1 conversation with another user
    other_id = request.data.get('user_id')
    try:
        other = User.objects.get(pk=other_id, is_active=True)
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    if other == request.user:
        return Response({'error': 'Cannot message yourself.'}, status=status.HTTP_400_BAD_REQUEST)

    conv = (
        Conversation.objects
        .filter(participants=request.user)
        .filter(participants=other)
        .first()
    )
    created = conv is None
    if created:
        conv = Conversation.objects.create()
        conv.participants.add(request.user, other)

    return Response(
        ConversationSerializer(conv, context={'request': request}).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['GET', 'POST'])
def conversation_messages(request, pk):
    conv = get_object_or_404(Conversation, pk=pk, participants=request.user)

    if request.method == 'GET':
        # Mark all incoming messages as read.
        conv.messages.filter(is_read=False).exclude(sender=request.user).update(is_read=True)
        msgs = conv.messages.select_related('sender').all()
        return Response(MessageSerializer(msgs, many=True).data)

    # POST — send a message
    body = (request.data.get('body') or '').strip()
    if not body:
        return Response({'error': 'Message body is required.'}, status=status.HTTP_400_BAD_REQUEST)

    msg = Message.objects.create(conversation=conv, sender=request.user, body=body)
    Conversation.objects.filter(pk=conv.pk).update(updated_at=timezone.now())
    return Response(MessageSerializer(msg).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def nearby(request):
    try:
        lat = float(request.query_params["lat"])
        lng = float(request.query_params["lng"])
    except (KeyError, ValueError, TypeError):
        return Response({"error": "lat and lng are required."}, status=status.HTTP_400_BAD_REQUEST)
    label = nearby_label(lat, lng)
    return Response({"label": label})


@api_view(["GET"])
def user_detail(request, pk):
    user = get_object_or_404(User.objects.select_related('verification'), pk=pk, is_active=True)
    if Block.objects.filter(blocker=request.user, blocked=user).exists() or \
            Block.objects.filter(blocker=user, blocked=request.user).exists():
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(UserProfileSerializer(user, context={'request': request}).data)


@api_view(["GET", "POST"])
def image_grants(request):
    if request.method == "GET":
        qs = request.user.image_grants_given.all()
        grantee_id = request.query_params.get('grantee_id')
        if grantee_id:
            qs = qs.filter(grantee_id=grantee_id)
        return Response(ImageShareGrantSerializer(qs, many=True).data)

    # POST — create a grant
    category = request.data.get('category', '')
    valid_categories = [c for c, _ in ImageShareGrant.CATEGORY_CHOICES]
    if category not in valid_categories:
        return Response({'error': 'Invalid category.'}, status=status.HTTP_400_BAD_REQUEST)

    grantee_id = request.data.get('grantee_id')
    try:
        grantee = User.objects.get(pk=grantee_id, is_active=True)
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    if grantee == request.user:
        return Response({'error': 'Cannot grant to yourself.'}, status=status.HTTP_400_BAD_REQUEST)

    expires_at = request.data.get('expires_at')

    grant, created = ImageShareGrant.objects.get_or_create(
        grantor=request.user, grantee=grantee, category=category,
    )
    if expires_at is not None or created:
        grant.expires_at = expires_at
        grant.save(update_fields=['expires_at'])
    return Response(ImageShareGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def image_grant_delete(request, pk):
    try:
        grant = request.user.image_grants_given.get(pk=pk)
    except ImageShareGrant.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    grant.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
def gallery_delete(request, pk):
    try:
        obj = request.user.gallery_images.get(pk=pk)
    except ProfileImage.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    obj.image.delete(save=False)
    obj.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET", "POST"])
def location_grants(request):
    if request.method == "GET":
        qs = request.user.location_grants_given.all()
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

    grantee_id = request.data.get('grantee_id')
    try:
        grantee = User.objects.get(pk=grantee_id, is_active=True)
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    if grantee == request.user:
        return Response({'error': 'Cannot grant to yourself.'}, status=status.HTTP_400_BAD_REQUEST)

    level = request.data.get('level', 2)
    try:
        level = int(level)
        if level not in (1, 2):
            raise ValueError
    except (ValueError, TypeError):
        return Response({'error': 'level must be 1 or 2.'}, status=status.HTTP_400_BAD_REQUEST)

    expires_at = request.data.get('expires_at')

    grant, created = LocationShareGrant.objects.get_or_create(
        grantor=request.user, grantee=grantee,
        defaults={'level': level, 'expires_at': expires_at},
    )
    if not created:
        grant.level = level
        grant.expires_at = expires_at
        grant.save(update_fields=['level', 'expires_at'])
    return Response(LocationShareGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def location_grant_delete(request, pk):
    try:
        grant = request.user.location_grants_given.get(pk=pk)
    except LocationShareGrant.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    grant.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET", "POST"])
def blocks(request):
    if request.method == "GET":
        qs = (
            Block.objects
            .filter(blocker=request.user)
            .select_related('blocked')
            .prefetch_related('blocked__gallery_images')
        )
        users = [b.blocked for b in qs]
        return Response(DiscoverUserSerializer(users, many=True, context={'request': request}).data)

    user_id = request.data.get('user_id')
    try:
        target = User.objects.get(pk=user_id, is_active=True)
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    if target == request.user:
        return Response({'error': 'Cannot block yourself.'}, status=status.HTTP_400_BAD_REQUEST)
    Block.objects.get_or_create(blocker=request.user, blocked=target)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
def block_delete(request, pk):
    Block.objects.filter(blocker=request.user, blocked_id=pk).delete()
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
    user_id = request.data.get('user_id')
    reason = request.data.get('reason', '')
    details = request.data.get('details', '')
    try:
        target = User.objects.get(pk=user_id, is_active=True)
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
    if target == request.user:
        return Response({'error': 'Cannot report yourself.'}, status=status.HTTP_400_BAD_REQUEST)
    valid_reasons = [r[0] for r in Report.REASON_CHOICES]
    if reason not in valid_reasons:
        return Response(
            {'error': f'reason must be one of {valid_reasons}.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    Report.objects.create(reporter=request.user, reported=target, reason=reason, details=details)
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
