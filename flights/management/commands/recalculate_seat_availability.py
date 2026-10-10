from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from flights.models import Seat, SeatClass
from tickets.models import Reservation, ReservationSeat
from tickets.services import expire_pending_reservations


class Command(BaseCommand):
    help = (
        "بازمحاسبه‌ی available_seats و is_available صندلی‌ها بر اساس رزروهایی که واقعاً صندلی نگه داشته‌اند: "
        "رزروهای قطعی و رزروهای در انتظار پرداخت که مهلتشان تمام نشده است. "
        "برای اصلاح داده‌ای که به‌دلیل ویرایش مستقیم دیتابیس یا پنل ادمین از حالت واقعی خارج شده استفاده می‌شود. "
        "با --dry-run فقط اختلاف‌ها نمایش داده می‌شود و چیزی تغییر نمی‌کند."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="فقط اختلاف‌ها را نشان بده و چیزی را تغییر نده.",
        )

    @staticmethod
    def holding_reservations():
        """Reservations that really hold seats right now."""
        return Reservation.objects.filter(
            Q(status=Reservation.StatusChoices.RESERVED)
            | Q(
                status=Reservation.StatusChoices.PENDING_PAYMENT,
                payment_expires_at__gt=timezone.now(),
            )
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if not dry_run:
            # Overdue unpaid reservations must be released first; they no longer hold seats.
            expired = expire_pending_reservations()
            if expired:
                self.stdout.write(f"{expired} رزرو پرداخت‌نشده‌ی منقضی‌شده آزاد شد.")

        fixed_classes = 0
        fixed_seats = 0

        for seat_class_id in SeatClass.objects.values_list('pk', flat=True):
            with transaction.atomic():
                # Same lock order as the booking flow: SeatClass -> Seat.
                seat_class = SeatClass.objects.select_for_update().get(pk=seat_class_id)
                holding = self.holding_reservations().filter(seat_class=seat_class)

                booked = holding.aggregate(total=Sum('seats_count'))['total'] or 0
                correct_available = seat_class.capacity - booked
                if correct_available < 0:
                    self.stdout.write(self.style.WARNING(
                        f"{seat_class}: تعداد صندلی‌های رزروشده ({booked}) از ظرفیت بیشتر است."
                    ))
                    correct_available = 0

                if seat_class.available_seats != correct_available:
                    self.stdout.write(
                        f"{seat_class}: {seat_class.available_seats} -> {correct_available}"
                    )
                    fixed_classes += 1
                    if not dry_run:
                        SeatClass.objects.filter(pk=seat_class.pk).update(
                            available_seats=correct_available, updated_at=timezone.now()
                        )

                # Per-seat flags (only for classes whose seats were generated).
                if seat_class.seats.exists():
                    held_ids = set(
                        ReservationSeat.objects.filter(reservation__in=holding)
                        .values_list('seat_id', flat=True)
                    )
                    wrongly_free = Seat.objects.filter(
                        seat_class=seat_class, is_available=True, pk__in=held_ids
                    )
                    wrongly_held = Seat.objects.filter(
                        seat_class=seat_class, is_available=False
                    ).exclude(pk__in=held_ids)

                    mismatched = wrongly_free.count() + wrongly_held.count()
                    if mismatched:
                        self.stdout.write(f"{seat_class}: وضعیت {mismatched} صندلی اصلاح می‌شود.")
                        fixed_seats += mismatched
                        if not dry_run:
                            wrongly_free.update(is_available=False)
                            wrongly_held.update(is_available=True)

        suffix = " (حالت آزمایشی؛ چیزی تغییر نکرد)" if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{fixed_classes} کلاس صندلی و {fixed_seats} صندلی اصلاح شد.{suffix}"
        ))