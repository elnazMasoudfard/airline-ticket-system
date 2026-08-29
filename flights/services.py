import logging
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from .models import Flight, Seat, SeatClass

logger = logging.getLogger('flights')

COLUMN_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F']

# ترتیب منطقی صف‌بندی ردیف‌ها در بدنه‌ی هواپیما: اکونومی -> بیزینس -> فرست کلاس
CLASS_ROW_ORDER = {
    SeatClass.ClassTypeChoices.ECONOMY: 0,
    SeatClass.ClassTypeChoices.BUSINESS: 1,
    SeatClass.ClassTypeChoices.FIRST: 2,
}


def generate_seats_for_flight(flight):
    """
    برای همه‌ی کلاس‌های صندلی یک پرواز که هنوز صندلی ندارند، صندلی‌ها را
    با ردیف‌های پیوسته (بر اساس ظرفیت واقعی هر کلاس) می‌سازد.

    خروجی: (تعداد صندلی ساخته‌شده, لیست کلاس‌هایی که از قبل صندلی داشتند و رد شدند)
    """
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
                    Seat(seat_class=seat_class, row_number=row, column_letter=letter)
                )
                remaining -= 1
            row += 1

        Seat.objects.bulk_create(seats_to_create)
        created_total += len(seats_to_create)
        next_row = row  # ردیف بعدی از همینجا برای کلاس بعدی ادامه پیدا می‌کند

    if created_total:
        logger.info(f"{created_total} صندلی برای پرواز {flight.flight_number} ساخته شد")
    if skipped:
        logger.info(f"کلاس‌های صندلی زیر از قبل صندلی داشتند و رد شدند: {', '.join(skipped)}")

    return created_total, skipped


def sync_flight_statuses():
    """
    وضعیت پروازها را بر اساس زمان واقعی به‌روزرسانی می‌کند:
    - از ۱ ساعت قبل از حرکت تا لحظه‌ی رسیدن: در حال انجام (ACTIVE)
    - بعد از زمان رسیدن: انجام‌شده (COMPLETED)

    هرگز پروازهای «لغو شده» را دست‌کاری نمی‌کند؛ تصمیم دستی مدیر همیشه اولویت دارد.
    این تابع به‌صورت سبک (bulk update، بدون بارگذاری کامل شیء) اجرا می‌شود، پس
    صدا زدنش در ابتدای هر view که لیست پرواز نشان می‌دهد بی‌خطر و ارزان است.
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