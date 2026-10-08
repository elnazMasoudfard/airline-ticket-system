import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from django.utils import timezone

from core.models import TimeStampedModel

logger = logging.getLogger('accounts')


class CustomUserManager(BaseUserManager):
    def create_user(self, username, email=None, password=None, **extra_fields):
        if not username:
            raise ValueError("نام کاربری الزامی است.")
        if email:
            email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError("کاربر سوپریوزر باید is_staff=True داشته باشد.")
        if extra_fields.get('is_superuser') is not True:
            raise ValueError("کاربر سوپریوزر باید is_superuser=True داشته باشد.")

        return self.create_user(username, email, password, **extra_fields)


class CustomUser(AbstractUser):
    phone_regex = RegexValidator(
        regex=r'^09\d{9}$',
        message="شماره موبایل باید با 09 شروع شده و ۱۱ رقم باشد."
    )
    email = models.EmailField(
        unique=True,
        null=True,
        blank=True,
        verbose_name="ایمیل"
    )
    phone_number = models.CharField(
        validators=[phone_regex],
        max_length=11,
        unique=True,
        null=True,
        blank=True,
        verbose_name="شماره موبایل"
    )
    wallet_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="موجودی کیف پول (تومان)"
    )
    phone_verified = models.BooleanField(default=False, verbose_name="تایید شماره موبایل")
    email_verified = models.BooleanField(default=False, verbose_name="تایید ایمیل")

    objects = CustomUserManager()

    class Meta:
        verbose_name = "کاربر"
        verbose_name_plural = "کاربران"
        ordering = ['username']

    def __str__(self):
        return f"{self.username} ({self.get_full_name() or 'بدون نام'})"

    # ------------------------------------------------------------------
    # Wallet. EVERY balance change must go through deposit()/withdraw(),
    # so that it is applied atomically AND recorded in WalletTransaction.
    # ------------------------------------------------------------------
    def _apply_wallet_change(self, delta, kind, reference, description):
        """
        Lock the user row, change the balance and write the ledger entry in one
        transaction. `delta` is positive for a deposit and negative for a
        withdrawal. Raises ValueError when the balance would become negative.
        """
        delta = Decimal(str(delta)).quantize(Decimal('0.01'))

        with transaction.atomic():
            locked = CustomUser.objects.select_for_update().get(pk=self.pk)
            balance_before = locked.wallet_balance
            balance_after = balance_before + delta

            if balance_after < 0:
                raise ValueError("موجودی کیف پول کافی نیست.")

            CustomUser.objects.filter(pk=self.pk).update(wallet_balance=balance_after)
            entry = WalletTransaction.objects.create(
                user=locked,
                kind=kind,
                amount=delta,
                balance_before=balance_before,
                balance_after=balance_after,
                reference=reference,
                description=description,
            )

            # Written to the log only after the whole transaction committed.
            transaction.on_commit(lambda: self._log_wallet_entry(entry))

        self.wallet_balance = balance_after
        return entry

    def _log_wallet_entry(self, entry):
        Kind = WalletTransaction.KindChoices
        labels = {
            Kind.CHARGE: "شارژ کیف پول",
            Kind.PAYMENT: "برداشت از کیف پول (پرداخت بلیت)",
            Kind.REFUND: "بازگشت وجه به کیف پول",
            Kind.ADJUSTMENT: "اصلاح دستی کیف پول",
        }
        message = (
            f"{labels.get(entry.kind, entry.kind)}: user={self.username}, "
            f"amount={abs(entry.amount)}, new_balance={entry.balance_after}"
        )
        if entry.reference:
            message += f", reference={entry.reference}"
        logger.info(message)

    def deposit(self, amount, *, kind=None, reference='', description=''):
        """شارژ/واریز به کیف پول به‌صورت اتمیک؛ هر واریز یک تراکنش ثبت می‌کند."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("مبلغ شارژ باید بیشتر از صفر باشد.")
        return self._apply_wallet_change(
            amount,
            kind or WalletTransaction.KindChoices.CHARGE,
            reference,
            description,
        )

    def withdraw(self, amount, *, kind=None, reference='', description=''):
        """
        Atomic wallet deduction (never produces a negative balance, safe against
        simultaneous transactions). Every withdrawal writes one ledger entry.
        """
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("مبلغ برداشت باید بیشتر از صفر باشد.")
        return self._apply_wallet_change(
            -amount,
            kind or WalletTransaction.KindChoices.PAYMENT,
            reference,
            description,
        )


class WalletTransaction(TimeStampedModel):
    """
    Append-only ledger of every wallet change.
    Invariant: balance_after == balance_before + amount.
    `amount` is positive for money coming in and negative for money going out.
    `reference` is the related booking reference (PNR), if any; it is a plain
    text field so that accounts does not depend on the tickets app.
    """

    class KindChoices(models.TextChoices):
        CHARGE = 'charge', 'شارژ کیف پول'
        PAYMENT = 'payment', 'پرداخت بلیت'
        REFUND = 'refund', 'استرداد'
        ADJUSTMENT = 'adjustment', 'اصلاح دستی'

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.PROTECT,
        related_name='wallet_transactions',
        verbose_name="کاربر"
    )
    kind = models.CharField(
        max_length=20,
        choices=KindChoices.choices,
        verbose_name="نوع تراکنش"
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="مبلغ (مثبت = واریز، منفی = برداشت)"
    )
    balance_before = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="موجودی قبل"
    )
    balance_after = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name="موجودی بعد"
    )
    reference = models.CharField(
        max_length=40,
        blank=True,
        db_index=True,
        verbose_name="کد رزرو مرتبط"
    )
    description = models.CharField(max_length=200, blank=True, verbose_name="توضیح")

    class Meta:
        verbose_name = "تراکنش کیف پول"
        verbose_name_plural = "تراکنش‌های کیف پول"
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', '-created_at']),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("تراکنش‌های کیف پول قابل ویرایش نیستند.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("تراکنش‌های کیف پول قابل حذف نیستند.")

    def __str__(self):
        return f"{self.user.username} · {self.get_kind_display()} · {self.amount}"


class PhoneVerificationCode(models.Model):
    """
    A 6-digit one-time code to verify the user's mobile number.
    Since there is no actual SMS gateway, the sending process is simulated, and the code is printed to the console/server log.
    It is valid for a maximum of 10 minutes.
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='phone_verification_codes',
        verbose_name="کاربر"
    )
    code = models.CharField(max_length=6, editable=False, verbose_name="کد تایید")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ایجاد")
    used_at = models.DateTimeField(null=True, blank=True, verbose_name="تاریخ استفاده")

    class Meta:
        verbose_name = "کد تایید پیامکی"
        verbose_name_plural = "کدهای تایید پیامکی"
        ordering = ['-created_at']

    @staticmethod
    def generate_code() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self.generate_code()
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def __str__(self):
        return f"کد تایید پیامکی برای {self.user.username}"


class EmailVerificationToken(models.Model):
    """
    A one-time token for verifying the user's email via a link sent to their email address.
    Each token is valid for a maximum of 24 hours and can be used only once.
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='email_verification_tokens',
        verbose_name="کاربر"
    )
    token = models.CharField(max_length=64, unique=True, editable=False, verbose_name="توکن")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ایجاد")
    used_at = models.DateTimeField(null=True, blank=True, verbose_name="تاریخ استفاده")

    class Meta:
        verbose_name = "توکن تایید ایمیل"
        verbose_name_plural = "توکن‌های تایید ایمیل"
        ordering = ['-created_at']

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.created_at + timedelta(hours=24)

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def __str__(self):
        return f"توکن تایید ایمیل برای {self.user.username}"