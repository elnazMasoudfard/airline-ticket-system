import logging
import traceback

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, FormView, View

from .forms import DepositForm, LoginForm, PhoneVerificationForm, RegistrationForm
from .models import CustomUser, EmailVerificationToken, PhoneVerificationCode

logger = logging.getLogger('accounts')
security_logger = logging.getLogger('accounts.security')


def send_verification_email(request, user):
    """ایجاد توکن و ارسال ایمیل فعال‌سازی به کاربر."""
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
    ایجاد کد تایید ۶ رقمی برای شماره موبایل کاربر.
    چون درگاه پیامک واقعی متصل نیست، ارسال آن شبیه‌سازی‌شده و فقط در
    کنسول/لاگ سرور چاپ می‌شود (دقیقاً مثل رفتار EMAIL_BACKEND=console).
    """
    # کدهای قبلی و استفاده‌نشده را باطل می‌کنیم تا فقط جدیدترین کد معتبر باشد
    PhoneVerificationCode.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())

    verification = PhoneVerificationCode.objects.create(user=user)
    print(f"[SMS DEBUG] کد تایید برای {user.phone_number}: {verification.code}")
    logger.info(f"کد تایید پیامکی (شبیه‌سازی‌شده) ساخته شد: user={user.username}, phone={user.phone_number}")


class RegisterView(FormView):
    """ثبت‌نام کاربر جدید، ارسال ایمیل تایید، و ورود خودکار."""
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
    """اعتبارسنجی توکن و فعال‌سازی تایید ایمیل کاربر."""

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
    """ارسال دوباره‌ی لینک تایید ایمیل (مثلاً وقتی کاربر لینک اول را گم کرده یا منقضی شده)."""

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
    صفحه‌ی تایید شماره موبایل: یک دکمه برای ارسال کد (شبیه‌سازی‌شده)
    و یک فرم برای وارد کردن کد دریافتی.
    """
    template_name = 'accounts/verify_phone.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {'form': PhoneVerificationForm(), 'code_sent': False})

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
                security_logger.warning(f"کد تایید پیامکی اشتباه: user={request.user.username}")
                messages.error(request, "کد وارد‌شده اشتباه است.")
            elif verification.is_expired:
                messages.error(request, "کد منقضی شده است. دوباره درخواست بدهید.")
            else:
                verification.used_at = timezone.now()
                verification.save(update_fields=['used_at'])
                request.user.phone_verified = True
                request.user.save(update_fields=['phone_verified'])
                logger.info(f"شماره موبایل تایید شد: user={request.user.username}")
                messages.success(request, "شماره موبایل شما با موفقیت تایید شد.")
                return redirect('accounts:profile')

        return render(request, self.template_name, {'form': form, 'code_sent': True})


class LoginView(FormView):
    """ورود کاربر با نام کاربری و رمز عبور."""
    template_name = 'accounts/login.html'
    form_class = LoginForm
    success_url = reverse_lazy('flights:flight_list')

    def form_valid(self, form):
        username = form.cleaned_data['username']
        user = authenticate(
            self.request,
            username=username,
            password=form.cleaned_data['password'],
        )
        if user is None:
            security_logger.warning(f"تلاش ناموفق برای ورود: username={username}")
            form.add_error(None, "نام کاربری یا رمز عبور اشتباه است.")
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
    """پروفایل کاربر شامل موجودی کیف پول و اطلاعات حساب."""
    model = CustomUser
    template_name = 'accounts/profile.html'
    context_object_name = 'profile_user'

    def get_object(self, queryset=None):
        return self.request.user


class DepositView(LoginRequiredMixin, FormView):
    """شارژ کیف پول (شبیه‌سازی‌شده، بدون اتصال به درگاه پرداخت واقعی)."""
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