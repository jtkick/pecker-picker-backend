"""
Who can use a persona. A solo persona belongs to `Persona.user`; a group
persona is shared by its owner (`Persona.user`) and its accepted members.
"""
from django.db.models import F, Q
from django.utils import timezone

from .models import Persona, PersonaMember, User

ACCEPTED = PersonaMember.STATUS_ACCEPTED


def personas_for(user):
    """Personas `user` may use: their own, plus groups they've joined."""
    return Persona.objects.filter(
        Q(user=user) | Q(group_members__user=user, group_members__status=ACCEPTED),
    ).distinct()


def persona_user_ids(persona_ids) -> set[int]:
    """Accounts behind the given personas (owners and accepted group members)."""
    owners = Persona.objects.filter(pk__in=persona_ids).values_list('user_id', flat=True)
    members = PersonaMember.objects.filter(persona_id__in=persona_ids, status=ACCEPTED).values_list('user_id', flat=True)
    return set(owners) | set(members)


def membership(user, persona) -> PersonaMember | None:
    """`user`'s accepted row in a group persona, if any."""
    if not persona.group_type:
        return None
    return PersonaMember.objects.filter(persona=persona, user=user, status=ACCEPTED).first()


def is_editor(user, persona) -> bool:
    if persona.user_id == user.pk:
        return True
    member = membership(user, persona)
    return member is not None and member.can_edit


def group_locked(persona) -> bool:
    """A group is read-only and hidden while its owner isn't Plus."""
    return bool(persona.group_type) and not persona.user.has_tier(User.TIER_PLUS)


def in_use(persona) -> bool:
    """Whether someone currently has `persona` as their active persona."""
    if persona.user.active_persona_id == persona.pk:
        return True
    last = persona.last_active_user
    return last is not None and last.active_persona_id == persona.pk


def in_use_q(prefix=''):
    """Queryset filter equivalent of `in_use`."""
    return Q(**{f'{prefix}user__active_persona': F(f'{prefix}pk')}) | Q(
        **{f'{prefix}last_active_user__active_persona': F(f'{prefix}pk')},
    )


def owner_plus_q():
    """Personas whose owner has an active Plus subscription."""
    return Q(user__subscription_tier__gte=User.TIER_PLUS) & (
        Q(user__subscription_expires_at__isnull=True) | Q(user__subscription_expires_at__gt=timezone.now())
    )
