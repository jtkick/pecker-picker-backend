from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db.models import Subquery
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from .models import ConversationMember, Persona, User
from .realtime import broadcast, mark_receipt, user_group


@database_sync_to_async
def _authenticate(scope):
    """Resolve the user from an `Authorization: Bearer <access token>` handshake header."""
    headers = dict(scope.get('headers', []))
    raw = headers.get(b'authorization', b'').decode()
    if not raw.startswith('Bearer '):
        return None
    auth = JWTAuthentication()
    try:
        user = auth.get_user(auth.get_validated_token(raw[len('Bearer '):]))
    except (InvalidToken, TokenError):
        return None
    return user if user.is_active else None


@database_sync_to_async
def _touch_active_persona(user):
    """Record activity on the persona the account is using (drives 'Active now')."""
    # Read the active persona fresh: the user may have switched since connecting.
    active = User.objects.filter(pk=user.pk).values('active_persona_id')[:1]
    Persona.objects.filter(pk=Subquery(active)).update(last_active_at=timezone.now())


@database_sync_to_async
def _membership(user, conversation_id):
    try:
        return ConversationMember.objects.get(
            conversation_id=int(conversation_id), persona__user=user, left_at__isnull=True,
        )
    except (ConversationMember.DoesNotExist, ConversationMember.MultipleObjectsReturned, TypeError, ValueError):
        return None


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    One socket per signed-in device. Server → client: chat events (see api.realtime).
    Client → server: `typing`, `read`, `delivered` with `conversation_id` (+ `message_id`),
    and a periodic `ping` that keeps the account's activity time fresh.
    """

    async def connect(self):
        self.user = await _authenticate(self.scope)
        if self.user is None:
            await self.close(code=4401)
            return
        self.group = user_group(self.user.pk)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await _touch_active_persona(self.user)

    async def disconnect(self, code):
        if getattr(self, 'group', None):
            await self.channel_layer.group_discard(self.group, self.channel_name)
            await _touch_active_persona(self.user)

    async def receive_json(self, content, **kwargs):
        kind = content.get('type')
        if kind == 'ping':
            await _touch_active_persona(self.user)
            return
        if kind not in ('typing', 'read', 'delivered'):
            return
        member = await _membership(self.user, content.get('conversation_id'))
        if member is None:
            return
        if kind == 'typing':
            await database_sync_to_async(broadcast)(member.conversation_id, {
                'type': 'typing',
                'conversation_id': member.conversation_id,
                'persona_id': member.persona_id,
            }, exclude_user_id=self.user.pk)
            return
        try:
            message_id = int(content.get('message_id'))
        except (TypeError, ValueError):
            return
        await database_sync_to_async(mark_receipt)(member, kind, message_id)

    async def chat_event(self, event):
        await self.send_json(event['payload'])
