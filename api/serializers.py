import random as _random
import phonenumbers
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from .models import User, Persona, ProfileImage, Album, AlbumGrant, Conversation, Message, LocationShareGrant, FaceVerification


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


def _image_url(image: ProfileImage, request) -> str:
    url = image.image.url
    return request.build_absolute_uri(url) if request else url


def _public_images(persona) -> list[ProfileImage]:
    """The persona's public photos in display order (uses prefetched `photos` when present)."""
    cache = getattr(persona, '_prefetched_objects_cache', {})
    photos = cache['photos'] if 'photos' in cache else persona.photos.select_related('image')
    return [pp.image for pp in photos]


def _granted_images(persona, viewer_persona) -> list[ProfileImage]:
    """Images in albums `persona` has actively granted to `viewer_persona`, excluding public ones."""
    if viewer_persona is None or viewer_persona.pk == persona.pk:
        return []
    now = timezone.now()
    album_ids = AlbumGrant.objects.filter(
        grantor=persona, grantee=viewer_persona,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).values_list('album_id', flat=True)
    public_ids = {img.pk for img in _public_images(persona)}
    return list(
        ProfileImage.objects
        .filter(albums__in=album_ids)
        .exclude(pk__in=public_ids)
        .distinct()
    )


def _first_photo_url(persona, request) -> str | None:
    images = _public_images(persona)
    return _image_url(images[0], request) if images else None


def _is_verified(user) -> bool:
    try:
        return user.verification.status == FaceVerification.STATUS_VERIFIED
    except FaceVerification.DoesNotExist:
        return False


def _location_at_level(persona, level: int, coord: int) -> float | None:
    """coord 0 = latitude, 1 = longitude. Fuzz is seeded per persona so profiles don't share offsets."""
    user = persona.user
    if user.latitude is None or user.longitude is None:
        return None
    if level == 2:
        return (user.latitude, user.longitude)[coord]
    if level == 1:
        return _fuzz_coords(user.latitude, user.longitude, persona.pk)[coord]
    return None


class GalleryImageSerializer(serializers.ModelSerializer):
    """A library image."""
    url = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()

    class Meta:
        model = ProfileImage
        fields = ['id', 'url', 'order', 'tags']

    def get_url(self, obj):
        return _image_url(obj, self.context.get('request'))

    def get_tags(self, obj):
        return obj.tags


class AccountSerializer(serializers.ModelSerializer):
    """Account-wide settings for the logged-in user."""
    verification_status = serializers.SerializerMethodField()
    persona_limit = serializers.SerializerMethodField()
    library_limit = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "notifications_enabled",
            "blur_explicit",
            "latitude",
            "longitude",
            "subscription_tier",
            "verification_status",
            "active_persona_id",
            "persona_limit",
            "library_limit",
        ]
        read_only_fields = ["subscription_tier", "active_persona_id"]

    def get_verification_status(self, obj):
        try:
            return obj.verification.status
        except FaceVerification.DoesNotExist:
            return None

    def get_persona_limit(self, obj):
        return obj.persona_limit()

    def get_library_limit(self, obj):
        return obj.library_limit()


class PersonaSummarySerializer(serializers.ModelSerializer):
    """Compact entry for the persona switcher. Context: `unlocked_ids`, optional `unread_counts`."""
    profile_picture = serializers.SerializerMethodField()
    is_locked = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Persona
        fields = ['id', 'display_name', 'profile_picture', 'color', 'is_primary', 'sort_order', 'is_locked', 'unread_count']

    def get_profile_picture(self, obj):
        return _first_photo_url(obj, self.context.get('request'))

    def get_is_locked(self, obj):
        return obj.pk not in self.context['unlocked_ids']

    def get_unread_count(self, obj):
        return self.context.get('unread_counts', {}).get(obj.pk, 0)


class PersonaSerializer(PersonaSummarySerializer):
    """Full editable persona for its owner. `gallery_images` are its public photos."""
    gallery_images = serializers.SerializerMethodField()

    class Meta(PersonaSummarySerializer.Meta):
        fields = PersonaSummarySerializer.Meta.fields + ['gallery_images'] + Persona.PROFILE_FIELDS
        read_only_fields = ['is_primary']

    def get_gallery_images(self, obj):
        return GalleryImageSerializer(_public_images(obj), many=True, context=self.context).data


class DiscoverPersonaSerializer(serializers.ModelSerializer):
    """Public-facing persona for the discover feed."""
    profile_picture = serializers.SerializerMethodField()
    gallery_images = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    neighborhood = serializers.CharField(source='user.neighborhood', read_only=True)
    is_verified = serializers.SerializerMethodField()
    kinks = serializers.SerializerMethodField()

    class Meta:
        model = Persona
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
        return _is_verified(obj.user)

    def get_kinks(self, obj):
        return obj.kinks if obj.share_kinks else []

    def get_profile_picture(self, obj):
        return _first_photo_url(obj, self.context.get('request'))

    def get_gallery_images(self, obj):
        return GalleryImageSerializer(_public_images(obj), many=True, context=self.context).data

    def _location_level(self, obj) -> int:
        return obj.location_sharing

    def get_latitude(self, obj):
        return _location_at_level(obj, self._location_level(obj), 0)

    def get_longitude(self, obj):
        return _location_at_level(obj, self._location_level(obj), 1)


class PersonaProfileSerializer(DiscoverPersonaSerializer):
    """
    Single-persona view — adds album- and location-grant access on top of public data.
    Context: `viewer_persona` (the viewer's active persona).
    """
    effective_location_sharing = serializers.SerializerMethodField()

    class Meta(DiscoverPersonaSerializer.Meta):
        fields = DiscoverPersonaSerializer.Meta.fields + ['effective_location_sharing']

    def get_gallery_images(self, obj):
        images = _public_images(obj) + _granted_images(obj, self.context.get('viewer_persona'))
        return GalleryImageSerializer(images, many=True, context=self.context).data

    def _location_grant_level(self, obj):
        viewer = self.context.get('viewer_persona')
        if viewer is None or viewer.pk == obj.pk:
            return None
        if not hasattr(self, '_grant_levels'):
            self._grant_levels = {}
        if obj.pk not in self._grant_levels:
            now = timezone.now()
            grant = LocationShareGrant.objects.filter(
                grantor=obj, grantee=viewer,
            ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).first()
            self._grant_levels[obj.pk] = grant.level if grant else None
        return self._grant_levels[obj.pk]

    def _location_level(self, obj) -> int:
        return max(obj.location_sharing, self._location_grant_level(obj) or 0)

    def get_effective_location_sharing(self, obj):
        return self._location_level(obj)


class AlbumSerializer(serializers.ModelSerializer):
    image_ids = serializers.SerializerMethodField()
    cover_url = serializers.SerializerMethodField()

    class Meta:
        model = Album
        fields = ['id', 'name', 'image_ids', 'cover_url', 'created_at']
        read_only_fields = ['id', 'created_at']

    def get_image_ids(self, obj):
        return [img.pk for img in obj.images.all()]

    def get_cover_url(self, obj):
        images = list(obj.images.all())
        return _image_url(images[0], self.context.get('request')) if images else None


class AlbumGrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlbumGrant
        fields = ['id', 'grantee_id', 'album_id', 'expires_at', 'created_at']
        read_only_fields = ['id', 'created_at']


class LocationShareGrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = LocationShareGrant
        fields = ['id', 'grantee_id', 'level', 'expires_at', 'created_at']
        read_only_fields = ['id', 'created_at']


class MessageSerializer(serializers.ModelSerializer):
    sender_id = serializers.IntegerField(read_only=True)
    conversation_id = serializers.IntegerField(read_only=True)
    reply_to = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'conversation_id', 'sender_id', 'client_id', 'kind', 'body', 'metadata',
            'reply_to', 'reactions', 'attachments', 'created_at', 'edited_at', 'deleted_at',
        ]

    def get_attachments(self, obj):
        # View-once photos never get a URL; they're fetched once through attachment_view.
        return [{
            'id': a.id,
            'mime': a.mime,
            'url': a.file.url if a.file and not a.view_once else None,
            'width': a.width,
            'height': a.height,
            'explicit': a.tag_explicit,
            'view_once': a.view_once,
            'viewed': a.viewed_at is not None,
        } for a in obj.attachments.all()]

    def get_reactions(self, obj):
        return [{'persona_id': r.persona_id, 'emoji': r.emoji} for r in obj.reactions.all()]

    def get_reply_to(self, obj):
        target = obj.reply_to
        if target is None:
            return None
        deleted = target.deleted_at is not None
        return {
            'id': target.id,
            'sender_id': target.sender_id,
            'kind': target.kind,
            'body': '' if deleted else target.body[:200],
            'deleted': deleted,
        }

    def to_representation(self, obj):
        data = super().to_representation(obj)
        if obj.deleted_at is not None:
            data['body'] = ''
            data['metadata'] = {}
            data['attachments'] = []
        return data


class ConversationSerializer(serializers.ModelSerializer):
    """Expects the queryset from views._conversation_qs (members, latest message, unread annotated)."""
    other_user = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    settings = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ['id', 'other_user', 'members', 'last_message', 'unread_count', 'settings', 'updated_at']

    def _members(self, obj):
        return [m for m in obj.members.all() if m.left_at is None]

    def _me(self, obj):
        me = self.context['persona']
        return next(m for m in obj.members.all() if m.persona_id == me.pk)

    def get_unread_count(self, obj):
        return max(obj.unread, 1 if self._me(obj).marked_unread else 0)

    def get_settings(self, obj):
        """The caller's own per-conversation settings."""
        me = self._me(obj)
        return {
            'pinned': me.pinned,
            'archived': me.archived,
            'muted_until': me.muted_until.isoformat() if me.muted_until else None,
            'marked_unread': me.marked_unread,
        }

    def get_other_user(self, obj):
        me = self.context['persona']
        other = next((m.persona for m in self._members(obj) if m.persona_id != me.pk), None)
        if other is None:
            return None
        return {
            'id': other.id,
            'display_name': other.display_name,
            'profile_picture': _first_photo_url(other, self.context['request']),
        }

    def get_members(self, obj):
        return [{
            'persona_id': m.persona_id,
            'display_name': m.persona.display_name,
            'profile_picture': _first_photo_url(m.persona, self.context['request']),
            'last_read_message_id': m.last_read_message_id,
            'last_delivered_message_id': m.last_delivered_message_id,
            # Only a persona the account is currently using shows activity.
            'last_active_at': (
                m.persona.last_active_at.isoformat()
                if m.persona.last_active_at and m.persona.user.active_persona_id == m.persona_id
                else None
            ),
        } for m in self._members(obj)]

    def get_last_message(self, obj):
        msg = obj.latest[0] if obj.latest else None
        if msg is None:
            return None
        return {
            'id': msg.id,
            'kind': msg.kind,
            'body': '' if msg.deleted_at else msg.body,
            'sender_id': msg.sender_id,
            'created_at': msg.created_at.isoformat(),
            'deleted': msg.deleted_at is not None,
        }
