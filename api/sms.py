import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def _masked(phone_number: str) -> str:
    return f"…{phone_number[-4:]}"


def send_otp_sms(phone_number: str, code: str) -> None:
    """Dev-only path: log the OTP instead of sending real SMS."""
    if not settings.DEBUG:
        logger.error("SMS is not configured; OTP for %s was not sent", _masked(phone_number))
        return
    logger.warning("[DEV] OTP for %s: %s", phone_number, code)


def send_verify_sms(phone_number: str) -> None:
    """Start a Twilio Verify check — Twilio generates and sends the code."""
    from twilio.rest import Client

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    verification = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID).verifications.create(
        to=phone_number,
        channel="sms",
    )
    logger.info("Verify started for %s — status: %s", _masked(phone_number), verification.status)


def check_verify_sms(phone_number: str, code: str) -> bool:
    """Check a Twilio Verify code. Returns True if approved."""
    from twilio.rest import Client

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    result = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID).verification_checks.create(
        to=phone_number,
        code=code,
    )
    logger.info("Verify check for %s — status: %s", _masked(phone_number), result.status)
    return result.status == "approved"
