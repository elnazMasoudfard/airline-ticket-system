from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import redirect


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    فقط به کاربرانی که is_staff=True دارند اجازه‌ی دسترسی می‌دهد.
    این جدا از پنل ادمین جنگو است؛ داشبورد اختصاصی خودمان را محدود می‌کند.
    """

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_staff

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.error(self.request, "شما دسترسی لازم برای این بخش را ندارید.")
            return redirect('flights:flight_list')
        return super().handle_no_permission()