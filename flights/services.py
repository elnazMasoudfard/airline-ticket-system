import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import Flight, Seat, SeatClass

logger = logging.getLogger('flights')

COLUMN_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F']

# Logical arrangement of cabin rows: Economy -> Business -> First Class
CLASS_ROW_ORDER = {
    SeatClass.ClassTypeChoices.ECONOMY: 0,
    SeatClass.ClassTypeChoices.BUSINESS: 1,
    SeatClass.ClassTypeChoices.FIRST: 2,
}


def generate_seats_for_flight(flight):
    """
    For all seat classes of a flight that do not yet have seats, it creates seats
    in contiguous rows (based on the actual capacity of each class).

    Output: (Number of seats created, list of classes that already had seats and were skipped)
    """
    with transaction.atomic():
        seat_classes = list(flight.seat_classes.all())
        seat_classes.sort(key=lambda sc: CLASS_ROW_ORDER.get(sc.class_type, 99))

        existing_max_row = Seat.objects.filter(
            seat_class__flight=flight
        ).aggregate(Max('row_number'))['row_number__max'] or 0
        next_row = existing_max_row + 1

        created_total = 0
        skipped = []

        for seat_class in seat_classes:
            if seat_class.seats.exists():
                skipped.append(str(seat_class))
                continue

            seats_to_create = []
            remaining = seat_class.capacity
            row = next_row

            while remaining > 0:
                for letter in COLUMN_LETTERS:
                    if remaining <= 0:
                        break

                    seats_to_create.append(
                        Seat(
                            seat_class=seat_class,
                            row_number=row,
                            column_letter=letter
                        )
                    )
                    remaining -= 1

                row += 1

            Seat.objects.bulk_create(seats_to_create)
            created_total += len(seats_to_create)
            next_row = row

        if created_total:
            logger.info(
                f"{created_total} صندلی برای پرواز {flight.flight_number} ساخته شد"
            )

        if skipped:
            logger.info(
                f"کلاس‌های صندلی زیر از قبل صندلی داشتند و رد شدند: "
                f"{', '.join(skipped)}"
            )

        return created_total, skipped


def sync_flight_statuses():
    """
    Updates flight statuses in real-time:
    - From 1 hour prior to departure until arrival: ACTIVE
    - After the arrival time: COMPLETED

    It never alters "Cancelled" flights; a manual decision by the manager always takes precedence.
    This function executes as a bulk update (without fully loading the object), so
    calling it at the start of any view displaying a flight list is safe and computationally inexpensive.
    """
    now = timezone.now()

    activated_count = Flight.objects.filter(
        status=Flight.StatusChoices.SCHEDULED,
        departure_datetime__lte=now + timedelta(hours=1),
        arrival_datetime__gt=now,
    ).update(status=Flight.StatusChoices.ACTIVE)

    completed_count = Flight.objects.filter(
        status__in=[Flight.StatusChoices.SCHEDULED, Flight.StatusChoices.ACTIVE],
        arrival_datetime__lte=now,
    ).update(status=Flight.StatusChoices.COMPLETED)

    if activated_count:
        logger.info(f"{activated_count} پرواز به وضعیت «در حال انجام» تغییر یافت")
    if completed_count:
        logger.info(f"{completed_count} پرواز به وضعیت «انجام شده» تغییر یافت")

    return activated_count, completed_count