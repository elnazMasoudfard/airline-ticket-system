import logging
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView, View

from accounts.models import CustomUser
from flights.models import Flight
from flights.services import generate_seats_for_flight, sync_flight_statuses
from tickets.models import Reservation
from tickets.services import ReservationError, cancel_flight_and_refund

from .forms import FlightForm, SeatClassFormSet
from .mixins import StaffRequiredMixin

logger = logging.getLogger('dashboard')

STATUS = Reservation.StatusChoices
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ZERO = Decimal('0.00')


def reservation_financials(queryset):
    """
    Financial summary of a Reservation queryset.

    IMPORTANT: `Reservation.total_paid_price` is filled when the reservation is
    CREATED (while it is still unpaid), so it must only be summed for
    reservations that were really paid (`paid_at` is set). Unpaid pending and
    unpaid cancelled/expired reservations never count as revenue.
    """
    now = timezone.now()
    paid = Q(paid_at__isnull=False)
    live_pending = Q(status=STATUS.PENDING_PAYMENT, payment_expires_at__gt=now)

    data = queryset.aggregate(
        gross=Coalesce(Sum('total_paid_price', filter=paid), ZERO, output_field=MONEY_FIELD),
        refunded=Coalesce(Sum('refund_amount', filter=paid), ZERO, output_field=MONEY_FIELD),
        pending_amount=Coalesce(
            Sum('total_paid_price', filter=live_pending), ZERO, output_field=MONEY_FIELD
        ),
        pending_count=Count('pk', filter=live_pending),
        paid_count=Count('pk', filter=Q(status=STATUS.RESERVED)),
    )
    data['net'] = data['gross'] - data['refunded']
    return data


def flash_flight_cancel_result(request, flight, result):
    messages.success(
        request,
        f"پرواز {flight.flight_number} لغو شد: {result.refunded_count} رزرو قطعی "
        f"به مبلغ کل {result.refunded_total:,.0f} تومان به کیف پول کاربران بازگردانده شد "
        f"و {result.cancelled_pending_count} رزرو پرداخت‌نشده لغو شد.",
    )
    if result.failed:
        messages.error(
            request,
            "این رزروها به‌صورت خودکار لغو/مسترد نشدند و باید دستی بررسی شوند: "
            + "، ".join(result.failed),
        )


class DashboardHomeView(StaffRequiredMixin, View):
    """Main dashboard page with an overview of system status and a financial summary."""

    def get(self, request, *args, **kwargs):
        sync_flight_statuses()

        paid_only = Q(seat_classes__reservations__paid_at__isnull=False)

        flights_financials = (
            Flight.objects
            .select_related('route__origin', 'route__destination')
            .annotate(
                gross_paid=Coalesce(
                    Sum('seat_classes__reservations__total_paid_price', filter=paid_only),
                    ZERO, output_field=MONEY_FIELD,
                ),
                total_refunded=Coalesce(
                    Sum('seat_classes__reservations__refund_amount', filter=paid_only),
                    ZERO, output_field=MONEY_FIELD,
                ),
            )
            .annotate(
                net_revenue=ExpressionWrapper(
                    F('gross_paid') - F('total_refunded'), output_field=MONEY_FIELD
                )
            )
            .order_by('-departure_datetime')
        )

        totals = reservation_financials(Reservation.objects.all())

        # Financial table pagination – without this,
        # the entire table would render at once as the number of flights increased.
        paginator = Paginator(flights_financials, 10)
        page_obj = paginator.get_page(request.GET.get('page'))

        context = {
            'flight_count': Flight.objects.count(),
            'upcoming_flight_count': Flight.objects.upcoming().count(),
            'paid_reservation_count': totals['paid_count'],
            'pending_reservation_count': totals['pending_count'],
            'pending_amount': totals['pending_amount'],
            'user_count': CustomUser.objects.count(),
            'flights_financials': page_obj,
            'page_obj': page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'total_gross': totals['gross'],
            'total_refunded': totals['refunded'],
            'total_net': totals['net'],
        }
        return render(request, 'dashboard/home.html', context)


class FlightManageListView(StaffRequiredMixin, ListView):
    """
    List of all flights for management — including past and future flights.
    Using ?filter=upcoming displays only future and scheduled flights.
    """
    model = Flight
    template_name = 'dashboard/flight_manage_list.html'
    context_object_name = 'flights'
    paginate_by = 15

    def get_queryset(self):
        sync_flight_statuses()
        now = timezone.now()

        queryset = (
            Flight.objects
            .with_route_info()
            .annotate(
                paid_reservation_count=Count(
                    'seat_classes__reservations',
                    filter=Q(seat_classes__reservations__status=STATUS.RESERVED),
                    distinct=True,
                ),
                pending_reservation_count=Count(
                    'seat_classes__reservations',
                    filter=Q(
                        seat_classes__reservations__status=STATUS.PENDING_PAYMENT,
                        seat_classes__reservations__payment_expires_at__gt=now,
                    ),
                    distinct=True,
                ),
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
    """Flight details for the manager, including a complete list of its bookings (paid, pending and cancelled)."""
    model = Flight
    template_name = 'dashboard/flight_manage_detail.html'
    context_object_name = 'flight'

    def get_queryset(self):
        sync_flight_statuses()
        return (
            Flight.objects
            .select_related('route__origin', 'route__destination', 'airline')
            .prefetch_related('seat_classes')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        flight_reservations = Reservation.objects.filter(seat_class__flight=self.object)

        context['reservations'] = (
            flight_reservations
            .select_related('user', 'seat_class')
            .order_by('-created_at')
        )
        context['financials'] = reservation_financials(flight_reservations)

        # Info for the "cancel flight" box
        context['can_cancel_flight'] = self.object.status not in (
            Flight.StatusChoices.CANCELLED,
            Flight.StatusChoices.COMPLETED,
        )
        context['cancel_info'] = {
            'paid_count': context['financials']['paid_count'],
            'pending_count': flight_reservations.filter(
                status=STATUS.PENDING_PAYMENT
            ).count(),
            'refundable': flight_reservations.filter(status=STATUS.RESERVED).aggregate(
                total=Coalesce(Sum('total_paid_price'), ZERO, output_field=MONEY_FIELD)
            )['total'],
        }
        return context


class FlightCreateView(StaffRequiredMixin, View):
    """Creating a new flight along with its seating classes in a single form."""
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
    """Editing an existing flight and its seat classes."""
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
            try:
                with transaction.atomic():
                    form.save()
                    formset.save()
            except ProtectedError:
                logger.warning(
                    f"تلاش ناموفق برای حذف کلاس صندلی دارای تاریخچه توسط={request.user.username}, "
                    f"flight={flight.flight_number}"
                )
                messages.error(
                    request,
                    "یکی از کلاس‌های صندلی به‌خاطر داشتن تاریخچه‌ی رزرو (حتی کنسل‌شده) قابل حذف نیست. "
                    "برای حذف اجباری همراه با پاک‌شدن تاریخچه، از اکشن مخصوص در پنل ادمین جنگو استفاده کنید."
                )
                return render(request, self.template_name, {
                    'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
                })

            logger.info(f"پرواز ویرایش شد توسط مدیر={request.user.username}: {flight.flight_number}")
            messages.success(request, "پرواز با موفقیت به‌روزرسانی شد.")

            # Setting the status to "cancelled" in the form must also cancel
            # and refund every reservation of this flight.
            if (
                'status' in form.changed_data
                and form.cleaned_data['status'] == Flight.StatusChoices.CANCELLED
            ):
                try:
                    result = cancel_flight_and_refund(flight.pk)
                except ReservationError as exc:
                    messages.error(request, str(exc))
                else:
                    flash_flight_cancel_result(request, flight, result)

            return redirect('dashboard:flight_manage_list')

        return render(request, self.template_name, {
            'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
        })


class FlightCancelView(StaffRequiredMixin, View):
    """Cancel a whole flight: refund every paid reservation and release the unpaid ones."""

    def post(self, request, pk, *args, **kwargs):
        flight = get_object_or_404(Flight, pk=pk)

        try:
            result = cancel_flight_and_refund(flight.pk)
        except ReservationError as exc:
            messages.error(request, str(exc))
        else:
            logger.info(
                f"لغو پرواز توسط مدیر={request.user.username}: {flight.flight_number} "
                f"استرداد={result.refunded_count} پرداخت‌نشده={result.cancelled_pending_count} "
                f"ناموفق={result.failed}"
            )
            flash_flight_cancel_result(request, flight, result)

        return redirect('dashboard:flight_manage_detail', pk=flight.pk)


class GenerateSeatsView(StaffRequiredMixin, View):
    """
    Automated seat creation for all flight seat classes, directly from the dashboard
    (without needing to access the Django admin panel).
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
    A list of all system reservations (not just those of a specific user).

    ?filter= values:
      paid              - confirmed and paid
      pending           - waiting for payment
      unpaid_cancelled  - cancelled WITHOUT ever being paid (timeout / user)
      refunded          - paid and cancelled later (refund issued)
      active            - pending + paid (kept for old links)
    """
    model = Reservation
    template_name = 'dashboard/reservation_manage_list.html'
    context_object_name = 'reservations'
    paginate_by = 20

    FILTER_TABS = [
        ('', 'همه'),
        ('paid', 'قطعی (پرداخت‌شده)'),
        ('pending', 'در انتظار پرداخت'),
        ('unpaid_cancelled', 'لغو‌شده بدون پرداخت'),
        ('refunded', 'لغو‌شده با استرداد'),
    ]

    def get_queryset(self):
        queryset = (
            Reservation.objects
            .select_related('user')
            .with_flight_info()
            .order_by('-created_at')
        )

        current = self.request.GET.get('filter', '')
        if current == 'active':
            queryset = queryset.active()
        elif current == 'paid':
            queryset = queryset.filter(status=STATUS.RESERVED)
        elif current == 'pending':
            queryset = queryset.filter(status=STATUS.PENDING_PAYMENT)
        elif current == 'unpaid_cancelled':
            queryset = queryset.filter(status=STATUS.CANCELLED, paid_at__isnull=True)
        elif current == 'refunded':
            queryset = queryset.filter(status=STATUS.CANCELLED, paid_at__isnull=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['filter_tabs'] = self.FILTER_TABS
        context['current_filter'] = self.request.GET.get('filter', '')
        return context


class UserManageListView(StaffRequiredMixin, ListView):
    """List of all registered users for the administrator."""
    model = CustomUser
    template_name = 'dashboard/user_manage_list.html'
    context_object_name = 'users'
    paginate_by = 20

    def get_queryset(self):
        return CustomUser.objects.order_by('-date_joined')