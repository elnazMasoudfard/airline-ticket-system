import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from core.models import TimeStampedModel
from flights.models import Seat, SeatClass


class Reservation(TimeStampedModel):
    class StatusChoices(models.TextChoices):
        RESERVED = 'reserved', 'رزرو شده (قطعی)'
        CANCELLED = 'cancelled', 'کنسل شده'

    booking_reference = models.CharField(
        max_length=10,
        unique=True,
        editable=False,
        verbose_name="شناسه رزرو (PNR)"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='reservations',
        verbose_name="کاربر رزروکننده"
    )
    seat_class = models.ForeignKey(
        SeatClass,
        on_delete=models.PROTECT,
        related_name='reservations',
        verbose_name="کلاس صندلی"
    )
    seats_count = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="تعداد صندلی"
    )
    total_paid_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="مبلغ پرداخت‌شده (تومان)"
    )
    refund_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="مبلغ استردادشده (تومان)"
    )
    status = models.CharField(
        max_length=15,
        choices=StatusChoices.choices,
        default=StatusChoices.RESERVED,
        verbose_name="وضعیت رزرو"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name="تاریخ و ساعت کنسلی")

    class Meta:
        verbose_name = "رزرو"
        verbose_name_plural = "رزروها"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
        ]

    def clean(self):
        if self.pk and self.seats_count != self.passengers.count():
            raise ValidationError("تعداد صندلی با تعداد مسافران ثبت‌شده مطابقت ندارد.")

    def save(self, *args, **kwargs):
        if not self.booking_reference:
            while True:
                ref = uuid.uuid4().hex[:8].upper()
                if not Reservation.objects.filter(booking_reference=ref).exists():
                    self.booking_reference = ref
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        return f"رزرو {self.booking_reference} - {self.user.username} ({self.get_status_display()})"


class Passenger(TimeStampedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name='passengers',
        verbose_name="رزرو مربوطه"
    )
    first_name = models.CharField(max_length=60, verbose_name="نام")
    last_name = models.CharField(max_length=60, verbose_name="نام خانوادگی")
    national_id = models.CharField(
        max_length=10,
        validators=[RegexValidator(r'^\d{10}$', 'کد ملی باید دقیقاً ۱۰ رقم باشد')],
        verbose_name="کد ملی"
    )

    class Meta:
        verbose_name = "مشخصات مسافر"
        verbose_name_plural = "مشخصات مسافران"
        unique_together = [['reservation', 'national_id']]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.national_id})"


class ReservationSeat(TimeStampedModel):
    """
    پیوند بین یک رزرو و صندلی‌های مشخصی که برای آن رزرو اختصاص یافته‌اند.
    هر صندلی فقط می‌تواند به یک رزرو تعلق داشته باشد (OneToOne روی seat).
    """
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name='reservation_seats',
        verbose_name="رزرو"
    )
    seat = models.OneToOneField(
        Seat,
        on_delete=models.PROTECT,
        related_name='reservation_seat',
        verbose_name="صندلی"
    )

    class Meta:
        verbose_name = "صندلی رزروشده"
        verbose_name_plural = "صندلی‌های رزروشده"

    def __str__(self):
        return f"{self.reservation.booking_reference} - {self.seat.seat_number}"