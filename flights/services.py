from django.db.models import Max

from .models import Seat, SeatClass

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

    return created_total, skipped