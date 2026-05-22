import random
import string
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTPVerification, User, ProfileImage
from .serializers import RequestOTPSerializer, VerifyOTPSerializer, ProfileSerializer, GalleryImageSerializer
from .sms import send_otp_sms
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


@api_view(["GET", "PATCH"])
def me(request):
    ctx = {'request': request}
    if request.method == "GET":
        return Response(ProfileSerializer(request.user, context=ctx).data)
    serializer = ProfileSerializer(request.user, data=request.data, partial=True, context=ctx)
    serializer.is_valid(raise_exception=True)
    serializer.save()
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


@api_view(["POST", "DELETE"])
def avatar(request):
    if request.method == "DELETE":
        if request.user.profile_picture:
            request.user.profile_picture.delete(save=True)
        return Response(status=status.HTTP_204_NO_CONTENT)

    image, err = _validate_image(request)
    if err:
        return err

    if request.user.profile_picture:
        request.user.profile_picture.delete(save=False)
    request.user.profile_picture = image
    request.user.save(update_fields=['profile_picture'])
    return Response({'profile_picture': request.build_absolute_uri(request.user.profile_picture.url)})


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

    obj = ProfileImage.objects.create(
        user=request.user,
        image=image,
        order=request.user.gallery_images.count(),
    )
    return Response(
        GalleryImageSerializer(obj, context={'request': request}).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["DELETE"])
def gallery_delete(request, pk):
    try:
        obj = request.user.gallery_images.get(pk=pk)
    except ProfileImage.DoesNotExist:
        return Response(status=status.HTTP_404_NOT_FOUND)
    obj.image.delete(save=False)
    obj.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
