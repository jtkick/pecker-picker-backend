from django.core.management.base import BaseCommand, CommandError

from api import areas
from api.models import User


class Command(BaseCommand):
    help = "Places every located account in its areas (run after installing a new areas file)."

    def handle(self, *args, **options):
        if not areas.available():
            raise CommandError(f'No areas file at {areas._path()}; build one with deploy/build_areas.py.')
        users = list(User.objects.exclude(latitude=None).exclude(longitude=None))
        for user in users:
            areas.assign(user)
        User.objects.bulk_update(users, areas.AREA_FIELDS, batch_size=500)
        self.stdout.write(self.style.SUCCESS(f'✓ Placed {len(users)} accounts.'))
