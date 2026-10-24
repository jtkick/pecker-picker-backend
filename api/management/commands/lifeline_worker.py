"""
Run lifeline warnings and alerts every few seconds. Runs as its own compose
service; a failed tick is logged and retried on the next one.
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from api.lifeline import alert_due, warn_due

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send lifeline warnings and alerts as they come due."

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=30, help='Seconds between checks.')
        parser.add_argument('--once', action='store_true', help='Run a single check and exit.')

    def handle(self, *args, interval, once, **options):
        self.stdout.write(f"Lifeline worker checking every {interval:g}s.")
        while True:
            close_old_connections()
            try:
                warned, alerted = warn_due(), alert_due()
                if warned or alerted:
                    logger.info('Lifelines: %d warned, %d alerted', warned, alerted)
            except Exception:
                logger.exception('Lifeline check failed')
            if once:
                return
            time.sleep(interval)
