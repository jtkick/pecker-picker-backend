import mimetypes

import phonenumbers
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from .models import User, Persona, PersonaMember, ProfileImage, Album, AlbumGrant, Conversation, Message, LocationShareGrant, FaceVerification, Lifeline, Friend
from . import discover
from .geo import fuzz_coords
from .membership import in_use


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
    if persona.group_type:
        # A group sits where the member who last used it is, and never more precisely
        # than General: members haven't each agreed to share an exact location.
        user = persona.last_active_user or user
        level = min(level, 1)
    if user.latitude is None or user.longitude is None:
        return None
    if level == 2:
        return (user.latitude, user.longitude)[coord]
    if level == 1:
        return fuzz_coords(user.latitude, user.longitude, persona.pk)[coord]
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
            "age",
            "pronouns",
            "height",
            "length",
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


def _accepted_members(persona) -> list[PersonaMember]:
    """Works with or without the views' accepted-only `group_members` prefetch."""
    return [m for m in persona.group_members.all() if m.status == PersonaMember.STATUS_ACCEPTED]


class PersonaSummarySerializer(serializers.ModelSerializer):
    """Compact entry for the persona switcher. Context: `request`, `unlocked_ids`, optional `unread_counts`."""
    profile_picture = serializers.SerializerMethodField()
    is_locked = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Persona
        fields = [
            'id', 'display_name', 'profile_picture', 'color_start', 'color_end', 'is_primary', 'sort_order',
            'is_locked', 'unread_count', 'group_type', 'role', 'can_edit', 'member_count',
        ]
        read_only_fields = ['group_type']

    def _is_owner(self, obj):
        return obj.user_id == self.context['request'].user.pk

    def get_role(self, obj):
        return PersonaMember.ROLE_OWNER if self._is_owner(obj) else PersonaMember.ROLE_MEMBER

    def get_can_edit(self, obj):
        user_id = self.context['request'].user.pk
        return self._is_owner(obj) or any(m.user_id == user_id and m.can_edit for m in _accepted_members(obj))

    def get_member_count(self, obj):
        return len(_accepted_members(obj)) if obj.group_type else 1

    def get_profile_picture(self, obj):
        return _first_photo_url(obj, self.context.get('request'))

    def get_is_locked(self, obj):
        return obj.pk not in self.context['unlocked_ids']

    def get_unread_count(self, obj):
        return self.context.get('unread_counts', {}).get(obj.pk, 0)


def persona_verified(persona) -> bool:
    """A solo persona is verified with its account; a group when every member is."""
    if persona.group_type:
        members = _accepted_members(persona)
        return bool(members) and all(_is_verified(m.user) for m in members)
    return _is_verified(persona.user)


class PersonaSerializer(PersonaSummarySerializer):
    """Full editable persona for its owner. `gallery_images` are its public photos."""
    gallery_images = serializers.SerializerMethodField()

    def validate_discover_filters(self, value):
        return discover.clean_filters(value)

    class Meta(PersonaSummarySerializer.Meta):
        fields = PersonaSummarySerializer.Meta.fields + ['gallery_images'] + Persona.PROFILE_FIELDS
        read_only_fields = ['is_primary', 'group_type']

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
    age = serializers.SerializerMethodField()
    pronouns = serializers.SerializerMethodField()
    height = serializers.SerializerMethodField()
    position = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    length = serializers.SerializerMethodField()
    distance = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    rank = serializers.SerializerMethodField()
    is_demo = serializers.BooleanField(source='user.is_demo', read_only=True)

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
            "is_side",
            "power_position",
            "can_host",
            "can_travel",
            "location_sharing",
            "latitude",
            "longitude",
            "neighborhood",
            "is_verified",
            "group_type",
            "members",
            "member_count",
            "distance",
            "is_online",
            "rank",
            "is_demo",
        ]

    def get_is_verified(self, obj):
        return persona_verified(obj)

    # From the discover feed (context `ranked`: {persona id: discover.Ranked}, `ranks`: {id: position}).
    def get_distance(self, obj):
        ranked = self.context.get('ranked', {}).get(obj.pk)
        return ranked.distance if ranked else None

    def get_is_online(self, obj):
        return discover.is_online(obj, timezone.now())

    def get_rank(self, obj):
        return self.context.get('ranks', {}).get(obj.pk)

    def get_members(self, obj):
        """For a group, each member's person card, leaving out blank values."""
        if not obj.group_type:
            return []
        result = []
        for m in _accepted_members(obj):
            entry = {'is_verified': _is_verified(m.user)}
            if m.name:
                entry['display_name'] = m.name
            for name in ('age', 'pronouns', 'height', 'length'):
                value = getattr(m, name)
                if value not in (None, ''):
                    entry[name] = value
            result.append(entry)
        return result

    def get_member_count(self, obj):
        return len(_accepted_members(obj)) if obj.group_type else 1

    def get_position(self, obj):
        return None if obj.is_side else obj.position

    # Account-wide values, shown only where the persona opts in.
    # Groups show these on each member's card instead (see get_members).
    def get_age(self, obj):
        return discover.shown_age(obj)

    def get_pronouns(self, obj):
        return obj.user.pronouns if obj.show_pronouns and not obj.group_type else ''

    def get_height(self, obj):
        return obj.user.height if obj.show_height and not obj.group_type else None

    def get_length(self, obj):
        return obj.user.length if obj.show_length and not obj.group_type else None

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
    is_friend = serializers.SerializerMethodField()

    class Meta(DiscoverPersonaSerializer.Meta):
        fields = DiscoverPersonaSerializer.Meta.fields + ['effective_location_sharing', 'is_friend']

    def get_is_friend(self, obj):
        viewer = self.context.get('viewer_persona')
        return viewer is not None and Friend.objects.filter(persona=viewer, friend=obj).exists()

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


class LifelineSerializer(serializers.ModelSerializer):
    persona_id = serializers.IntegerField(read_only=True)
    recipients = serializers.SerializerMethodField()

    class Meta:
        model = Lifeline
        fields = ['id', 'persona_id', 'recipients', 'status', 'started_at', 'expires_at',
                  'alerted_at', 'ended_at', 'end_reason']

    def get_recipients(self, obj):
        return [{'id': p.pk, 'display_name': p.display_name} for p in obj.recipients.all()]


def _member_name(persona_id: int, user_id: int) -> str:
    """Who in a group sent a message: their card name, else the profile they were invited as."""
    member = (
        PersonaMember.objects.filter(persona_id=persona_id, user_id=user_id)
        .select_related('invited_as').first()
    )
    if member is None:
        return 'A member'
    if member.name:
        return member.name
    return member.invited_as.display_name if member.invited_as else 'A member'


def _share_active(grant, metadata) -> bool:
    """A share's chat message is live while its grant exists and neither the grant nor the message has expired."""
    expires = parse_datetime(metadata['expires_at']) if metadata.get('expires_at') else None
    return grant is not None and grant.is_active() and (expires is None or timezone.now() < expires)


class MessageSerializer(serializers.ModelSerializer):
    """Context: `viewer_persona_id` — only when it's the sending group does `sent_by` say which member wrote it."""
    sender_id = serializers.IntegerField(read_only=True)
    conversation_id = serializers.IntegerField(read_only=True)
    reply_to = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    sent_by = serializers.SerializerMethodField()
    album = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'conversation_id', 'sender_id', 'client_id', 'kind', 'body', 'metadata',
            'reply_to', 'reactions', 'attachments', 'created_at', 'edited_at', 'deleted_at', 'sent_by', 'album',
            'location',
        ]

    def _album_share(self, obj):
        """(grant, active) for an album message: active while its grant exists and neither it nor this share has expired."""
        if not hasattr(self, '_shares'):
            self._shares = {}
        if obj.pk not in self._shares:
            grant = AlbumGrant.objects.select_related('album').filter(pk=obj.metadata.get('grant_id')).first()
            self._shares[obj.pk] = (grant, _share_active(grant, obj.metadata))
        return self._shares[obj.pk]

    def get_album(self, obj):
        if obj.kind != Message.KIND_ALBUM or obj.deleted_at is not None:
            return None
        _, active = self._album_share(obj)
        return {'name': obj.metadata.get('name', ''), 'expires_at': obj.metadata.get('expires_at'), 'active': active}

    def get_sent_by(self, obj):
        if obj.sender_id != self.context.get('viewer_persona_id') or obj.sender_user_id is None:
            return None
        if not obj.sender.group_type:
            return None
        if not hasattr(self, '_names'):
            self._names = {}
        if obj.sender_user_id not in self._names:
            self._names[obj.sender_user_id] = _member_name(obj.sender_id, obj.sender_user_id)
        return self._names[obj.sender_user_id]

    def get_location(self, obj):
        """The sharer's current location at the shared level, only while the share is active."""
        if obj.kind != Message.KIND_LIVE_LOCATION or obj.deleted_at is not None:
            return None
        grant = (
            LocationShareGrant.objects.select_related('grantor__user', 'grantor__last_active_user')
            .filter(pk=obj.metadata.get('grant_id')).first()
        )
        active = _share_active(grant, obj.metadata)
        data = {'level': None, 'expires_at': obj.metadata.get('expires_at'), 'active': active,
                'latitude': None, 'longitude': None, 'located_at': None}
        if active:
            persona = grant.grantor
            user = (persona.last_active_user or persona.user) if persona.group_type else persona.user
            # Groups never share more precisely than General (see _location_at_level).
            data['level'] = min(grant.level, 1) if persona.group_type else grant.level
            data['latitude'] = _location_at_level(persona, grant.level, 0)
            data['longitude'] = _location_at_level(persona, grant.level, 1)
            if data['latitude'] is not None and user.location_updated_at is not None:
                data['located_at'] = user.location_updated_at.isoformat()
        return data

    def get_attachments(self, obj):
        if obj.kind == Message.KIND_ALBUM:
            # The album's current photos, only while the share is active.
            grant, active = self._album_share(obj)
            if not active:
                return []
            return [{
                'id': img.id,
                'mime': mimetypes.guess_type(img.image.name)[0] or '',
                'url': img.image.url,
                'width': None,
                'height': None,
                'explicit': img.tag_explicit,
                'view_once': False,
                'viewed': False,
            } for img in grant.album.images.all()]
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
            # Only a persona someone is currently using shows activity.
            'last_active_at': (
                m.persona.last_active_at.isoformat()
                if m.persona.last_active_at and in_use(m.persona)
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


class GroupMemberSerializer(serializers.ModelSerializer):
    """A group member as the group's own members see them: who the account is, plus their person card."""
    is_me = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()

    class Meta:
        model = PersonaMember
        fields = [
            'id', 'role', 'status', 'can_edit', 'is_me', 'display_name', 'profile_picture',
            *PersonaMember.CARD_FIELDS,
        ]

    def get_is_me(self, obj):
        return obj.user_id == self.context['request'].user.pk

    def get_display_name(self, obj):
        return obj.invited_as.display_name if obj.invited_as else 'A member'

    def get_profile_picture(self, obj):
        return _first_photo_url(obj.invited_as, self.context['request']) if obj.invited_as else None


class InvitationSerializer(serializers.ModelSerializer):
    """An invitation to join a group persona, for the invitee."""
    group = serializers.SerializerMethodField()
    invited_by = serializers.SerializerMethodField()

    class Meta:
        model = PersonaMember
        fields = ['id', 'group', 'invited_by', 'created_at']

    def get_group(self, obj):
        p = obj.persona
        return {
            'id': p.pk,
            'display_name': p.display_name,
            'group_type': p.group_type,
            'profile_picture': _first_photo_url(p, self.context['request']),
            'member_count': len(_accepted_members(p)),
            'color_start': p.color_start,
            'color_end': p.color_end,
        }

    def get_invited_by(self, obj):
        via = obj.invited_via
        if via is None:
            return None
        return {'display_name': via.display_name, 'profile_picture': _first_photo_url(via, self.context['request'])}
