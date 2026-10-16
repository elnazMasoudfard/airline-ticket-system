from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel


class Notification(TimeStampedModel):
    """
    A message for one user ("your payment succeeded", "the flight was cancelled" ...).

    An unread notification has `read_at = NULL`. `url` is an address inside this site
    (a reservation or a flight page) that the notification points to, or ''.

    The app knows nothing about flights or reservations: the apps that create
    notifications (tickets, accounts) pass in the text and the address, so this app
    never imports them and the dependency only goes one way.
    """

    class Kind(models.TextChoices):
        PAYMENT_SUCCESS = 'payment_success', 'پرداخت موفق'
        RESERVATION_EXPIRING = 'reservation_expiring', 'مهلت پرداخت'
        RESERVATION_EXPIRED = 'reservation_expired', 'رزرو منقضی شد'
        FLIGHT_CANCELLED = 'flight_cancelled', 'لغو پرواز'
        REFUND = 'refund', 'استرداد وجه'
        WALLET_CHARGED = 'wallet_charged', 'شارژ کیف پول'

    # The colour of the small label of each kind (classes of static/css/main.css).
    BADGE_CLASSES = {
        Kind.PAYMENT_SUCCESS: 'badge-success',
        Kind.RESERVATION_EXPIRING: 'badge-warning',
        Kind.RESERVATION_EXPIRED: 'badge-muted',
        Kind.FLIGHT_CANCELLED: 'badge-danger',
        Kind.REFUND: 'badge-info',
        Kind.WALLET_CHARGED: 'badge-success',
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name="کاربر",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices, verbose_name="نوع")
    title = models.CharField(max_length=120, verbose_name="عنوان")
    message = models.CharField(max_length=400, blank=True, verbose_name="متن")
    url = models.CharField(max_length=300, blank=True, verbose_name="لینک مرتبط")
    # Names the EVENT ("paid:A1B2C3D4"): the same event can never notify the same user twice,
    # even if the code that reports it runs again. Empty = no such protection.
    dedupe_key = models.CharField(max_length=80, blank=True, verbose_name="کلید جلوگیری از تکرار")
    read_at = models.DateTimeField(null=True, blank=True, verbose_name="زمان خوانده‌شدن")

    class Meta:
        verbose_name = "اعلان"
        verbose_name_plural = "اعلان‌ها"
        ordering = ['-created_at', '-id']
        indexes = [
            # the unread counter shown on every page
            models.Index(fields=['user', 'read_at']),
            # the list page
            models.Index(fields=['user', '-created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'dedupe_key'],
                condition=~Q(dedupe_key=''),
                name='unique_notification_per_event',
            ),
        ]

    @property
    def is_read(self):
        return self.read_at is not None

    @property
    def badge_class(self):
        return self.BADGE_CLASSES.get(self.kind, 'badge-muted')

    def __str__(self):
        return f"{self.user} · {self.get_kind_display()} · {self.title}"