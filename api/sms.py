import logging
from django.conf import settings

logger = logging.getLogger(__name__)


class SmsUnavailable(Exception):
    """No code was sent. The message is safe to show the user."""


def _masked(phone_number: str) -> str:
    return f"…{phone_number[-4:]}"


def send_otp_sms(phone_number: str, code: str) -> None:
    """Dev-only path: log the OTP instead of sending real SMS."""
    if not settings.DEBUG:
        logger.error("SMS is not configured; OTP for %s was not sent", _masked(phone_number))
        raise SmsUnavailable("Text message sign-in isn't available right now.")
    logger.warning("[DEV] OTP for %s: %s", phone_number, code)


def send_verify_sms(phone_number: str) -> None:
    """Start a Twilio Verify check — Twilio generates and sends the code."""
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    try:
        verification = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID).verifications.create(
            to=phone_number,
            channel="sms",
        )
    except TwilioRestException as exc:
        # e.g. 60200 invalid number, 60605 country blocked by geo permissions.
        logger.warning("Verify refused %s: Twilio error %s", _masked(phone_number), exc.code)
        raise SmsUnavailable("We couldn't send a code to that number.") from exc
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
