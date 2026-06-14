import random as _random
import phonenumbers
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from .models import User, ProfileImage, Conversation, Message, ImageShareGrant, LocationShareGrant, FaceVerification, Interest


def normalize_phone(value: str) -> str:
    try:
        parsed = phonenumbers.parse(value, None)
        if not phonenumbers.is_valid_number(parsed):
            raise serializers.ValidationError("Invalid phone number.")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        raise serializers.ValidationError(
            "Invalid phone number format. Use E.164 format (e.g. +15551234567)."
        )


def _fuzz_coords(lat: float, lng: float, seed: int) -> tuple[float, float]:
    """Return a deterministically jittered position (~±450 m) for General-mode users."""
    rng = _random.Random(seed)
    return lat + rng.uniform(-0.004, 0.004), lng + rng.uniform(-0.004, 0.004)


_TAG_FIELDS = ('face', 'torso', 'explicit', 'other')


def _tag_q(tag: str) -> Q:
    return Q(**{f'tag_{tag}': True})


def _untagged_q() -> Q:
    return Q(tag_face=False, tag_torso=False, tag_explicit=False, tag_other=False)


def _public_filter(user) -> Q | None:
    """
    Returns a Q that matches images visible to the public, or None if nothing
    should be shown (share_images master toggle is off).

    An image is visible only when none of its active tags belong to a category
    the user hasn't opted into sharing.  This prevents multi-tagged images
    (e.g. face + explicit) from leaking through a category the viewer isn't
    allowed to see.
    """
    if not user.share_images:
        return None
    q = Q()
    if not user.share_face:     q &= Q(tag_face=False)
    if not user.share_torso:    q &= Q(tag_torso=False)
    if not user.share_explicit: q &= Q(tag_explicit=False)
    if not user.share_other:    q &= Q(tag_other=False)
    return q


def _viewer_filter(user, viewer) -> Q | None:
    """Public filter + any active (non-expired) categories the user has granted to viewer."""
    q = _public_filter(user)
    if viewer and viewer.pk != user.pk:
        now = timezone.now()
        grants = ImageShareGrant.objects.filter(
            grantor=user, grantee=viewer,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).values_list('category', flat=True)
        for cat in grants:
            extra = _tag_q(cat)
            q = extra if q is None else (q | extra)
    return q


class GalleryImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()

    class Meta:
        model = ProfileImage
        fields = ['id', 'url', 'order', 'tags']

    def get_url(self, obj):
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url

    def get_tags(self, obj):
        return obj.tags


def _first_gallery_url(obj, request, image_filter: Q | None = ...):
    """
    Return the URL of the first visible gallery image, or None.
    Pass image_filter=None explicitly to return the first image with no filtering.
    The default sentinel skips filtering entirely (used for the owner's own profile).
    """
    qs = obj.gallery_images.order_by('order', 'created_at')
    if image_filter is not ...:
        if image_filter is None:
            return None
        qs = qs.filter(image_filter)
    first = qs.first()
    if first is None:
        return None
    url = first.image.url
    return request.build_absolute_uri(url) if request else url


class ProfileSerializer(serializers.ModelSerializer):
    """Serializer for the logged-in user's own profile — shows all images unfiltered."""
    profile_picture = serializers.SerializerMethodField()
    gallery_images = GalleryImageSerializer(many=True, read_only=True)
    verification_status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "profile_picture",
            "gallery_images",
            "display_name",
            "bio",
            "pronouns",
            "age",
            "notifications_enabled",
            "intent",
            "looking_for_chat",
            "looking_for_cuddle",
            "looking_for_fool_around",
            "looking_for_sex",
            "location_sharing",
            "share_images",
            "share_face",
            "share_torso",
            "share_explicit",
            "share_other",
            "blur_explicit",
            "height",
            "length",
            "kinks",
            "share_kinks",
            "interests",
            "relationship_types",
            "position",
            "power_position",
            "can_host",
            "can_travel",
            "latitude",
            "longitude",
            "subscription_tier",
            "verification_status",
        ]

    def get_profile_picture(self, obj):
        return _first_gallery_url(obj, self.context.get('request'))

    def get_verification_status(self, obj):
        try:
            return obj.verification.status
        except FaceVerification.DoesNotExist:
            return None


class DiscoverUserSerializer(serializers.ModelSerializer):
    """Public-facing profile for the discover feed — images filtered by public settings."""
    profile_picture = serializers.SerializerMethodField()
    gallery_images = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    is_verified = serializers.SerializerMethodField()
    kinks = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "display_name",
            "age",
            "pronouns",
            "bio",
            "profile_picture",
            "gallery_images",
            "intent",
            "looking_for_chat",
            "looking_for_cuddle",
            "looking_for_fool_around",
            "looking_for_sex",
            "height",
            "length",
            "kinks",
            "interests",
            "relationship_types",
            "position",
            "power_position",
            "can_host",
            "can_travel",
            "location_sharing",
            "latitude",
            "longitude",
            "neighborhood",
            "is_verified",
        ]

    def get_is_verified(self, obj):
        try:
            return obj.verification.status == FaceVerification.STATUS_VERIFIED
        except FaceVerification.DoesNotExist:
            return False

    def get_kinks(self, obj):
        return obj.kinks if obj.share_kinks else []

    def get_profile_picture(self, obj):
        return _first_gallery_url(obj, self.context.get('request'), _public_filter(obj))

    def get_gallery_images(self, obj):
        f = _public_filter(obj)
        qs = obj.gallery_images.filter(f) if f is not None else obj.gallery_images.none()
        return GalleryImageSerializer(qs, many=True, context=self.context).data

    def get_latitude(self, obj):
        if obj.location_sharing == 2:
            return obj.latitude
        if obj.location_sharing == 1 and obj.latitude is not None:
            return _fuzz_coords(obj.latitude, obj.longitude, obj.pk)[0]
        return None

    def get_longitude(self, obj):
        if obj.location_sharing == 2:
            return obj.longitude
        if obj.location_sharing == 1 and obj.longitude is not None:
            return _fuzz_coords(obj.latitude, obj.longitude, obj.pk)[1]
        return None


class UserProfileSerializer(DiscoverUserSerializer):
    """Single-user profile view — adds grant-based images and location on top of public ones."""
    effective_location_sharing = serializers.SerializerMethodField()

    class Meta(DiscoverUserSerializer.Meta):
        fields = DiscoverUserSerializer.Meta.fields + ['effective_location_sharing']

    def get_effective_location_sharing(self, obj):
        grant_level = self._location_grant_level(obj)
        return max(obj.location_sharing, grant_level or 0)

    def get_profile_picture(self, obj):
        viewer = self.context['request'].user
        return _first_gallery_url(obj, self.context['request'], _viewer_filter(obj, viewer))

    def get_gallery_images(self, obj):
        viewer = self.context['request'].user
        f = _viewer_filter(obj, viewer)
        qs = obj.gallery_images.filter(f) if f is not None else obj.gallery_images.none()
        return GalleryImageSerializer(qs, many=True, context=self.context).data

    def _location_grant_level(self, obj):
        viewer = self.context['request'].user
        if viewer.pk == obj.pk:
            return None
        now = timezone.now()
        grant = LocationShareGrant.objects.filter(
            grantor=obj, grantee=viewer,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).first()
        return grant.level if grant else None

    def get_latitude(self, obj):
        grant_level = self._location_grant_level(obj)
        effective = max(obj.location_sharing, grant_level or 0)
        if effective == 2:
            return obj.latitude
        if effective == 1 and obj.latitude is not None:
            return _fuzz_coords(obj.latitude, obj.longitude, obj.pk)[0]
        return None

    def get_longitude(self, obj):
        grant_level = self._location_grant_level(obj)
        effective = max(obj.location_sharing, grant_level or 0)
        if effective == 2:
            return obj.longitude
        if effective == 1 and obj.longitude is not None:
            return _fuzz_coords(obj.latitude, obj.longitude, obj.pk)[1]
        return None


class ImageShareGrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageShareGrant
        fields = ['id', 'grantee_id', 'category', 'expires_at', 'created_at']
        read_only_fields = ['id', 'created_at']


class LocationShareGrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = LocationShareGrant
        fields = ['id', 'grantee_id', 'level', 'expires_at', 'created_at']
        read_only_fields = ['id', 'created_at']


class MessageSerializer(serializers.ModelSerializer):
    sender_id = serializers.IntegerField(source='sender.id', read_only=True)

    class Meta:
        model = Message
        fields = ['id', 'sender_id', 'body', 'created_at', 'is_read']
        read_only_fields = ['id', 'sender_id', 'created_at', 'is_read']


class ConversationSerializer(serializers.ModelSerializer):
    other_user = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ['id', 'other_user', 'last_message', 'unread_count', 'updated_at']

    def get_other_user(self, obj):
        request = self.context['request']
        other = next((p for p in obj.participants.all() if p.pk != request.user.pk), None)
        if other is None:
            return None
        return {
            'id': other.id,
            'display_name': other.display_name,
            'profile_picture': _first_gallery_url(other, request, _public_filter(other)),
        }

    def get_last_message(self, obj):
        if hasattr(obj, 'prefetched_messages'):
            msgs = obj.prefetched_messages
            msg = msgs[0] if msgs else None
        else:
            msg = obj.messages.order_by('-created_at').first()
        if msg is None:
            return None
        return {
            'body': msg.body,
            'sender_id': msg.sender_id,
            'created_at': msg.created_at.isoformat(),
        }

    def get_unread_count(self, obj):
        me = self.context['request'].user
        if hasattr(obj, 'prefetched_messages'):
            return sum(1 for m in obj.prefetched_messages if not m.is_read and m.sender_id != me.pk)
        return obj.messages.filter(is_read=False).exclude(sender=me).count()
