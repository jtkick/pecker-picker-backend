"""
Delete expired one-time auth rows (passkey challenges, OTP codes) and
closed lifelines, which hold a last known location.
Run daily, alongside simplejwt's `flushexpiredtokens`.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import Lifeline, OTPVerification, WebAuthnChallenge


class Command(BaseCommand):
    help = "Delete expired passkey challenges, OTP codes and old lifelines."

    def handle(self, *args, **options):
        # A day's grace keeps recent rows around for debugging a failed sign-in.
        cutoff = timezone.now() - timedelta(days=1)
        challenges, _ = WebAuthnChallenge.objects.filter(expires_at__lt=cutoff).delete()
        otps, _ = OTPVerification.objects.filter(expires_at__lt=cutoff).delete()
        _, by_model = Lifeline.objects.filter(
            status=Lifeline.STATUS_ENDED, ended_at__lt=timezone.now() - timedelta(days=30),
        ).delete()
        lifelines = by_model.get('api.Lifeline', 0)
        self.stdout.write(f"Pruned {challenges} passkey challenges, {otps} OTP codes, {lifelines} lifelines.")
