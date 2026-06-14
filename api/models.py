from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
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
    date_joined = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    display_name = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=300, blank=True)
    pronouns = models.CharField(max_length=20, blank=True)
    age = models.SmallIntegerField(null=True, blank=True)
    notifications_enabled = models.BooleanField(default=True)

    # 0 = Browsing, 1 = Looking
    intent = models.SmallIntegerField(default=0)
    looking_for_chat = models.BooleanField(default=False)
    looking_for_cuddle = models.BooleanField(default=False)
    looking_for_fool_around = models.BooleanField(default=False)
    looking_for_sex = models.BooleanField(default=False)

    # 0 = Off, 1 = General, 2 = Exact
    location_sharing = models.SmallIntegerField(default=0)

    share_images = models.BooleanField(default=False)
    share_face = models.BooleanField(default=False)
    share_torso = models.BooleanField(default=False)
    share_explicit = models.BooleanField(default=False)
    share_other = models.BooleanField(default=False)

    blur_explicit = models.BooleanField(default=False)

    POSITION_CHOICES = [(0,'Bottom'),(1,'Vers Bottom'),(2,'Vers'),(3,'Vers Top'),(4,'Top')]
    POWER_POSITION_CHOICES = [(0,'Sub'),(1,'Sub Switch'),(2,'Switch'),(3,'Dom Switch'),(4,'Dom')]

    position       = models.SmallIntegerField(null=True, blank=True, choices=POSITION_CHOICES)
    power_position = models.SmallIntegerField(null=True, blank=True, choices=POWER_POSITION_CHOICES)

    height = models.SmallIntegerField(null=True, blank=True)   # cm
    length = models.SmallIntegerField(null=True, blank=True)   # tenths of an inch, 0–120
    kinks  = models.JSONField(default=list, blank=True)        # list of strings
    share_kinks = models.BooleanField(default=False)
    interests = models.JSONField(default=list, blank=True)          # list of strings
    relationship_types = models.JSONField(default=list, blank=True)  # list of strings

    can_host = models.BooleanField(default=False)
    can_travel = models.BooleanField(default=False)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
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

    def has_tier(self, tier: int) -> bool:
        """True if the user's active subscription is at least `tier`."""
        if self.subscription_tier < tier:
            return False
        if self.subscription_expires_at and timezone.now() > self.subscription_expires_at:
            return False
        return True

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.phone_number or f'User {self.pk}'


class ProfileImage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='gallery_images')
    image = models.ImageField(upload_to='gallery/')
    order = models.SmallIntegerField(default=0)
    tag_face     = models.BooleanField(default=False)
    tag_torso    = models.BooleanField(default=False)
    tag_explicit = models.BooleanField(default=False)
    tag_other    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

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


class ImageShareGrant(models.Model):
    CATEGORY_CHOICES = [
        ('face', 'Face'),
        ('torso', 'Torso'),
        ('explicit', 'Explicit'),
        ('other', 'Other'),
    ]
    grantor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='image_grants_given')
    grantee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='image_grants_received')
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('grantor', 'grantee', 'category')]

    def is_active(self):
        return self.expires_at is None or timezone.now() < self.expires_at


class LocationShareGrant(models.Model):
    LEVEL_CHOICES = [(1, 'General'), (2, 'Exact')]
    grantor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='location_grants_given')
    grantee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='location_grants_received')
    level = models.SmallIntegerField(choices=LEVEL_CHOICES, default=2)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('grantor', 'grantee')]

    def is_active(self):
        return self.expires_at is None or timezone.now() < self.expires_at


class Conversation(models.Model):
    participants = models.ManyToManyField(User, related_name='conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-updated_at']


class Message(models.Model):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages'
    )
    sender = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='sent_messages'
    )
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['created_at']


class Block(models.Model):
    blocker = models.ForeignKey(User, on_delete=models.CASCADE, related_name='blocks_given')
    blocked = models.ForeignKey(User, on_delete=models.CASCADE, related_name='blocks_received')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('blocker', 'blocked')]


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
