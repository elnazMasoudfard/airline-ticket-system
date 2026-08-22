from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import DetailView, FormView, View

from .forms import DepositForm, LoginForm, RegistrationForm
from .models import CustomUser


class RegisterView(FormView):
    """ثبت‌نام کاربر جدید و ورود خودکار پس از ثبت‌نام موفق."""
    template_name = 'accounts/register.html'
    form_class = RegistrationForm
    success_url = reverse_lazy('flights:flight_list')

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)
        messages.success(self.request, "ثبت‌نام با موفقیت انجام شد.")
        return super().form_valid(form)


class LoginView(FormView):
    """ورود کاربر با نام کاربری و رمز عبور."""
    template_name = 'accounts/login.html'
    form_class = LoginForm
    success_url = reverse_lazy('flights:flight_list')

    def form_valid(self, form):
        user = authenticate(
            self.request,
            username=form.cleaned_data['username'],
            password=form.cleaned_data['password'],
        )
        if user is None:
            form.add_error(None, "نام کاربری یا رمز عبور اشتباه است.")
            return self.form_invalid(form)

        login(self.request, user)
        return super().form_valid(form)


class LogoutView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
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
    """
    شارژ کیف پول.
    توجه: این یک شارژ شبیه‌سازی‌شده است و به هیچ درگاه پرداخت واقعی وصل نیست
    (طبق تعریف پروژه، اتصال به درگاه پرداخت واقعی جزو الزامات نیست).
    """
    template_name = 'accounts/deposit.html'
    form_class = DepositForm
    success_url = reverse_lazy('accounts:profile')

    def form_valid(self, form):
        amount = form.cleaned_data['amount']
        self.request.user.deposit(amount)
        messages.success(
            self.request,
            f"مبلغ {amount:.0f} تومان به کیف پول شما اضافه شد. "
            f"(این یک شارژ شبیه‌سازی‌شده است و به درگاه پرداخت واقعی متصل نیست.)"
        )
        return super().form_valid(form)