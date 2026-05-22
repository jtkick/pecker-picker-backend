import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def send_otp_sms(phone_number: str, code: str) -> None:
    if not settings.TWILIO_ENABLED:
        print(f"[DEV] OTP for {phone_number}: {code}", flush=True)
        return

    from twilio.rest import Client

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    client.messages.create(
        body=f"Your Pecker Picker code is {code}. Valid for {settings.OTP_EXPIRY_MINUTES} minutes.",
        from_=settings.TWILIO_PHONE_NUMBER,
        to=phone_number,
    )
