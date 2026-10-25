"""
Account deletion. Everything the user created goes: personas (including
groups they own), photos, conversations they're in, messages they sent as a
member of someone else's group, passkeys, tokens. Stored files are removed
from object storage once the database delete commits.
"""
import logging

import stripe
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from .models import Conversation, Message, MessageAttachment, ProfileImage, User

logger = logging.getLogger(__name__)


class AccountDeletionError(Exception):
    """Nothing was deleted. The message is safe to show the user."""


def delete_account(user: User) -> None:
    _cancel_subscription(user)
    with transaction.atomic():
        persona_ids = list(user.personas.values_list('pk', flat=True))
        conversation_ids = list(
            Conversation.objects.filter(members__persona__in=persona_ids).values_list('pk', flat=True).distinct()
        )
        own_messages = Message.objects.filter(sender_user=user)
        files = [
            *MessageAttachment.objects.filter(message__conversation__in=conversation_ids).values_list('file', flat=True),
            *MessageAttachment.objects.filter(message__in=own_messages).values_list('file', flat=True),
            *ProfileImage.objects.filter(user=user).values_list('image', flat=True),
        ]
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)

        own_messages.delete()
        Conversation.objects.filter(pk__in=conversation_ids).delete()
        user.delete()
        transaction.on_commit(lambda: _delete_files({f for f in files if f}))


def _cancel_subscription(user: User) -> None:
    """Stop billing before the account (and our record of the subscription) is gone."""
    if not (user.stripe_subscription_id and settings.STRIPE_SECRET_KEY):
        return
    try:
        stripe.Subscription.cancel(user.stripe_subscription_id)
    except stripe.InvalidRequestError as exc:
        if exc.code != 'resource_missing':   # already gone on Stripe's side is fine
            raise AccountDeletionError("We couldn't cancel your subscription. Please try again.") from exc
    except stripe.StripeError as exc:
        logger.warning("Subscription cancel failed for user %s: %s", user.pk, exc)
        raise AccountDeletionError("We couldn't cancel your subscription. Please try again.") from exc


def _delete_files(names: set[str]) -> None:
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:
            logger.warning("Couldn't delete %s from storage after account deletion", name, exc_info=True)
