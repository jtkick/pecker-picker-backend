from functools import reduce
from django.db.models import Q, QuerySet
from .models import Kink, User


def find_similar_users(user: User, qs: QuerySet | None = None) -> QuerySet:
    """
    Return users who share at least one kink with `user`, accounting for the
    kink hierarchy (ancestors and descendants of each kink are treated as
    related matches).

    The returned queryset is unordered. Call rank_by_kink_similarity() if you
    need users sorted by degree of overlap.
    """
    if not user.kinks:
        return User.objects.none()

    if qs is None:
        qs = User.objects.all()

    qs = qs.exclude(pk=user.pk)
    expanded = Kink.related_names(user.kinks)
    if not expanded:
        return User.objects.none()

    overlap_q = reduce(lambda a, b: a | b, (Q(kinks__contains=[k]) for k in expanded))
    return qs.filter(overlap_q)


def rank_by_kink_similarity(user: User, qs: QuerySet | None = None) -> list[User]:
    """
    Return users sorted by number of kink overlaps with `user` (descending).
    Hierarchy-aware: a match on a parent or child kink counts.

    Returns a plain list because the sort requires Python-side evaluation
    (JSON array intersection isn't expressible in portable SQL).
    """
    candidate_qs = find_similar_users(user, qs)
    expanded = Kink.related_names(user.kinks)

    def overlap_count(other: User) -> int:
        return len(expanded.intersection(other.kinks))

    return sorted(candidate_qs, key=overlap_count, reverse=True)
