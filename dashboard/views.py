import logging
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView, View

from accounts.models import CustomUser
from flights.models import Flight
from flights.services import generate_seats_for_flight
from tickets.models import Reservation

from .forms import FlightForm, SeatClassFormSet
from .mixins import StaffRequiredMixin

logger = logging.getLogger('dashboard')


class DashboardHomeView(StaffRequiredMixin, View):
    """صفحه‌ی اصلی داشبورد با نمای کلی وضعیت سیستم و خلاصه‌ی مالی."""

    def get(self, request, *args, **kwargs):
        money_field = DecimalField(max_digits=14, decimal_places=2)

        flights_financials = (
            Flight.objects
            .select_related('route__origin', 'route__destination')
            .annotate(
                gross_paid=Coalesce(
                    Sum('seat_classes__reservations__total_paid_price'), Decimal('0.00'),
                    output_field=money_field,
                ),
                total_refunded=Coalesce(
                    Sum('seat_classes__reservations__refund_amount'), Decimal('0.00'),
                    output_field=money_field,
                ),
            )
            .annotate(
                net_revenue=ExpressionWrapper(
                    F('gross_paid') - F('total_refunded'), output_field=money_field
                )
            )
            .order_by('-departure_datetime')
        )

        reservation_totals = Reservation.objects.aggregate(
            total_gross=Coalesce(Sum('total_paid_price'), Decimal('0.00'), output_field=money_field),
            total_refunded=Coalesce(Sum('refund_amount'), Decimal('0.00'), output_field=money_field),
        )
        total_gross = reservation_totals['total_gross']
        total_refunded = reservation_totals['total_refunded']
        total_net = total_gross - total_refunded

        context = {
            'flight_count': Flight.objects.count(),
            'upcoming_flight_count': Flight.objects.upcoming().count(),
            'active_reservation_count': Reservation.objects.active().count(),
            'user_count': CustomUser.objects.count(),
            'flights_financials': flights_financials,
            'total_gross': total_gross,
            'total_refunded': total_refunded,
            'total_net': total_net,
        }
        return render(request, 'dashboard/home.html', context)


class FlightManageListView(StaffRequiredMixin, ListView):
    """
    لیست همه‌ی پروازها برای مدیریت — شامل پروازهای گذشته و آینده.
    با ?filter=upcoming فقط پروازهای آینده و برنامه‌ریزی‌شده نمایش داده می‌شود.
    """
    model = Flight
    template_name = 'dashboard/flight_manage_list.html'
    context_object_name = 'flights'
    paginate_by = 15

    def get_queryset(self):
        queryset = (
            Flight.objects
            .with_route_info()
            .annotate(
                active_reservation_count=Count(
                    'seat_classes__reservations',
                    filter=Q(seat_classes__reservations__status='reserved'),
                    distinct=True,
                )
            )
            .order_by('-departure_datetime')
        )
        if self.request.GET.get('filter') == 'upcoming':
            queryset = queryset.upcoming()
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['now'] = timezone.now()
        context['is_upcoming_filter'] = self.request.GET.get('filter') == 'upcoming'
        return context


class FlightManageDetailView(StaffRequiredMixin, DetailView):
    """جزئیات یک پرواز برای مدیر، شامل لیست کامل رزروهای آن (فعال و کنسل‌شده)."""
    model = Flight
    template_name = 'dashboard/flight_manage_detail.html'
    context_object_name = 'flight'

    def get_queryset(self):
        return (
            Flight.objects
            .select_related('route__origin', 'route__destination', 'airline')
            .prefetch_related('seat_classes')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['reservations'] = (
            Reservation.objects
            .filter(seat_class__flight=self.object)
            .select_related('user', 'seat_class')
            .order_by('-created_at')
        )
        return context


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
            logger.info(f"پرواز جدید ایجاد شد توسط مدیر={request.user.username}: {flight.flight_number}")
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
            logger.info(f"پرواز ویرایش شد توسط مدیر={request.user.username}: {flight.flight_number}")
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
        logger.info(
            f"ساخت خودکار صندلی توسط مدیر={request.user.username} "
            f"برای پرواز={flight.flight_number}: ساخته‌شده={created} رد‌شده={len(skipped)}"
        )

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


class ReservationManageListView(StaffRequiredMixin, ListView):
    """
    لیست همه‌ی رزروهای سیستم (نه فقط رزروهای یک کاربر خاص).
    با ?filter=active فقط رزروهای فعال (کنسل‌نشده) نمایش داده می‌شود.
    """
    model = Reservation
    template_name = 'dashboard/reservation_manage_list.html'
    context_object_name = 'reservations'
    paginate_by = 20

    def get_queryset(self):
        queryset = (
            Reservation.objects
            .select_related('user')
            .with_flight_info()
            .order_by('-created_at')
        )
        if self.request.GET.get('filter') == 'active':
            queryset = queryset.active()
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_active_filter'] = self.request.GET.get('filter') == 'active'
        return context


class UserManageListView(StaffRequiredMixin, ListView):
    """لیست همه‌ی کاربران ثبت‌نام‌شده برای مدیر."""
    model = CustomUser
    template_name = 'dashboard/user_manage_list.html'
    context_object_name = 'users'
    paginate_by = 20

    def get_queryset(self):
        return CustomUser.objects.order_by('-date_joined')