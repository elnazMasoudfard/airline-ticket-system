from django.core.management.base import BaseCommand

from tickets.services import expire_pending_reservations, warn_expiring_reservations


class Command(BaseCommand):
    help = (
        "Cancel unpaid reservations whose payment window is over and release their seats, "
        "and warn users whose payment window is about to end. Run it every minute."
    )

    def handle(self, *args, **options):
        count = expire_pending_reservations()
        self.stdout.write(self.style.SUCCESS(f"{count} reservation(s) expired."))

        warned = warn_expiring_reservations()
        self.stdout.write(self.style.SUCCESS(f"{warned} expiry warning(s) sent."))