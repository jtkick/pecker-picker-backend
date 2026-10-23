"""
Delete expired one-time auth rows (passkey challenges, OTP codes).
Run daily, alongside simplejwt's `flushexpiredtokens`.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import OTPVerification, WebAuthnChallenge


class Command(BaseCommand):
    help = "Delete expired passkey challenges and OTP codes."

    def handle(self, *args, **options):
        # A day's grace keeps recent rows around for debugging a failed sign-in.
        cutoff = timezone.now() - timedelta(days=1)
        challenges, _ = WebAuthnChallenge.objects.filter(expires_at__lt=cutoff).delete()
        otps, _ = OTPVerification.objects.filter(expires_at__lt=cutoff).delete()
        self.stdout.write(f"Pruned {challenges} passkey challenges, {otps} OTP codes.")
