import phonenumbers
from rest_framework import serializers
from .models import User, ProfileImage


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


class RequestOTPSerializer(serializers.Serializer):
    phone_number = serializers.CharField()

    def validate_phone_number(self, value):
        return normalize_phone(value)


class VerifyOTPSerializer(serializers.Serializer):
    phone_number = serializers.CharField()
    code = serializers.CharField(min_length=6, max_length=6)

    def validate_phone_number(self, value):
        return normalize_phone(value)

    def validate_code(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("OTP must be 6 digits.")
        return value


class GalleryImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = ProfileImage
        fields = ['id', 'url', 'order']

    def get_url(self, obj):
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class ProfileSerializer(serializers.ModelSerializer):
    profile_picture = serializers.SerializerMethodField()
    gallery_images = GalleryImageSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = [
            "profile_picture",
            "gallery_images",
            "display_name",
            "bio",
            "pronouns",
            "age",
            "species",
            "notifications_enabled",
            "intent",
            "looking_for_chat",
            "looking_for_cuddle",
            "looking_for_fool_around",
            "looking_for_sex",
            "location_sharing",
            "share_face",
            "share_torso",
            "share_explicit",
            "share_other",
            "can_host",
            "can_travel",
        ]

    def get_profile_picture(self, obj):
        if not obj.profile_picture:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.profile_picture.url) if request else obj.profile_picture.url
