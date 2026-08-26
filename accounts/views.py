import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.mail import send_mail
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import DetailView, FormView, View

from .forms import DepositForm, LoginForm, RegistrationForm
from .models import CustomUser, EmailVerificationToken

logger = logging.getLogger('accounts')
security_logger = logging.getLogger('accounts.security')


import traceback

def send_verification_email(request, user):
    """ایجاد توکن و ارسال ایمیل فعال‌سازی به کاربر"""
    if not user.email:
        print(f"[EMAIL DEBUG] کاربر {user.username} ایمیل ندارد!")
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
    
    sender = settings.EMAIL_HOST_USER
    print(f"[EMAIL DEBUG] در حال ارسال ایمیل از {sender} به {user.email}...")

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=sender,
            recipient_list=[user.email],
            fail_silently=False,
        )
        print(f"[EMAIL DEBUG] ایمیل با موفقیت ارسال شد.")
    except Exception as e:
        print("=" * 40)
        print("[EMAIL ERROR TRACEBACK]:")
        traceback.print_exc()
        print("=" * 40)
        logger.error(f"خطا در ارسال ایمیل تایید به {user.email}: {e}")
        
        
class RegisterView(FormView):
    """New user registration, generation and sending of an email verification token, and automatic login."""
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
    """ Token verification and user email confirmation activation """
    def get(self, request, token, *args, **kwargs):
        try:
            token_obj = EmailVerificationToken.objects.select_related('user').get(token=token)
        except EmailVerificationToken.DoesNotExist:
            messages.error(request, "لینک تایید نامعتبر است.")
            return redirect('flights:flight_list')

        if not token_obj.is_valid():
            messages.error(request, "لینک تایید منقضی شده یا قبلاً استفاده شده است.")
            return redirect('flights:flight_list')

        user = token_obj.user
        user.email_verified = True
        user.save(update_fields=['email_verified'])

        token_obj.is_used = True
        token_obj.save(update_fields=['is_used'])

        logger.info(f"ایمیل کاربر {user.username} با موفقیت تایید شد.")
        messages.success(request, "ایمیل شما با موفقیت تایید شد.")
        return redirect('accounts:profile')


class LoginView(FormView):
    """User login with username and password."""
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
    """ The user profile includes the wallet balance and account information. """
    model = CustomUser
    template_name = 'accounts/profile.html'
    context_object_name = 'profile_user'

    def get_object(self, queryset=None):
        return self.request.user


class DepositView(LoginRequiredMixin, FormView):
    """ Topping up the simulated wallet. """
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