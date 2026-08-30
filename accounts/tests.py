from decimal import Decimal

from django.test import TestCase

from .models import CustomUser


class CustomUserManagerTests(TestCase):
    def test_create_user_requires_username(self):
        with self.assertRaises(ValueError):
            CustomUser.objects.create_user(username='', password='pass12345')

    def test_create_superuser_sets_staff_and_superuser_flags(self):
        user = CustomUser.objects.create_superuser(
            username='admin', email='admin@example.com', password='pass12345'
        )
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_create_superuser_rejects_explicit_false_staff_flag(self):
        with self.assertRaises(ValueError):
            CustomUser.objects.create_superuser(
                username='admin2', password='pass12345', is_staff=False
            )


class WalletTests(TestCase):
    """
    تست منطق کیف پول: دقیقاً همان متدهایی که برای رزرو بلیط و
    استرداد پول کنسلی استفاده می‌شوند.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(username='user1', password='pass12345')

    def test_deposit_increases_balance(self):
        self.user.deposit(Decimal('100000'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, Decimal('100000.00'))

    def test_deposit_rejects_non_positive_amount(self):
        with self.assertRaises(ValueError):
            self.user.deposit(Decimal('0'))
        with self.assertRaises(ValueError):
            self.user.deposit(Decimal('-500'))

    def test_withdraw_decreases_balance(self):
        self.user.deposit(Decimal('100000'))
        self.user.withdraw(Decimal('40000'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, Decimal('60000.00'))

    def test_withdraw_fails_on_insufficient_balance(self):
        with self.assertRaises(ValueError):
            self.user.withdraw(Decimal('1000'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, Decimal('0.00'))

    def test_withdraw_rejects_non_positive_amount(self):
        self.user.deposit(Decimal('10000'))
        with self.assertRaises(ValueError):
            self.user.withdraw(Decimal('-100'))