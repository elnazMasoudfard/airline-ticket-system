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
        # Lock the flight row: a double click (or two managers) can no longer
        # create the same seats twice, the second call waits and then skips.
        Flight.objects.select_for_update().get(pk=flight.pk)

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


def sync_seats_with_capacity(seat_class):
    """
    Keep the number of Seat rows equal to the class capacity, for a class that
    ALREADY has seats (a class whose seats were never generated is left alone).

    - capacity went up:   new seats are added after the existing ones. If this class
      owns the last row of the cabin the row is simply continued, otherwise the new
      seats go into new rows after the last row of the flight, so rows of different
      classes never overlap.
    - capacity went down: FREE seats are removed, starting from the back of the
      class. Booked seats are never touched; callers must have checked that the new
      capacity is not smaller than the number of booked seats.

    Returns (added, removed).
    """
    with transaction.atomic():
        # Lock order: SeatClass -> Seat, the same as the booking flow.
        locked = SeatClass.objects.select_for_update().get(pk=seat_class.pk)
        existing = list(locked.seats.order_by('row_number', 'column_letter'))
        if not existing:
            return 0, 0

        difference = locked.capacity - len(existing)

        if difference > 0:
            flight_last_row = Seat.objects.filter(
                seat_class__flight_id=locked.flight_id
            ).aggregate(Max('row_number'))['row_number__max'] or 0
            last = existing[-1]

            if last.row_number == flight_last_row and last.column_letter in COLUMN_LETTERS:
                row = last.row_number
                column = COLUMN_LETTERS.index(last.column_letter) + 1
            else:
                row = flight_last_row + 1
                column = 0

            new_seats = []
            for _ in range(difference):
                if column >= len(COLUMN_LETTERS):
                    row += 1
                    column = 0
                new_seats.append(
                    Seat(seat_class=locked, row_number=row, column_letter=COLUMN_LETTERS[column])
                )
                column += 1

            Seat.objects.bulk_create(new_seats)
            logger.info(f"{difference} صندلی به {locked} اضافه شد (هماهنگی با ظرفیت جدید)")
            return difference, 0

        if difference < 0:
            to_remove = -difference
            candidate_ids = list(
                Seat.objects.filter(seat_class=locked, is_available=True)
                .order_by('-row_number', '-column_letter')
                .values_list('pk', flat=True)[:to_remove]
            )
            removable = list(
                Seat.objects.select_for_update().filter(pk__in=candidate_ids, is_available=True)
            )
            if len(removable) < to_remove:
                raise ValueError(
                    "برای کاهش ظرفیت به اندازه‌ی کافی صندلی آزاد وجود ندارد."
                )
            Seat.objects.filter(pk__in=[seat.pk for seat in removable]).delete()
            logger.info(f"{to_remove} صندلی آزاد از {locked} حذف شد (هماهنگی با ظرفیت جدید)")
            return 0, to_remove

        return 0, 0


def resync_flight_seats(flight):
    """sync_seats_with_capacity for every class of the flight. Returns (added, removed)."""
    added_total = removed_total = 0
    for seat_class in SeatClass.objects.filter(flight=flight):
        added, removed = sync_seats_with_capacity(seat_class)
        added_total += added
        removed_total += removed
    return added_total, removed_total


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