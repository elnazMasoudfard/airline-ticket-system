from decimal import Decimal

from django.test import TestCase

from .models import CustomUser
from .views import RequestPhoneVerificationView


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

# ======================================================================
# تست‌های اضافه‌شده: دفتر کل، تایید ایمیل/موبایل، ورود، پروفایل
# ======================================================================
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .models import EmailVerificationToken, PhoneVerificationCode, WalletTransaction

TEST_CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
FLIGHT_LIST = 'flights:flight_list'


class WalletLedgerTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(username='u', password='pass12345')

    def test_each_change_writes_ledger_entry_with_consistent_balances(self):
        self.user.deposit(Decimal('100000'))
        self.user.withdraw(Decimal('30000'), reference='PNR123')
        entries = list(WalletTransaction.objects.filter(user=self.user).order_by('id'))
        self.assertEqual(len(entries), 2)
        for e in entries:
            self.assertEqual(e.balance_after, e.balance_before + e.amount)
        self.assertEqual(entries[1].amount, Decimal('-30000.00'))
        self.assertEqual(entries[1].reference, 'PNR123')
        self.assertEqual(entries[1].balance_after, Decimal('70000.00'))

    def test_failed_withdraw_writes_no_ledger_entry(self):
        with self.assertRaises(ValueError):
            self.user.withdraw(Decimal('1000'))
        self.assertEqual(WalletTransaction.objects.count(), 0)

    def test_ledger_entries_cannot_be_edited_or_deleted(self):
        entry = self.user.deposit(Decimal('5000'))
        entry.description = 'x'
        with self.assertRaises(ValueError):
            entry.save()
        with self.assertRaises(ValueError):
            entry.delete()


@override_settings(CACHES=TEST_CACHES)
class RegistrationAndLoginTests(TestCase):
    def setUp(self):
        cache.clear()

    def _register(self, **overrides):
        data = {
            'username': 'newuser', 'email': 'New@Example.com', 'phone_number': '',
            'password1': 'Str0ng-pass-123', 'password2': 'Str0ng-pass-123',
        }
        data.update(overrides)
        return self.client.post(reverse('accounts:register'), data)

    def test_register_creates_user_logs_in_and_sends_email(self):
        response = self._register()
        self.assertRedirects(response, reverse(FLIGHT_LIST), fetch_redirect_response=False)
        user = CustomUser.objects.get(username='newuser')
        self.assertEqual(user.email, 'new@example.com')  # lowercase شده
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_register_rejects_email_differing_only_by_case(self):
        CustomUser.objects.create_user(username='a', email='a@x.com', password='pass12345')
        response = self._register(username='b', email='A@X.com')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomUser.objects.filter(username='b').exists())

    def test_create_user_lowercases_email(self):
        user = CustomUser.objects.create_user(username='c', email='Mixed@Example.COM', password='pass12345')
        self.assertEqual(user.email, 'mixed@example.com')

    def test_login_with_username_and_with_email(self):
        CustomUser.objects.create_user(username='bob', email='bob@x.com', password='pass12345')
        for identifier in ('bob', 'BOB@x.com'):
            self.client.logout()
            response = self.client.post(
                reverse('accounts:login'), {'username': identifier, 'password': 'pass12345'}
            )
            self.assertRedirects(response, reverse(FLIGHT_LIST), fetch_redirect_response=False)

    def test_login_wrong_password_shows_generic_error(self):
        CustomUser.objects.create_user(username='bob', password='pass12345')
        response = self.client.post(reverse('accounts:login'), {'username': 'bob', 'password': 'bad'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_respects_safe_next(self):
        CustomUser.objects.create_user(username='bob', password='pass12345')
        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'bob', 'password': 'pass12345', 'next': reverse('accounts:profile')},
        )
        self.assertRedirects(response, reverse('accounts:profile'), fetch_redirect_response=False)

    def test_login_ignores_external_next(self):
        CustomUser.objects.create_user(username='bob', password='pass12345')
        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'bob', 'password': 'pass12345', 'next': 'https://evil.example.com/'},
        )
        self.assertRedirects(response, reverse(FLIGHT_LIST), fetch_redirect_response=False)

    def test_authenticated_user_is_redirected_away_from_login_and_register(self):
        user = CustomUser.objects.create_user(username='bob', password='pass12345')
        self.client.force_login(user)
        for name in ('accounts:login', 'accounts:register'):
            response = self.client.get(reverse(name))
            self.assertRedirects(response, reverse(FLIGHT_LIST), fetch_redirect_response=False)

    def test_logout_requires_post(self):
        user = CustomUser.objects.create_user(username='bob', password='pass12345')
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('accounts:logout')).status_code, 405)
        self.client.post(reverse('accounts:logout'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_profile_requires_login(self):
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 302)


@override_settings(CACHES=TEST_CACHES)
class EmailVerificationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(username='u', email='u@x.com', password='pass12345')

    def test_valid_token_verifies_email_once(self):
        token = EmailVerificationToken.objects.create(user=self.user)
        url = reverse('accounts:verify_email', kwargs={'token': token.token})
        self.client.get(url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)

        self.user.email_verified = False
        self.user.save()
        self.client.get(url)  # استفاده‌ی دوباره
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_unknown_and_expired_tokens_are_rejected(self):
        self.client.get(reverse('accounts:verify_email', kwargs={'token': 'nope'}))
        token = EmailVerificationToken.objects.create(user=self.user)
        EmailVerificationToken.objects.filter(pk=token.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        self.client.get(reverse('accounts:verify_email', kwargs={'token': token.token}))
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_resend_invalidates_previous_token(self):
        self.client.force_login(self.user)
        old = EmailVerificationToken.objects.create(user=self.user)
        self.client.post(reverse('accounts:resend_verification_email'))
        old.refresh_from_db()
        self.assertIsNotNone(old.used_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_changing_email_invalidates_old_token_and_resets_flag(self):
        self.user.email_verified = True
        self.user.save()
        old = EmailVerificationToken.objects.create(user=self.user)
        self.client.force_login(self.user)
        self.client.post(reverse('accounts:profile_edit'), {
            'first_name': '', 'last_name': '', 'email': 'other@x.com', 'phone_number': '',
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'other@x.com')
        self.assertFalse(self.user.email_verified)
        # لینک قدیمی دیگر کار نمی‌کند
        self.client.get(reverse('accounts:verify_email', kwargs={'token': old.token}))
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    def test_editing_email_case_only_does_not_reset_verification(self):
        self.user.email_verified = True
        self.user.save()
        self.client.force_login(self.user)
        self.client.post(reverse('accounts:profile_edit'), {
            'first_name': 'A', 'last_name': '', 'email': 'U@X.com', 'phone_number': '',
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)


@override_settings(CACHES=TEST_CACHES, DEBUG=False)
class PhoneVerificationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            username='u', password='pass12345', phone_number='09123456789'
        )
        self.client.force_login(self.user)
        self.url = reverse('accounts:verify_phone')

    def _send(self):
        self.client.post(self.url, {'send_code': '1'})
        return PhoneVerificationCode.objects.filter(user=self.user, used_at__isnull=True).get()

    def test_correct_code_verifies_phone(self):
        code = self._send().code
        response = self.client.post(self.url, {'code': code})
        self.assertRedirects(response, reverse('accounts:profile'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.phone_verified)

    def test_new_code_invalidates_previous_one(self):
        first = self._send()
        second = self._send()
        first.refresh_from_db()
        self.assertIsNotNone(first.used_at)
        self.assertIsNone(second.used_at)

    def test_expired_code_is_rejected(self):
        code = self._send()
        PhoneVerificationCode.objects.filter(pk=code.pk).update(
            created_at=timezone.now() - timedelta(minutes=11)
        )
        self.client.post(self.url, {'code': code.code})
        self.user.refresh_from_db()
        self.assertFalse(self.user.phone_verified)

    def test_lockout_after_max_wrong_attempts_even_for_correct_code(self):
        code = self._send().code
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(RequestPhoneVerificationView.MAX_ATTEMPTS):
            self.client.post(self.url, {'code': wrong})
        self.client.post(self.url, {'code': code})  # درست، ولی قفل است
        self.user.refresh_from_db()
        self.assertFalse(self.user.phone_verified)

    def test_changing_phone_invalidates_pending_code_and_resets_flag(self):
        code = self._send()
        self.user.phone_verified = True
        self.user.save()
        self.client.post(reverse('accounts:profile_edit'), {
            'first_name': '', 'last_name': '', 'email': '', 'phone_number': '09111111111',
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.phone_verified)
        code.refresh_from_db()
        self.assertIsNotNone(code.used_at)


class DepositViewTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(username='u', password='pass12345')
        self.client.force_login(self.user)

    def test_deposit_updates_balance_and_ledger(self):
        self.client.post(reverse('accounts:deposit'), {'amount': '50000'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, Decimal('50000.00'))
        entry = WalletTransaction.objects.get(user=self.user)
        self.assertEqual(entry.kind, WalletTransaction.KindChoices.CHARGE)

    def test_deposit_below_minimum_is_rejected(self):
        self.client.post(reverse('accounts:deposit'), {'amount': '500'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, Decimal('0.00'))