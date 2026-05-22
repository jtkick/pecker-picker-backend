from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, phone_number, **extra_fields):
        if not phone_number:
            raise ValueError("Phone number is required")
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
    phone_number = models.CharField(max_length=20, unique=True)
    date_joined = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    profile_picture = models.ImageField(upload_to='avatars/', null=True, blank=True)

    display_name = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=300, blank=True)
    pronouns = models.CharField(max_length=20, blank=True)
    age = models.SmallIntegerField(null=True, blank=True)
    species = models.CharField(max_length=50, blank=True)
    notifications_enabled = models.BooleanField(default=True)

    # 0 = Browsing, 1 = Looking
    intent = models.SmallIntegerField(default=0)
    looking_for_chat = models.BooleanField(default=False)
    looking_for_cuddle = models.BooleanField(default=False)
    looking_for_fool_around = models.BooleanField(default=False)
    looking_for_sex = models.BooleanField(default=False)

    # 0 = Off, 1 = General, 2 = Exact
    location_sharing = models.SmallIntegerField(default=0)

    share_face = models.BooleanField(default=False)
    share_torso = models.BooleanField(default=False)
    share_explicit = models.BooleanField(default=False)
    share_other = models.BooleanField(default=False)

    can_host = models.BooleanField(default=False)
    can_travel = models.BooleanField(default=False)

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.phone_number


class ProfileImage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='gallery_images')
    image = models.ImageField(upload_to='gallery/')
    order = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']


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
