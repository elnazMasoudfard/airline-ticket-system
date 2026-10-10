from django.core.management.base import BaseCommand

from tickets.services import expire_pending_reservations


class Command(BaseCommand):
    help = "Cancel unpaid reservations whose payment window is over and release their seats."

    def handle(self, *args, **options):
        count = expire_pending_reservations()
        self.stdout.write(self.style.SUCCESS(f"{count} reservation(s) expired."))