from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, phone_number=None, **extra_fields):
        user = self.model(phone_number=phone_number, **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        user = self.model(phone_number=phone_number, **extra_fields)
        if password:
            user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, PermissionsMixin):
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    phone_verified = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    notifications_enabled = models.BooleanField(default=True)
    blur_explicit = models.BooleanField(default=False)

    # Handmade demo accounts (see `manage.py demo_users`) have a handle and are
    # seen only by tester accounts; switching one off sets is_active False.
    demo_handle = models.CharField(max_length=40, unique=True, null=True, blank=True)
    is_tester = models.BooleanField(default=False)

    # Shared by every persona; each persona decides whether to show them (Persona.show_*).
    pronouns = models.CharField(max_length=20, blank=True)
    height = models.SmallIntegerField(null=True, blank=True)   # cm
    length = models.SmallIntegerField(null=True, blank=True)   # tenths of an inch, 0–120

    # The persona currently visible in discover and used for messaging.
    active_persona = models.ForeignKey(
        'Persona', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )

    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    neighborhood = models.CharField(max_length=100, blank=True)

    # ── Subscription ──────────────────────────────────────────────────────────
    # 0 = Free, 1 = Plus  (extend by adding new integer constants)
    TIER_FREE = 0
    TIER_PLUS = 1
    TIER_CHOICES = [(TIER_FREE, 'Free'), (TIER_PLUS, 'Plus')]

    subscription_tier       = models.SmallIntegerField(default=0)
    subscription_expires_at = models.DateTimeField(null=True, blank=True)
    stripe_customer_id      = models.CharField(max_length=100, blank=True)
    stripe_subscription_id  = models.CharField(max_length=100, blank=True)

    @property
    def is_demo(self) -> bool:
        return self.demo_handle is not None

    def has_tier(self, tier: int) -> bool:
        """True if the user's active subscription is at least `tier`."""
        if self.subscription_tier < tier:
            return False
        if self.subscription_expires_at and timezone.now() > self.subscription_expires_at:
            return False
        return True

    def _tier_limit(self, limits: dict) -> int:
        return limits[self.TIER_PLUS if self.has_tier(self.TIER_PLUS) else self.TIER_FREE]

    def persona_limit(self) -> int:
        return self._tier_limit(settings.PERSONA_LIMITS)

    def library_limit(self) -> int:
        return self._tier_limit(settings.LIBRARY_LIMITS)

    def unlocked_persona_ids(self) -> set[int]:
        """
        Personas usable under the current tier. Over the limit (e.g. after a
        downgrade) the primary plus the most recently used ones stay unlocked.
        Group personas need their owner to be Plus; ones the user only belongs
        to don't count toward the user's own limit.
        """
        is_plus = self.has_tier(self.TIER_PLUS)
        personas = [p for p in self.personas.all() if is_plus or not p.group_type]
        joined = {
            p.pk for p in Persona.objects.filter(
                group_members__user=self, group_members__status=PersonaMember.STATUS_ACCEPTED,
            ).exclude(user=self).select_related('user')
            if p.user.has_tier(self.TIER_PLUS)
        }
        limit = self.persona_limit()
        if len(personas) <= limit:
            return {p.pk for p in personas} | joined
        primary = [p for p in personas if p.is_primary]
        others = sorted(
            (p for p in personas if not p.is_primary),
            key=lambda p: (p.pk == self.active_persona_id, p.last_active_at or p.created_at),
            reverse=True,
        )
        return {p.pk for p in (primary + others)[:limit]} | joined

    def get_active_persona(self) -> 'Persona':
        """Return the active persona, creating/assigning the primary one if needed."""
        persona = self.active_persona
        if persona is None:
            persona = self.personas.filter(is_primary=True).first()
            if persona is None:
                persona = Persona.objects.create(user=self, is_primary=True)
            User.objects.filter(pk=self.pk).update(active_persona=persona)
            self.active_persona = persona
        return persona

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.phone_number or f'User {self.pk}'


class Persona(models.Model):
    """
    One of a user's profiles. Only a persona someone is using (their active one)
    appears in discover. A group persona (`group_type` set) is shared by its
    owner (`user`) and the accounts in `group_members`.
    """
    GROUP_COUPLE, GROUP_THROUPLE, GROUP_GROUP = 'couple', 'throuple', 'group'
    GROUP_TYPE_CHOICES = [('', 'Solo'), (GROUP_COUPLE, 'Couple'), (GROUP_THROUPLE, 'Throuple'), (GROUP_GROUP, 'Group')]
    GROUP_CAPS = {GROUP_COUPLE: 2, GROUP_THROUPLE: 3, GROUP_GROUP: 10}

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='personas')
    group_type = models.CharField(max_length=10, choices=GROUP_TYPE_CHOICES, blank=True, default='')
    # Member who last used the persona; places a group persona on the map and shows its activity.
    last_active_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    is_primary = models.BooleanField(default=False)
    sort_order = models.SmallIntegerField(default=0)
    # Accent gradient the client tints its UI with while this persona is active (0xRRGGBB).
    color_start = models.PositiveIntegerField(default=0xE53935, validators=[MaxValueValidator(0xFFFFFF)])
    color_end = models.PositiveIntegerField(default=0xF9A825, validators=[MaxValueValidator(0xFFFFFF)])
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The discover filters this persona browses with (see api.discover.clean_filters).
    discover_filters = models.JSONField(default=dict, blank=True)

    display_name = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=300, blank=True)
    age = models.SmallIntegerField(null=True, blank=True)

    # 0 = Browsing, 1 = Looking
    intent = models.SmallIntegerField(default=0)
    looking_for_chat = models.BooleanField(default=False)
    looking_for_cuddle = models.BooleanField(default=False)
    looking_for_fool_around = models.BooleanField(default=False)
    looking_for_sex = models.BooleanField(default=False)

    # 0 = Off, 1 = General, 2 = Exact
    location_sharing = models.SmallIntegerField(default=0)

    POSITION_CHOICES = [(0,'Bottom'),(1,'Vers Bottom'),(2,'Vers'),(3,'Vers Top'),(4,'Top')]
    POWER_POSITION_CHOICES = [(0,'Sub'),(1,'Sub Switch'),(2,'Switch'),(3,'Dom Switch'),(4,'Dom')]

    position       = models.SmallIntegerField(null=True, blank=True, choices=POSITION_CHOICES)
    is_side        = models.BooleanField(default=False)   # hides position; position is kept for switching back
    power_position = models.SmallIntegerField(null=True, blank=True, choices=POWER_POSITION_CHOICES)

    # Whether this persona shows the account's pronouns / height / length.
    show_pronouns = models.BooleanField(default=True)
    show_height = models.BooleanField(default=True)
    show_length = models.BooleanField(default=True)
    kinks  = models.JSONField(default=list, blank=True)        # list of strings
    interests = models.JSONField(default=list, blank=True)          # list of strings
    relationship_types = models.JSONField(default=list, blank=True)  # list of strings

    can_host = models.BooleanField(default=False)
    can_travel = models.BooleanField(default=False)

    # Fields copied by "duplicate"; everything a user edits on a profile.
    PROFILE_FIELDS = [
        'display_name', 'bio', 'age', 'intent',
        'looking_for_chat', 'looking_for_cuddle', 'looking_for_fool_around', 'looking_for_sex',
        'location_sharing', 'position', 'is_side', 'power_position', 'show_pronouns', 'show_height', 'show_length',
        'kinks', 'interests', 'relationship_types', 'can_host', 'can_travel', 'discover_filters',
    ]

    class Meta:
        ordering = ['sort_order', 'created_at']

    def __str__(self):
        return self.display_name or f'Persona {self.pk}'


class DiscoverEvent(models.Model):
    """A viewer acting on a profile they found in discover; used to tune ranking weights."""
    KIND_OPENED, KIND_MESSAGED = 'opened', 'messaged'

    viewer_persona = models.ForeignKey('Persona', on_delete=models.CASCADE, related_name='+')
    target_persona = models.ForeignKey('Persona', on_delete=models.CASCADE, related_name='+')
    kind = models.CharField(max_length=10, choices=[(KIND_OPENED, 'Opened'), (KIND_MESSAGED, 'Messaged')])
    rank = models.PositiveIntegerField()        # 0-based position in the feed
    sort = models.CharField(max_length=12, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PersonaMember(models.Model):
    """
    An account in a group persona. The owner has a row too. Only the owner sets
    `can_edit`; only the member themself sets what of their own info is shared.
    """
    ROLE_OWNER, ROLE_MEMBER = 'owner', 'member'
    STATUS_INVITED, STATUS_ACCEPTED, STATUS_DECLINED = 'invited', 'accepted', 'declined'
    STATUS_CHOICES = [(STATUS_INVITED, 'Invited'), (STATUS_ACCEPTED, 'Accepted'), (STATUS_DECLINED, 'Declined')]

    persona = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='group_members')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='group_memberships')
    role = models.CharField(max_length=10, choices=[(ROLE_OWNER, 'Owner'), (ROLE_MEMBER, 'Member')], default=ROLE_MEMBER)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_INVITED)
    invited_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    # The inviter's persona at the time — what the invitee knows them as.
    invited_via = models.ForeignKey(Persona, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    # The invitee's persona the owner invited — how the group knows them until they pick `represent_persona`.
    invited_as = models.ForeignKey(Persona, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    can_edit = models.BooleanField(default=False)
    # One of the member's own solo personas that stands for them (name + first photo).
    represent_persona = models.ForeignKey(Persona, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    share_profile = models.BooleanField(default=False)
    share_pronouns = models.BooleanField(default=False)
    share_height = models.BooleanField(default=False)
    share_length = models.BooleanField(default=False)

    SHARE_FIELDS = ['share_profile', 'share_pronouns', 'share_height', 'share_length']

    class Meta:
        unique_together = [('persona', 'user')]
        ordering = ['created_at']


class ProfileImage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='gallery_images')
    image = models.ImageField(upload_to='gallery/')
    order = models.SmallIntegerField(default=0)
    tag_face     = models.BooleanField(default=False)
    tag_torso    = models.BooleanField(default=False)
    tag_explicit = models.BooleanField(default=False)
    tag_other    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    albums = models.ManyToManyField('Album', related_name='images', blank=True)

    @property
    def tags(self) -> list[str]:
        result = []
        if self.tag_face:     result.append('face')
        if self.tag_torso:    result.append('torso')
        if self.tag_explicit: result.append('explicit')
        if self.tag_other:    result.append('other')
        return result

    class Meta:
        ordering = ['order', 'created_at']


class PersonaPhoto(models.Model):
    """A library image shown publicly on a persona, in display order."""
    persona = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='photos')
    image = models.ForeignKey(ProfileImage, on_delete=models.CASCADE, related_name='persona_photos')
    order = models.SmallIntegerField(default=0)

    class Meta:
        ordering = ['order']
        unique_together = [('persona', 'image')]


class Album(models.Model):
    """A named, account-wide collection of library images that can be shared privately."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='albums')
    name = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class AlbumGrant(models.Model):
    grantor = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='album_grants_given')
    grantee = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='album_grants_received')
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name='grants')
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('grantor', 'grantee', 'album')]

    def is_active(self):
        return self.expires_at is None or timezone.now() < self.expires_at


class LocationShareGrant(models.Model):
    LEVEL_CHOICES = [(1, 'General'), (2, 'Exact')]
    grantor = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='location_grants_given')
    grantee = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='location_grants_received')
    level = models.SmallIntegerField(choices=LEVEL_CHOICES, default=2)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('grantor', 'grantee')]

    def is_active(self):
        return self.expires_at is None or timezone.now() < self.expires_at


class Conversation(models.Model):
    participants = models.ManyToManyField(Persona, through='ConversationMember', related_name='conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-updated_at']


class ConversationMember(models.Model):
    """A persona's membership in a conversation, with its per-member read/delivery state."""
    ROLE_MEMBER, ROLE_ADMIN, ROLE_OWNER = 0, 1, 2
    ROLE_CHOICES = [(ROLE_MEMBER, 'Member'), (ROLE_ADMIN, 'Admin'), (ROLE_OWNER, 'Owner')]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='members')
    persona = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='memberships')
    role = models.SmallIntegerField(choices=ROLE_CHOICES, default=ROLE_MEMBER)
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)
    last_read_message = models.ForeignKey(
        'Message', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    last_delivered_message = models.ForeignKey(
        'Message', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    muted_until = models.DateTimeField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    pinned = models.BooleanField(default=False)
    marked_unread = models.BooleanField(default=False)   # cleared when the member next reads

    class Meta:
        unique_together = [('conversation', 'persona')]

    def is_muted(self):
        return self.muted_until is not None and timezone.now() < self.muted_until


class Message(models.Model):
    KIND_TEXT = 'text'
    KIND_IMAGE = 'image'
    KIND_CHOICES = [
        (KIND_TEXT, 'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
        ('voice', 'Voice'),
        ('album', 'Album'),
        ('location', 'Location'),
        ('live_location', 'Live location'),
        ('profile', 'Profile'),
        ('system', 'System'),
    ]

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages'
    )
    sender = models.ForeignKey(
        Persona, on_delete=models.CASCADE, related_name='sent_messages'
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_TEXT)
    body = models.TextField(max_length=2000, blank=True)
    metadata = models.JSONField(default=dict, blank=True)   # per-kind payload
    # Client-generated id so a retried send never creates a duplicate.
    client_id = models.UUIDField(null=True, blank=True)
    reply_to = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    # The account that sent it; differs between messages from a group persona.
    sender_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # Members who deleted this message for themselves only.
    hidden_for = models.ManyToManyField(Persona, related_name='+', blank=True)

    # Window in which the sender may edit or unsend.
    EDIT_WINDOW = timedelta(minutes=15)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(fields=['sender', 'client_id'], name='unique_message_client_id'),
        ]


class MessageAttachment(models.Model):
    """A file sent in a message (photos for now)."""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='attachments')
    order = models.SmallIntegerField(default=0)
    # Emptied once a view-once photo has been opened.
    file = models.FileField(upload_to='chat/', blank=True)
    mime = models.CharField(max_length=50)
    size = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    tag_explicit = models.BooleanField(default=False)
    view_once = models.BooleanField(default=False)
    viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['order']


class MessageReaction(models.Model):
    """One emoji reaction per member per message."""
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reactions')
    persona = models.ForeignKey(Persona, on_delete=models.CASCADE, related_name='+')
    emoji = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('message', 'persona')]


class SavedPhrase(models.Model):
    """A user's quick reply, insertable from the chat input."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_phrases')
    text = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class DeviceToken(models.Model):
    """A push-notification (FCM) registration token for one of the user's devices."""
    PLATFORM_CHOICES = [('ios', 'iOS'), ('android', 'Android')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='device_tokens')
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Block(models.Model):
    blocker = models.ForeignKey(User, on_delete=models.CASCADE, related_name='blocks_given')
    # Null for a block of a group persona only, which hides that group but not its members' own profiles.
    blocked = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE, related_name='blocks_received')
    # The persona the block was made from, shown in the blocked list. Account blocks apply account-wide.
    blocked_persona = models.ForeignKey(Persona, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('blocker', 'blocked')]
        constraints = [
            models.UniqueConstraint(
                fields=['blocker', 'blocked_persona'], condition=models.Q(blocked__isnull=True),
                name='unique_group_block',
            ),
        ]


class Report(models.Model):
    REASON_CHOICES = [
        ('spam', 'Spam'),
        ('harassment', 'Harassment'),
        ('fake', 'Fake profile'),
        ('inappropriate', 'Inappropriate content'),
        ('other', 'Other'),
    ]
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reports_given')
    reported = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reports_received')
    reported_persona = models.ForeignKey(Persona, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    # Evidence when reporting from a chat.
    message = models.ForeignKey('Message', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    conversation = models.ForeignKey('Conversation', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class FaceVerification(models.Model):
    STATUS_PENDING  = 'pending'
    STATUS_VERIFIED = 'verified'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES  = [
        (STATUS_PENDING,  'Pending'),
        (STATUS_VERIFIED, 'Verified'),
        (STATUS_REJECTED, 'Rejected'),
    ]
    user         = models.OneToOneField(User, on_delete=models.CASCADE, related_name='verification')
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at  = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} – {self.status}"


class Kink(models.Model):
    name = models.CharField(max_length=100, unique=True)
    parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='children',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @classmethod
    def related_names(cls, names: list[str]) -> set[str]:
        """
        Return the given kink names plus all their ancestors and descendants.
        Used to broaden kink-based matching across the hierarchy.
        """
        all_kinks = list(cls.objects.select_related('parent'))
        by_name = {k.name: k for k in all_kinks}
        children_of: dict[str, list[str]] = {}
        for k in all_kinks:
            if k.parent_id is not None:
                children_of.setdefault(k.parent.name, []).append(k.name)

        result = set(names)

        # Walk up to ancestors
        for name in names:
            node = by_name.get(name)
            while node and node.parent:
                result.add(node.parent.name)
                node = by_name.get(node.parent.name)

        # Walk down to all descendants (BFS)
        queue = list(names)
        while queue:
            current = queue.pop()
            for child in children_of.get(current, []):
                if child not in result:
                    result.add(child)
                    queue.append(child)

        return result


class Interest(models.Model):
    name = models.CharField(max_length=100, unique=True)
    parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='children',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class OTPVerification(models.Model):
    phone_number = models.CharField(max_length=20)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def is_valid(self):
        return not self.is_used and timezone.now() < self.expires_at


class PasskeyCredential(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='passkey_credentials')
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveBigIntegerField(default=0)
    transports = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Passkey for {self.user}'


class WebAuthnChallenge(models.Model):
    session_id = models.UUIDField(unique=True)
    challenge = models.CharField(max_length=256)
    action = models.CharField(max_length=20)  # 'register' | 'authenticate'
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    def is_expired(self):
        return timezone.now() > self.expires_at
