from decimal import Decimal

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F


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

    def deposit(self, amount: Decimal) -> None:
        """شارژ کیف پول به‌صورت اتمیک."""
        if amount <= 0:
            raise ValueError("مبلغ شارژ باید بیشتر از صفر باشد.")
        CustomUser.objects.filter(pk=self.pk).update(
            wallet_balance=F('wallet_balance') + amount
        )
        self.refresh_from_db(fields=['wallet_balance'])

    def withdraw(self, amount: Decimal) -> None:
        """
        کسر از کیف پول به‌صورت اتمیک.
        از موجودی منفی و race condition (دو تراکنش هم‌زمان) جلوگیری می‌کند.
        """
        if amount <= 0:
            raise ValueError("مبلغ برداشت باید بیشتر از صفر باشد.")
        updated = CustomUser.objects.filter(
            pk=self.pk, wallet_balance__gte=amount
        ).update(wallet_balance=F('wallet_balance') - amount)
        if not updated:
            raise ValueError("موجودی کیف پول کافی نیست.")
        self.refresh_from_db(fields=['wallet_balance'])