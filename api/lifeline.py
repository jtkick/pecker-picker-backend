"""
Lifeline: a safety check-in timer run by the server, so it still fires when
the owner's phone is dead or gone.

`warn_due` and `alert_due` are run every few seconds by the `lifeline_worker`
command. Recipients hear nothing until an alert: then each gets a system
message in their chat with the lifeline's persona, a push, and exact location
for a day. Checking in afterwards posts a "safe" message and undoes the grants.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .membership import persona_user_ids
from .models import Block, Conversation, ConversationMember, Lifeline, LocationShareGrant, Message
from .push import notify_lifeline

WARN_BEFORE = timedelta(minutes=10)
ALERT_SHARE = timedelta(hours=24)


def _post(lifeline, recipient, kind, body, extra=None):
    """A system message from the lifeline's persona to one recipient, broadcast after commit."""
    from .views import _broadcast_message   # views imports this module

    conv, _ = Conversation.get_or_create_between(lifeline.persona, recipient)
    msg = Message.objects.create(
        conversation=conv, sender=lifeline.persona, sender_user=lifeline.user, kind='system', body=body,
        metadata={'type': kind, 'lifeline_id': lifeline.pk, **(extra or {})},
    )
    Conversation.objects.filter(pk=conv.pk).update(updated_at=timezone.now())
    conv.members.exclude(persona=lifeline.persona).filter(archived=True).update(archived=False)
    _broadcast_message('message.new', msg)
    return conv


def _recipient_targets(lifeline, conv_ids):
    """(user_id, data) push targets for every account behind each recipient, mute ignored."""
    members = ConversationMember.objects.filter(
        conversation_id__in=conv_ids, left_at__isnull=True,
    ).exclude(persona=lifeline.persona)
    return [
        (uid, {'conversation_id': str(m.conversation_id), 'persona_id': str(m.persona_id), 'type': 'lifeline'})
        for m in members
        for uid in persona_user_ids([m.persona_id]) - {lifeline.user_id}
    ]


def _reachable(lifeline):
    """Recipients neither side has blocked since the lifeline started."""
    user = lifeline.user_id
    for recipient in lifeline.recipients.all():
        others = persona_user_ids([recipient.pk])
        if not Block.objects.filter(
            Q(blocker_id=user, blocked__in=others) | Q(blocker__in=others, blocked_id=user)
            | Q(blocker_id=user, blocked__isnull=True, blocked_persona=recipient)
        ).exists():
            yield recipient


def _share_location(lifeline, recipient, until):
    """Give `recipient` exact location until `until`, remembering what to restore."""
    grant = LocationShareGrant.objects.filter(grantor=lifeline.persona, grantee=recipient).first()
    if grant and grant.level == 2 and (grant.expires_at is None or grant.expires_at >= until):
        return
    lifeline.grant_restore[str(recipient.pk)] = (
        {'level': grant.level, 'expires_at': grant.expires_at.isoformat() if grant.expires_at else None}
        if grant else None
    )
    LocationShareGrant.objects.update_or_create(
        grantor=lifeline.persona, grantee=recipient, defaults={'level': 2, 'expires_at': until},
    )


def _restore_grants(lifeline):
    for grantee_id, prior in lifeline.grant_restore.items():
        qs = LocationShareGrant.objects.filter(grantor=lifeline.persona, grantee_id=grantee_id)
        if prior is None:
            qs.delete()
        else:
            qs.update(level=prior['level'], expires_at=prior['expires_at'])
    lifeline.grant_restore = {}


def warn_due(now=None) -> int:
    """Push the owner once when their lifeline is about to run out."""
    now = now or timezone.now()
    due = list(Lifeline.objects.filter(
        status=Lifeline.STATUS_ACTIVE, warned_at__isnull=True, expires_at__lte=now + WARN_BEFORE,
    ).values_list('pk', 'user_id'))
    for pk, user_id in due:
        with transaction.atomic():
            if Lifeline.objects.filter(pk=pk, warned_at__isnull=True).update(warned_at=now):
                notify_lifeline([(user_id, {'type': 'lifeline.warning', 'lifeline_id': str(pk)})],
                                'Your lifeline ends soon — snooze or end it', 'lifeline')
    return len(due)


def alert_due(now=None) -> int:
    """Alert every recipient of each lifeline that ran out."""
    now = now or timezone.now()
    count = 0
    while True:
        with transaction.atomic():
            lifeline = (
                Lifeline.objects.select_for_update(skip_locked=True, of=('self',))
                .select_related('user', 'persona')
                .filter(status=Lifeline.STATUS_ACTIVE, expires_at__lte=now)
                .first()
            )
            if lifeline is None:
                return count
            alert(lifeline, now)
            count += 1


def alert(lifeline, now):
    user = lifeline.user
    lifeline.status = Lifeline.STATUS_ALERTED
    lifeline.alerted_at = now
    lifeline.alert_lat, lifeline.alert_lng = user.latitude, user.longitude
    lifeline.alert_location_at = user.location_updated_at if user.latitude is not None else None
    name = lifeline.persona.display_name or 'Your contact'
    extra = {
        'expected_at': lifeline.expires_at.isoformat(),
        'lat': lifeline.alert_lat,
        'lng': lifeline.alert_lng,
        'located_at': lifeline.alert_location_at.isoformat() if lifeline.alert_location_at else None,
    }
    conv_ids = []
    for recipient in _reachable(lifeline):
        conv_ids.append(_post(
            lifeline, recipient, 'lifeline.alert',
            f"{name} started a lifeline and hasn't checked in. They may need help — "
            f"try to reach them, and call emergency services if you can't.",
            extra,
        ).pk)
        _share_location(lifeline, recipient, now + ALERT_SHARE)
    lifeline.save()
    notify_lifeline(_recipient_targets(lifeline, conv_ids), "Lifeline alert: someone hasn't checked in", 'lifeline')


def end(lifeline, reason):
    """Close a lifeline; after an alert, tell recipients they're safe and undo the grants."""
    with transaction.atomic():
        lifeline = Lifeline.objects.select_for_update(of=('self',)).select_related('persona').get(pk=lifeline.pk)
        if lifeline.status not in Lifeline.OPEN:
            return lifeline
        was_alerted = lifeline.status == Lifeline.STATUS_ALERTED
        lifeline.status = Lifeline.STATUS_ENDED
        lifeline.ended_at = timezone.now()
        lifeline.end_reason = reason
        if was_alerted:
            name = lifeline.persona.display_name or 'Your contact'
            conv_ids = [
                _post(lifeline, r, 'lifeline.safe', f'{name} checked in and is safe.').pk
                for r in _reachable(lifeline)
            ]
            _restore_grants(lifeline)
            notify_lifeline(_recipient_targets(lifeline, conv_ids), 'Lifeline: your contact checked in safe', 'lifeline',
                            background=True)
        lifeline.save()
    return lifeline
