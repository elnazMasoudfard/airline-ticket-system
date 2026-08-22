from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import ListView, View

from accounts.models import CustomUser
from flights.models import Flight
from flights.services import generate_seats_for_flight
from tickets.models import Reservation

from .forms import FlightForm, SeatClassFormSet
from .mixins import StaffRequiredMixin


class DashboardHomeView(StaffRequiredMixin, View):
    """صفحه‌ی اصلی داشبورد با یک نمای کلی از وضعیت سیستم."""

    def get(self, request, *args, **kwargs):
        context = {
            'flight_count': Flight.objects.count(),
            'upcoming_flight_count': Flight.objects.filter(
                status=Flight.StatusChoices.SCHEDULED,
                departure_datetime__gt=timezone.now(),
            ).count(),
            'active_reservation_count': Reservation.objects.filter(
                status=Reservation.StatusChoices.RESERVED
            ).count(),
            'user_count': CustomUser.objects.count(),
        }
        return render(request, 'dashboard/home.html', context)


class FlightManageListView(StaffRequiredMixin, ListView):
    """لیست همه‌ی پروازها برای مدیریت (نه فقط پروازهای آینده)."""
    model = Flight
    template_name = 'dashboard/flight_manage_list.html'
    context_object_name = 'flights'
    paginate_by = 15

    def get_queryset(self):
        return (
            Flight.objects
            .select_related('route__origin', 'route__destination', 'airline')
            .order_by('-departure_datetime')
        )


class FlightCreateView(StaffRequiredMixin, View):
    """ایجاد پرواز جدید همراه با کلاس‌های صندلی آن در یک فرم."""
    template_name = 'dashboard/flight_form.html'

    def get(self, request, *args, **kwargs):
        form = FlightForm()
        formset = SeatClassFormSet()
        return render(request, self.template_name, {'form': form, 'formset': formset, 'is_edit': False})

    def post(self, request, *args, **kwargs):
        form = FlightForm(request.POST)
        formset = SeatClassFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                flight = form.save()
                formset.instance = flight
                formset.save()
            messages.success(request, f"پرواز {flight.flight_number} با موفقیت ایجاد شد.")
            return redirect('dashboard:flight_manage_list')

        return render(request, self.template_name, {'form': form, 'formset': formset, 'is_edit': False})


class FlightEditView(StaffRequiredMixin, View):
    """ویرایش یک پرواز موجود و کلاس‌های صندلی آن."""
    template_name = 'dashboard/flight_form.html'

    def get_flight(self):
        return get_object_or_404(Flight, pk=self.kwargs['pk'])

    def get(self, request, *args, **kwargs):
        flight = self.get_flight()
        form = FlightForm(instance=flight)
        formset = SeatClassFormSet(instance=flight)
        return render(request, self.template_name, {
            'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
        })

    def post(self, request, *args, **kwargs):
        flight = self.get_flight()
        form = FlightForm(request.POST, instance=flight)
        formset = SeatClassFormSet(request.POST, instance=flight)

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                form.save()
                formset.save()
            messages.success(request, "پرواز با موفقیت به‌روزرسانی شد.")
            return redirect('dashboard:flight_manage_list')

        return render(request, self.template_name, {
            'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
        })


class GenerateSeatsView(StaffRequiredMixin, View):
    """
    ساخت خودکار صندلی‌ها برای همه‌ی کلاس‌های صندلیِ یک پرواز، مستقیم از داشبورد
    (بدون نیاز به رفتن به پنل ادمین جنگو).
    """

    def post(self, request, pk, *args, **kwargs):
        flight = get_object_or_404(Flight, pk=pk)
        created, skipped = generate_seats_for_flight(flight)

        if created:
            messages.success(request, f"{created} صندلی برای پرواز {flight.flight_number} ساخته شد.")
        if skipped:
            messages.warning(
                request,
                "این کلاس‌های صندلی از قبل صندلی داشتند و رد شدند: " + "، ".join(skipped)
            )
        if not created and not skipped:
            messages.info(request, "هنوز کلاس صندلی‌ای برای این پرواز ثبت نشده است.")

        return redirect('dashboard:flight_edit', pk=flight.pk)