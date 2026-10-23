"""
How people act on what discover shows them, for tuning api.discover's weights.
Counts profile opens and new chats by feed position and by what each side is
looking for. A good ranking concentrates both near the top.
"""
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.discover import INTENT_GROUPS
from api.models import DiscoverEvent

RANK_BUCKETS = [(0, 9), (10, 29), (30, 59), (60, None)]


def _bucket(rank: int) -> str:
    for low, high in RANK_BUCKETS:
        if high is None or rank <= high:
            return f'{low}+' if high is None else f'{low}–{high}'
    return '?'


def _groups(persona) -> str:
    groups = sorted({INTENT_GROUPS.get(t, '?') for t in persona.relationship_types or ()})
    return '/'.join(groups) or 'browsing'


class Command(BaseCommand):
    help = "Discover opens and new chats by feed position and by intent pair."

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Look back this many days (default 30).')

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(days=options['days'])
        events = list(
            DiscoverEvent.objects.filter(created_at__gte=since)
            .select_related('viewer_persona', 'target_persona')
        )
        if not events:
            self.stdout.write('No discover events yet.')
            return

        by_rank = Counter((_bucket(e.rank), e.kind) for e in events)
        self.stdout.write(f'Feed position   opened  messaged  (last {options["days"]} days)')
        for low, high in RANK_BUCKETS:
            label = _bucket(low)
            opened, messaged = by_rank[(label, 'opened')], by_rank[(label, 'messaged')]
            self.stdout.write(f'{label:<14} {opened:>7} {messaged:>9}')

        by_pair = Counter(
            (_groups(e.viewer_persona), _groups(e.target_persona), e.kind) for e in events
        )
        self.stdout.write('\nViewer → shown            opened  messaged')
        for viewer, target in sorted({(v, t) for v, t, _ in by_pair}):
            opened, messaged = by_pair[(viewer, target, 'opened')], by_pair[(viewer, target, 'messaged')]
            self.stdout.write(f'{viewer + " → " + target:<25} {opened:>6} {messaged:>9}')
