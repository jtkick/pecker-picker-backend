"""
Push notifications via Firebase Cloud Messaging (covers APNs on iOS).

Disabled unless FIREBASE_CREDENTIALS_FILE points at a service-account JSON.
Notifications never include the sender, persona or message text — the lock
screen only ever shows "New message" or "New invitation".
"""
import logging
import threading

from django.conf import settings
from django.db import close_old_connections, transaction

from .membership import persona_user_ids
from .models import ConversationMember, DeviceToken

logger = logging.getLogger(__name__)

_app = None


def _firebase_app():
    global _app
    if _app is None:
        import firebase_admin
        from firebase_admin import credentials
        _app = firebase_admin.initialize_app(credentials.Certificate(settings.FIREBASE_CREDENTIALS_FILE))
    return _app


def _send(targets: list[tuple[int, dict]], title: str, thread: str) -> None:
    """Push `title` to each (user_id, data) target's devices, grouped under `thread`."""
    from firebase_admin import messaging
    try:
        app = _firebase_app()
        for user_id, data in targets:
            tokens = list(DeviceToken.objects.filter(user_id=user_id).values_list('token', flat=True))
            if not tokens:
                continue
            msg = messaging.MulticastMessage(
                tokens=tokens,
                notification=messaging.Notification(title=title),
                data=data,
                android=messaging.AndroidConfig(
                    notification=messaging.AndroidNotification(tag=thread),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(aps=messaging.Aps(sound='default', thread_id=thread)),
                ),
            )
            res = messaging.send_each_for_multicast(msg, app=app)
            stale = [
                tokens[i] for i, r in enumerate(res.responses)
                if not r.success and isinstance(r.exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError))
            ]
            if stale:
                DeviceToken.objects.filter(token__in=stale).delete()
    except Exception:
        logger.exception('Push notification failed')
    finally:
        close_old_connections()


def notify_new_message(message) -> None:
    """After commit, push to every account behind the other members who haven't muted it."""
    if not settings.FIREBASE_CREDENTIALS_FILE:
        return
    members = (
        ConversationMember.objects
        .filter(conversation_id=message.conversation_id, left_at__isnull=True)
        .exclude(persona_id=message.sender_id)
    )
    conv_id = message.conversation_id
    targets = [
        (user_id, {'conversation_id': str(conv_id), 'persona_id': str(m.persona_id)})
        for m in members if not m.is_muted()
        for user_id in persona_user_ids([m.persona_id])
    ]
    if targets:
        transaction.on_commit(lambda: threading.Thread(
            target=_send, args=(targets, 'New message', f'conversation-{conv_id}'), daemon=True,
        ).start())


def notify_persona_invite(user_id: int) -> None:
    """After commit, tell an account it was invited to a group profile."""
    if not settings.FIREBASE_CREDENTIALS_FILE:
        return
    targets = [(user_id, {'type': 'persona.invite'})]
    transaction.on_commit(lambda: threading.Thread(
        target=_send, args=(targets, 'New invitation', 'invitations'), daemon=True,
    ).start())
