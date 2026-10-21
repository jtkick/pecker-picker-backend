"""
Realtime fan-out over the Channels layer. Every connected client joins its
account's group; events carry `conversation_id` so the client can route them.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q

from .models import ConversationMember, Message


def user_group(user_id: int) -> str:
    return f'user_{user_id}'


def member_user_ids(conversation_id: int) -> set[int]:
    return set(
        ConversationMember.objects
        .filter(conversation_id=conversation_id, left_at__isnull=True)
        .values_list('persona__user_id', flat=True)
    )


def send_to_users(user_ids, payload: dict) -> None:
    layer = get_channel_layer()
    for uid in user_ids:
        async_to_sync(layer.group_send)(user_group(uid), {'type': 'chat.event', 'payload': payload})


def broadcast(conversation_id: int, payload: dict, exclude_user_id: int | None = None) -> None:
    """Send `payload` to every member's devices once the current transaction commits."""
    user_ids = member_user_ids(conversation_id) - {exclude_user_id}
    transaction.on_commit(lambda: send_to_users(user_ids, payload))


def mark_receipt(member: ConversationMember, kind: str, message_id: int) -> bool:
    """
    Advance a member's `read` or `delivered` pointer to `message_id` (never backwards;
    reading implies delivery). Returns True and broadcasts if anything changed.
    """
    if not Message.objects.filter(pk=message_id, conversation_id=member.conversation_id).exists():
        return False
    fields = ['last_delivered_message'] if kind == 'delivered' else ['last_read_message', 'last_delivered_message']
    changed = False
    for field in fields:
        updated = (
            ConversationMember.objects
            .filter(pk=member.pk)
            .filter(Q(**{f'{field}__isnull': True}) | Q(**{f'{field}_id__lt': message_id}))
            .update(**{f'{field}_id': message_id})
        )
        changed = changed or bool(updated)
    if changed:
        broadcast(member.conversation_id, {
            'type': kind,
            'conversation_id': member.conversation_id,
            'persona_id': member.persona_id,
            'message_id': message_id,
        })
    return changed
