from decimal import Decimal, ROUND_HALF_UP

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F
from django.utils import timezone

from core.models import TimeStampedModel


class Airport(TimeStampedModel):
    name = models.CharField(max_length=120, verbose_name="نام فرودگاه")
    city = models.CharField(max_length=80, db_index=True, verbose_name="شهر")
    iata_code = models.CharField(max_length=3, unique=True, verbose_name="کد یاتا (IATA)")

    class Meta:
        verbose_name = "فرودگاه"
        verbose_name_plural = "فرودگاه‌ها"
        ordering = ['city', 'name']

    def __str__(self):
        return f"{self.city} ({self.iata_code}) - {self.name}"


class Airline(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="نام شرکت هواپیمایی")
    logo = models.ImageField(upload_to='airlines/logos/', null=True, blank=True, verbose_name="لوگو")

    class Meta:
        verbose_name = "شرکت هواپیمایی"
        verbose_name_plural = "شرکت‌های هواپیمایی"

    def __str__(self):
        return self.name


class Route(TimeStampedModel):
    origin = models.ForeignKey(
        Airport,
        on_delete=models.CASCADE,
        related_name='departing_routes',
        verbose_name="مبدا"
    )
    destination = models.ForeignKey(
        Airport,
        on_delete=models.CASCADE,
        related_name='arriving_routes',
        verbose_name="مقصد"
    )

    class Meta:
        verbose_name = "مسیر پروازی"
        verbose_name_plural = "مسیرهای پروازی"
        constraints = [
            models.UniqueConstraint(fields=['origin', 'destination'], name='unique_route_pair'),
            models.CheckConstraint(
                condition=~models.Q(origin=models.F('destination')),
                name='prevent_self_route'
            )
        ]

    def __str__(self):
        return f"{self.origin.city} به {self.destination.city}"


class FlightQuerySet(models.QuerySet):
    def upcoming(self):
        """فقط پروازهای برنامه‌ریزی‌شده‌ای که هنوز پرواز نکرده‌اند."""
        return self.filter(
            status=Flight.StatusChoices.SCHEDULED,
            departure_datetime__gt=timezone.now(),
        )

    def by_route(self, origin=None, destination=None):
        """فیلتر بر اساس مبدا و/یا مقصد (هر کدام اختیاری)."""
        queryset = self
        if origin:
            queryset = queryset.filter(route__origin=origin)
        if destination:
            queryset = queryset.filter(route__destination=destination)
        return queryset

    def on_date(self, date):
        """فقط پروازهایی که در یک تاریخ مشخص حرکت می‌کنند."""
        return self.filter(departure_datetime__date=date)

    def with_route_info(self):
        """select_related استاندارد برای نمایش مسیر/ایرلاین بدون N+1 query."""
        return self.select_related('route__origin', 'route__destination', 'airline')


FlightManager = models.Manager.from_queryset(FlightQuerySet)


class Flight(TimeStampedModel):
    class StatusChoices(models.TextChoices):
        SCHEDULED = 'scheduled', 'برنامه‌ریزی شده'
        ACTIVE = 'active', 'در حال انجام'
        COMPLETED = 'completed', 'انجام شده'
        CANCELLED = 'cancelled', 'لغو شده'

    class AirplaneTypeChoices(models.TextChoices):
        A320 = 'a320', 'ایرباس A320'
        A321 = 'a321', 'ایرباس A321'
        A330 = 'a330', 'ایرباس A330'
        B737 = 'b737', 'بوئینگ 737'
        B777 = 'b777', 'بوئینگ 777'
        B787 = 'b787', 'بوئینگ 787 دریم‌لاینر'
        ATR72 = 'atr72', 'ATR 72'
        MD88 = 'md88', 'مک‌دانل داگلاس MD-88'
        FOKKER100 = 'fokker100', 'فوکر 100'

    flight_number = models.CharField(max_length=20, unique=True, verbose_name="شماره پرواز")
    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name='flights', verbose_name="مسیر")
    airline = models.ForeignKey(Airline, on_delete=models.PROTECT, related_name='flights', verbose_name="ایرلاین")
    airplane_type = models.CharField(
        max_length=15,
        choices=AirplaneTypeChoices.choices,
        verbose_name="نوع هواپیما"
    )
    departure_datetime = models.DateTimeField(db_index=True, verbose_name="تاریخ و ساعت حرکت")
    arrival_datetime = models.DateTimeField(verbose_name="تاریخ و ساعت رسیدن")
    base_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="قیمت پایه (تومان)"
    )
    cancellation_penalty_percent = models.PositiveSmallIntegerField(
        default=20,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name="درصد جریمه کنسلی"
    )
    status = models.CharField(
        max_length=15,
        choices=StatusChoices.choices,
        default=StatusChoices.SCHEDULED,
        verbose_name="وضعیت پرواز"
    )

    objects = FlightManager()

    class Meta:
        verbose_name = "پرواز"
        verbose_name_plural = "پروازها"
        ordering = ['departure_datetime']
        indexes = [
            models.Index(fields=['route', 'departure_datetime']),
            models.Index(fields=['status']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(arrival_datetime__gt=models.F('departure_datetime')),
                name='arrival_after_departure'
            )
        ]

    def __str__(self):
        return f"{self.flight_number} | {self.airline.name} ({self.route})"

    @property
    def total_available_seats(self):
        return sum(seat.available_seats for seat in self.seat_classes.all())


class SeatClassQuerySet(models.QuerySet):
    def available(self):
        """فقط کلاس‌های صندلی‌ای که هنوز حداقل یک صندلی خالی دارند."""
        return self.filter(available_seats__gt=0)


SeatClassManager = models.Manager.from_queryset(SeatClassQuerySet)


class SeatClass(TimeStampedModel):
    class ClassTypeChoices(models.TextChoices):
        ECONOMY = 'economy', 'اکونومی'
        BUSINESS = 'business', 'بیزینس'
        FIRST = 'first', 'فرست کلاس'

    flight = models.ForeignKey(Flight, on_delete=models.CASCADE, related_name='seat_classes', verbose_name="پرواز")
    class_type = models.CharField(max_length=15, choices=ClassTypeChoices.choices, verbose_name="کلاس صندلی")
    price_multiplier = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal('1.00'),
        validators=[MinValueValidator(Decimal('1.00'))],
        verbose_name="ضریب قیمت"
    )
    capacity = models.PositiveIntegerField(verbose_name="ظرفیت کل")
    available_seats = models.PositiveIntegerField(verbose_name="صندلی‌های موجود")

    objects = SeatClassManager()

    class Meta:
        verbose_name = "کلاس پروازی صندلی"
        verbose_name_plural = "کلاس‌های پروازی صندلی"
        constraints = [
            models.UniqueConstraint(fields=['flight', 'class_type'], name='unique_flight_seat_class'),
            models.CheckConstraint(
                condition=models.Q(available_seats__lte=models.F('capacity')),
                name='available_seats_lte_capacity'
            )
        ]

    def __str__(self):
        return f"{self.flight.flight_number} - {self.get_class_type_display()} ({self.available_seats}/{self.capacity})"

    @property
    def final_price(self) -> Decimal:
        return (self.flight.base_price * self.price_multiplier).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

    def reserve_seats(self, count: int) -> None:
        """
        به‌صورت اتمیک تعداد مشخصی صندلی رزرو می‌کند (فقط شمارنده‌ی کلی).
        برای رزرو صندلی‌های مشخص از Seat.reserve_specific_seats استفاده کنید.
        """
        if count < 1:
            raise ValueError("تعداد صندلی باید حداقل ۱ باشد.")
        updated = SeatClass.objects.filter(
            pk=self.pk, available_seats__gte=count
        ).update(available_seats=F('available_seats') - count)
        if not updated:
            raise ValueError("ظرفیت کافی برای این کلاس پروازی وجود ندارد.")
        self.refresh_from_db(fields=['available_seats'])

    def release_seats(self, count: int) -> None:
        """صندلی‌های آزادشده (مثلاً بعد از کنسلی رزرو) را به‌صورت اتمیک برمی‌گرداند."""
        if count < 1:
            raise ValueError("تعداد صندلی باید حداقل ۱ باشد.")
        SeatClass.objects.filter(pk=self.pk).update(
            available_seats=F('available_seats') + count
        )
        self.refresh_from_db(fields=['available_seats'])


class Seat(TimeStampedModel):
    """
    یک صندلی مشخص در یک کلاس پروازی خاص (مثلاً ردیف ۱۲، ستون C).
    این مدل امکان انتخاب صندلی مشخص توسط کاربر و تضمین کنار هم بودن
    صندلی‌ها برای رزروهای گروهی را فراهم می‌کند.
    """
    seat_class = models.ForeignKey(
        SeatClass, on_delete=models.CASCADE, related_name='seats', verbose_name="کلاس صندلی"
    )
    row_number = models.PositiveSmallIntegerField(verbose_name="شماره ردیف")
    column_letter = models.CharField(max_length=1, verbose_name="حرف ستون")
    is_available = models.BooleanField(default=True, verbose_name="در دسترس")

    class Meta:
        verbose_name = "صندلی"
        verbose_name_plural = "صندلی‌ها"
        ordering = ['row_number', 'column_letter']
        constraints = [
            models.UniqueConstraint(
                fields=['seat_class', 'row_number', 'column_letter'], name='unique_seat_per_class'
            )
        ]

    @property
    def seat_number(self) -> str:
        return f"{self.row_number}{self.column_letter}"

    def __str__(self):
        return f"{self.seat_class} - {self.seat_number}"