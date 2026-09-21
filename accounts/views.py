import logging
import traceback

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.mail import message as django_mail_message
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, FormView, UpdateView, View

from .forms import DepositForm, LoginForm, PhoneVerificationForm, ProfileEditForm, RegistrationForm
from .models import CustomUser, EmailVerificationToken, PhoneVerificationCode

# For UTF-8 emails with short lines, Django uses its own internal Charset object (utf8_charset)
# with body_encoding=None—effectively BASE64—regardless of the global email.charset setting.
# By directly changing this value to QP (quoted-printable),
# ASCII text (such as a confirmation link) remains readable in the console or logs.
django_mail_message.utf8_charset.body_encoding = django_mail_message.Charset.QP

logger = logging.getLogger('accounts')
security_logger = logging.getLogger('accounts.security')


def send_verification_email(request, user):
    """Create a verification token and send a verification email to the user."""
    if not user.email:
        logger.warning(f"تلاش برای ارسال ایمیل تایید بدون ایمیل ثبت‌شده: user={user.username}")
        return

    token_obj = EmailVerificationToken.objects.create(user=user)
    verify_url = request.build_absolute_uri(
        reverse('accounts:verify_email', kwargs={'token': token_obj.token})
    )
    subject = "تایید آدرس ایمیل - سامانه رزرواسیون پرواز"
    message = (
        f"سلام {user.get_full_name() or user.username} عزیز،\n\n"
        f"برای تایید آدرس ایمیل حساب کاربری خود، روی لینک زیر کلیک کنید (اعتبار: ۲۴ ساعت):\n"
        f"{verify_url}\n\n"
        f"اگر این درخواست از سمت شما نبوده، این ایمیل را نادیده بگیرید."
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info(f"ایمیل تایید ارسال شد: user={user.username}, email={user.email}")
    except Exception as e:
        logger.error(f"خطا در ارسال ایمیل تایید به {user.email}: {e}\n{traceback.format_exc()}")


def send_verification_sms(user):
    """
    Generates a 6-digit verification code for the user's mobile number.
    Since no actual SMS gateway is connected, the sending process is simulated.
    The code value is printed or logged only in DEBUG mode (for local testing);
    in a production environment (DEBUG=False), the code is not exposed to any
    console or file, as it constitutes sensitive security information.
    """
    # invalidate previous, unused codes so that only the latest code remains valid.
    PhoneVerificationCode.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())

    verification = PhoneVerificationCode.objects.create(user=user)

    if settings.DEBUG:
        print(f"[SMS DEBUG] کد تایید برای {user.phone_number}: {verification.code}")
        logger.info(f"کد تایید پیامکی (شبیه‌سازی‌شده) ساخته شد: user={user.username}, code={verification.code}")
    else:
        logger.warning(f"درگاه پیامک واقعی متصل نیست؛ کد تایید ساخته شد ولی ارسال نشد: user={user.username}")


class RegisterView(FormView):
    """Register a new user, send a verification email, and log them in automatically."""
    template_name = 'accounts/register.html'
    form_class = RegistrationForm
    success_url = reverse_lazy('flights:flight_list')

    def form_valid(self, form):
        user = form.save()
        send_verification_email(self.request, user)
        login(self.request, user)
        logger.info(f"کاربر جدید ثبت‌نام کرد: username={user.username}, email={user.email}")
        security_logger.info(f"ورود پس از ثبت‌نام: username={user.username}")
        messages.success(
            self.request,
            "ثبت‌نام با موفقیت انجام شد. لینک فعال‌سازی به ایمیل شما ارسال گردید."
        )
        return super().form_valid(form)


class VerifyEmailView(View):
    """Verify the email verification token and activate the user's email."""

    def get(self, request, token, *args, **kwargs):
        try:
            token_obj = EmailVerificationToken.objects.select_related('user').get(token=token)
        except EmailVerificationToken.DoesNotExist:
            messages.error(request, "لینک تایید نامعتبر است.")
            return redirect('flights:flight_list')

        if token_obj.is_used or token_obj.is_expired:
            messages.error(request, "لینک تایید منقضی شده یا قبلاً استفاده شده است.")
            return redirect('flights:flight_list')

        user = token_obj.user
        user.email_verified = True
        user.save(update_fields=['email_verified'])

        token_obj.used_at = timezone.now()
        token_obj.save(update_fields=['used_at'])

        logger.info(f"ایمیل کاربر {user.username} با موفقیت تایید شد.")
        messages.success(request, "ایمیل شما با موفقیت تایید شد.")
        return redirect('accounts:profile')


class ResendVerificationEmailView(LoginRequiredMixin, View):
    """Resend the email verification link (e.g., when the user loses the first link or it expires)."""

    def post(self, request, *args, **kwargs):
        if request.user.email_verified:
            messages.info(request, "ایمیل شما قبلاً تایید شده است.")
        elif not request.user.email:
            messages.error(request, "برای دریافت لینک تایید، ابتدا باید ایمیل خود را ثبت کنید.")
        else:
            send_verification_email(request, request.user)
            messages.success(request, "لینک تایید جدید به ایمیل شما ارسال شد.")
        return redirect('accounts:profile')


class RequestPhoneVerificationView(LoginRequiredMixin, View):
    """
    Phone verification page: A button to send the verification code (simulated)
    and a form to enter the received code.
    """
    template_name = 'accounts/verify_phone.html'
    MAX_ATTEMPTS = 5
    LOCKOUT_SECONDS = 15 * 60

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {'form': PhoneVerificationForm(), 'code_sent': False})

    def _attempts_cache_key(self, user):
        return f'phone_verify_attempts_{user.pk}'

    def post(self, request, *args, **kwargs):
        if request.user.phone_verified:
            messages.info(request, "شماره موبایل شما قبلاً تایید شده است.")
            return redirect('accounts:profile')

        if 'send_code' in request.POST:
            if not request.user.phone_number:
                messages.error(request, "ابتدا باید شماره موبایل خود را ثبت کنید.")
                return redirect('accounts:profile')

            send_verification_sms(request.user)
            messages.info(
                request,
                "کد تایید ارسال شد. (چون به درگاه پیامک واقعی متصل نیستیم، کد در کنسول سرور چاپ می‌شود.)"
            )
            return render(request, self.template_name, {'form': PhoneVerificationForm(), 'code_sent': True})

        cache_key = self._attempts_cache_key(request.user)
        attempts = cache.get(cache_key, 0)
        if attempts >= self.MAX_ATTEMPTS:
            security_logger.warning(f"قفل موقت تایید پیامکی به‌خاطر تلاش زیاد: user={request.user.username}")
            messages.error(
                request,
                "به‌خاطر تلاش‌های ناموفق زیاد، برای چند دقیقه امکان تایید کد وجود ندارد."
            )
            return render(request, self.template_name, {'form': PhoneVerificationForm(), 'code_sent': True})

        form = PhoneVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['code']
            verification = (
                PhoneVerificationCode.objects
                .filter(user=request.user, code=code, used_at__isnull=True)
                .order_by('-created_at')
                .first()
            )

            if verification is None:
                cache.set(cache_key, attempts + 1, timeout=self.LOCKOUT_SECONDS)
                security_logger.warning(f"کد تایید پیامکی اشتباه: user={request.user.username}")
                messages.error(request, "کد وارد‌شده اشتباه است.")
            elif verification.is_expired:
                messages.error(request, "کد منقضی شده است. دوباره درخواست بدهید.")
            else:
                cache.delete(cache_key)
                verification.used_at = timezone.now()
                verification.save(update_fields=['used_at'])
                request.user.phone_verified = True
                request.user.save(update_fields=['phone_verified'])
                logger.info(f"شماره موبایل تایید شد: user={request.user.username}")
                messages.success(request, "شماره موبایل شما با موفقیت تایید شد.")
                return redirect('accounts:profile')

        return render(request, self.template_name, {'form': form, 'code_sent': True})


class LoginView(FormView):
    """User login using username or email, along with the password."""
    template_name = 'accounts/login.html'
    form_class = LoginForm
    success_url = reverse_lazy('flights:flight_list')

    def form_valid(self, form):
        identifier = form.cleaned_data['username']
        password = form.cleaned_data['password']

        user = authenticate(self.request, username=identifier, password=password)

        # If the value is not found as a username but resembles an email address,
        # locate the user via their email and authenticate them using their actual username.
        if user is None and '@' in identifier:
            matched_user = CustomUser.objects.filter(email__iexact=identifier).first()
            if matched_user:
                user = authenticate(self.request, username=matched_user.username, password=password)

        if user is None:
            security_logger.warning(f"تلاش ناموفق برای ورود: identifier={identifier}")
            form.add_error(None, "نام کاربری/ایمیل یا رمز عبور اشتباه است.")
            return self.form_invalid(form)

        security_logger.info(f"ورود موفق کاربر: username={user.username}")
        login(self.request, user)
        return super().form_valid(form)


class LogoutView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        security_logger.info(f"خروج کاربر: username={request.user.username}")
        logout(request)
        return redirect('accounts:login')


class ProfileView(LoginRequiredMixin, DetailView):
    """User profile including wallet balance and account information."""
    model = CustomUser
    template_name = 'accounts/profile.html'
    context_object_name = 'profile_user'

    def get_object(self, queryset=None):
        return self.request.user


class ProfileEditView(LoginRequiredMixin, UpdateView):
    """
    Edit user profile information (first name, last name, email, phone number).
    If the email or phone number is changed, the verification status will be automatically reset to
    "not verified" since the previous verification was based on the old values.
    """
    model = CustomUser
    form_class = ProfileEditForm
    template_name = 'accounts/profile_edit.html'
    success_url = reverse_lazy('accounts:profile')

    def get_object(self, queryset=None):
        return self.request.user

    def form_valid(self, form):
        email_changed = 'email' in form.changed_data
        phone_changed = 'phone_number' in form.changed_data

        response = super().form_valid(form)
        user = self.object

        if email_changed:
            user.email_verified = False
            user.save(update_fields=['email_verified'])
            logger.info(f"ایمیل کاربر تغییر کرد و نیاز به تایید مجدد دارد: user={user.username}")
            if user.email:
                send_verification_email(self.request, user)
                messages.info(self.request, "چون ایمیل خود را تغییر دادید، لینک تایید جدید برایتان ارسال شد.")

        if phone_changed:
            user.phone_verified = False
            user.save(update_fields=['phone_verified'])
            messages.info(self.request, "چون شماره موبایل خود را تغییر دادید، باید دوباره تاییدش کنید.")

        logger.info(f"پروفایل کاربر ویرایش شد: user={user.username}")
        messages.success(self.request, "اطلاعات پروفایل با موفقیت به‌روزرسانی شد.")
        return response


class DepositView(LoginRequiredMixin, FormView):
    """Deposit funds into the user's wallet (simulated, without connecting to a real payment gateway)."""
    template_name = 'accounts/deposit.html'
    form_class = DepositForm
    success_url = reverse_lazy('accounts:profile')

    def form_valid(self, form):
        amount = form.cleaned_data['amount']
        self.request.user.deposit(amount)
        logger.info(
            f"شارژ کیف پول: user={self.request.user.username}, amount={amount}, "
            f"new_balance={self.request.user.wallet_balance}"
        )
        messages.success(
            self.request,
            f"مبلغ {amount:.0f} تومان به کیف پول شما اضافه شد. "
            f"(این یک شارژ شبیه‌سازی‌شده است و به درگاه پرداخت واقعی متصل نیست.)"
        )
        return super().form_valid(form)