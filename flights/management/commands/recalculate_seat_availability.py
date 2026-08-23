from django.core.management.base import BaseCommand
from django.db.models import Sum

from flights.models import Seat, SeatClass
from tickets.models import Reservation, ReservationSeat


class Command(BaseCommand):
    help = (
        "بازمحاسبه‌ی available_seats و is_available صندلی‌ها بر اساس رزروهای فعال واقعی. "
        "برای اصلاح دیتایی که به‌دلیل ویرایش مستقیم status از پنل ادمین (به‌جای دکمه‌ی کنسل سایت) "
        "از حالت واقعی خارج شده استفاده می‌شود."
    )

    def handle(self, *args, **options):
        fixed_seat_classes = 0

        for seat_class in SeatClass.objects.all():
            booked = Reservation.objects.filter(
                seat_class=seat_class,
                status=Reservation.StatusChoices.RESERVED,
            ).aggregate(total=Sum('seats_count'))['total'] or 0

            correct_available = seat_class.capacity - booked

            if seat_class.available_seats != correct_available:
                self.stdout.write(
                    f"{seat_class}: {seat_class.available_seats} -> {correct_available}"
                )
                seat_class.available_seats = correct_available
                seat_class.save(update_fields=['available_seats'])
                fixed_seat_classes += 1

            # هماهنگ‌سازی وضعیت تک‌تک صندلی‌ها (اگر برای این کلاس صندلی ساخته شده باشند)
            if seat_class.seats.exists():
                Seat.objects.filter(seat_class=seat_class).update(is_available=True)
                active_seat_ids = ReservationSeat.objects.filter(
                    reservation__seat_class=seat_class,
                    reservation__status=Reservation.StatusChoices.RESERVED,
                ).values_list('seat_id', flat=True)
                Seat.objects.filter(id__in=active_seat_ids).update(is_available=False)

        self.stdout.write(self.style.SUCCESS(f"{fixed_seat_classes} کلاس صندلی اصلاح شد."))
