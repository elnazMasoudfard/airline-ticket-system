import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel
from flights.models import Seat, SeatClass


class ReservationQuerySet(models.QuerySet):
    def active(self):
        """Reservations that still hold seats, including unpaid ones."""
        return self.filter(
            status__in=[
                Reservation.StatusChoices.PENDING_PAYMENT,
                Reservation.StatusChoices.RESERVED,
            ]
        )

    def cancelled(self):
        """Cancelled reservations only."""
        return self.filter(status=Reservation.StatusChoices.CANCELLED)

    def for_user(self, user):
        """Bookings for a specific user"""
        return self.filter(user=user)

    def with_flight_info(self):
        """Use `select_related` to display flight information without N+1 queries."""
        return self.select_related(
            'seat_class__flight__route__origin',
            'seat_class__flight__route__destination',
            'seat_class__flight__airline',
        )


ReservationManager = models.Manager.from_queryset(ReservationQuerySet)


class Reservation(TimeStampedModel):
    class StatusChoices(models.TextChoices):
        PENDING_PAYMENT = 'pending_payment', 'در انتظار پرداخت'
        RESERVED = 'reserved', 'رزرو شده (قطعی)'
        CANCELLED = 'cancelled', 'کنسل شده'

    class CancellationReason(models.TextChoices):
        USER = 'user', 'لغو توسط کاربر'
        TIMEOUT = 'timeout', 'پایان مهلت پرداخت'
        FLIGHT_CANCELLED = 'flight_cancelled', 'لغو پرواز'

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

    payment_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="مهلت پرداخت"
    )

    # NOTE: while the reservation is still pending this holds the amount
    # that must be paid; it becomes the "paid" amount after payment.
    total_paid_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="مبلغ رزرو/پرداخت‌شده (تومان)"
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="تاریخ و ساعت پرداخت"
    )

    refund_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name="مبلغ استردادشده (تومان)"
    )

    # A new reservation must never become "final" by accident:
    # it starts as pending and only the payment flow makes it RESERVED.
    status = models.CharField(
        max_length=20,
        choices=StatusChoices.choices,
        default=StatusChoices.PENDING_PAYMENT,
        verbose_name="وضعیت رزرو"
    )

    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="تاریخ و ساعت کنسلی"
    )

    cancellation_reason = models.CharField(
        max_length=20,
        choices=CancellationReason.choices,
        blank=True,
        default='',
        verbose_name="دلیل کنسلی"
    )

    objects = ReservationManager()

    class Meta:
        verbose_name = "رزرو"
        verbose_name_plural = "رزروها"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            # Used by the expiry job.
            models.Index(fields=['status', 'payment_expires_at']),
        ]

    @property
    def is_payment_expired(self):
        """
        True when a pending reservation can no longer be paid.
        A pending reservation without a deadline is treated as expired,
        so it can never hold seats forever.
        """
        if self.status != self.StatusChoices.PENDING_PAYMENT:
            return False
        return (
            self.payment_expires_at is None
            or self.payment_expires_at <= timezone.now()
        )

    def clean(self):
        super().clean()
        if self.seats_count < 1:
            raise ValidationError({'seats_count': "تعداد صندلی باید حداقل یک باشد."})
        # Passenger count is required only for a paid/final reservation.
        if self.pk and self.status == self.StatusChoices.RESERVED:
            if self.seats_count != self.passengers.count():
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
        # ASCII digits only (\d also matches Persian/Arabic digits) and \Z
        # instead of $ so a trailing newline is rejected.
        validators=[RegexValidator(r'^[0-9]{10}\Z', 'کد ملی باید دقیقاً ۱۰ رقم (انگلیسی) باشد')],
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
    The link between a reservation and the specific seats allocated to it.
    Each seat can belong to only one reservation (OneToOne relationship on the seat).
    The row is deleted when the reservation is cancelled/expired, which frees the seat.
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