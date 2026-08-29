from django.core.management.base import BaseCommand

from flights.services import sync_flight_statuses


class Command(BaseCommand):
    help = (
        "وضعیت پروازها را بر اساس زمان واقعی به‌روزرسانی می‌کند "
        "(SCHEDULED -> ACTIVE -> COMPLETED). برای اجرای دوره‌ای با cron واقعی مناسب است."
    )

    def handle(self, *args, **options):
        activated, completed = sync_flight_statuses()
        self.stdout.write(
            self.style.SUCCESS(
                f"{activated} پرواز فعال شد، {completed} پرواز به‌عنوان انجام‌شده علامت خورد."
            )
        )