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
from flights.models import Flight, SeatClass
from flights.services import (
    generate_seats_for_flight,
    resync_flight_seats,
    sync_flight_statuses,
)
from tickets.models import Reservation
from tickets.services import (
    ReservationError,
    cancel_flight_and_refund,
    expire_pending_reservations,
)

from .forms import FlightForm, SeatClassFormSet
from .mixins import StaffRequiredMixin

logger = logging.getLogger('dashboard')

STATUS = Reservation.StatusChoices
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ZERO = Decimal('0.00')


def live_pending_q(prefix='', now=None):
    """
    The ONE definition of a "pending reservation" used by every dashboard page:
    waiting for payment AND still inside its payment window. `prefix` lets the
    same condition be used across a relation (e.g. 'seat_classes__reservations__').
    """
    now = now or timezone.now()
    return Q(**{
        f'{prefix}status': STATUS.PENDING_PAYMENT,
        f'{prefix}payment_expires_at__gt': now,
    })


def expire_stale_reservations():
    """
    Cancel pending reservations whose payment window is over (and free their
    seats) before a page is built, so the manager never sees stale "pending"
    rows and every page agrees on the numbers. A failure must never break the page.
    """
    try:
        return expire_pending_reservations()
    except Exception:
        logger.exception("خطا در آزادسازی رزروهای منقضی‌شده‌ی پرداخت‌نشده")
        return 0


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
    live_pending = live_pending_q(now=now)

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
        expire_stale_reservations()

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
        expire_stale_reservations()
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
                    filter=live_pending_q('seat_classes__reservations__', now),
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
        expire_stale_reservations()
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

        # Info for the "cancel flight" box.
        # A flight that is already CANCELLED may still hold reservations (e.g. a
        # refund failed, or the status was set by hand), and cancel_flight_and_refund
        # is safe to run again, so the button must stay available until none are left.
        has_active_reservations = flight_reservations.filter(
            status__in=[STATUS.PENDING_PAYMENT, STATUS.RESERVED]
        ).exists()
        status = self.object.status
        context['needs_cancel_retry'] = (
            status == Flight.StatusChoices.CANCELLED and has_active_reservations
        )
        context['can_cancel_flight'] = (
            status != Flight.StatusChoices.COMPLETED
            and (status != Flight.StatusChoices.CANCELLED or has_active_reservations)
        )
        context['cancel_info'] = {
            'paid_count': context['financials']['paid_count'],
            'pending_count': flight_reservations.filter(live_pending_q()).count(),
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
        seats_added = seats_removed = 0
        try:
            with transaction.atomic():
                # Lock order for management screens: Flight -> SeatClass -> Seat. The Flight
                # comes FIRST (it is also what seat generation locks first), and the booking
                # flow never locks a Flight, so no two operations can wait for each other.
                # The seat classes are locked BEFORE the forms are built and validated:
                # SeatClassForm derives `available_seats` from the instance's current values,
                # so those values must be read under the lock; otherwise a booking made while
                # the manager edits would be overwritten (overbooking).
                get_object_or_404(Flight.objects.select_for_update(), pk=self.kwargs['pk'])
                list(
                    SeatClass.objects.select_for_update()
                    .filter(flight_id=self.kwargs['pk'])
                    .order_by('pk')
                )
                flight = self.get_flight()
                form = FlightForm(request.POST, instance=flight)
                formset = SeatClassFormSet(request.POST, instance=flight)

                # Validate both so the manager sees every error at once.
                form_ok = form.is_valid()
                formset_ok = formset.is_valid()
                is_valid = form_ok and formset_ok

                if is_valid:
                    form.save()
                    formset.save()
                    # A capacity change must also add/remove the real Seat rows of classes
                    # that already have seats, otherwise the number and the seat map drift apart.
                    seats_added, seats_removed = resync_flight_seats(flight)
        except ProtectedError:
            logger.warning(
                f"تلاش ناموفق برای حذف کلاس صندلی دارای تاریخچه توسط={request.user.username}, "
                f"flight={flight.flight_number}"
            )
            messages.error(
                request,
                "یکی از کلاس‌های صندلی به‌خاطر داشتن تاریخچه‌ی رزرو (حتی کنسل‌شده) قابل حذف نیست. "
                "اگر هیچ‌یک از رزروهای آن پرداخت‌شده یا فعال نیست، می‌توانید از اکشن «حذف کلاس صندلی» "
                "در پنل ادمین جنگو استفاده کنید؛ کلاس‌های دارای رزرو پرداخت‌شده (حتی لغوشده) "
                "عمداً قابل حذف نیستند تا آمار مالی تغییر نکند."
            )
            return render(request, self.template_name, {
                'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
            })
        except ValueError as exc:
            # Raised when the seats cannot follow the new capacity (not enough free seats to
            # remove). The whole transaction was rolled back, nothing was saved.
            logger.warning(
                f"هماهنگی صندلی‌ها با ظرفیت ناموفق بود: flight={flight.flight_number} علت={exc}"
            )
            messages.error(request, str(exc))
            return render(request, self.template_name, {
                'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
            })

        if not is_valid:
            return render(request, self.template_name, {
                'form': form, 'formset': formset, 'is_edit': True, 'flight': flight,
            })

        logger.info(f"پرواز ویرایش شد توسط مدیر={request.user.username}: {flight.flight_number}")
        messages.success(request, "پرواز با موفقیت به‌روزرسانی شد.")
        if seats_added or seats_removed:
            messages.info(
                request,
                f"صندلی‌ها با ظرفیت جدید هماهنگ شد: {seats_added} صندلی اضافه و {seats_removed} صندلی حذف شد.",
            )

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
            except Exception:
                # The status is already saved as CANCELLED, but the refunds did not
                # finish. cancel_flight_and_refund is idempotent, so it can simply be
                # run again from the flight details page.
                logger.exception(
                    f"خطا در استرداد رزروها پس از لغو پرواز از فرم ویرایش: flight={flight.flight_number}"
                )
                messages.error(
                    request,
                    "وضعیت پرواز «لغو» ثبت شد ولی استرداد رزروها کامل انجام نشد. "
                    "از صفحه‌ی جزئیات پرواز دوباره «لغو پرواز» را بزنید."
                )
            else:
                flash_flight_cancel_result(request, flight, result)

        return redirect('dashboard:flight_manage_list')


class FlightCancelView(StaffRequiredMixin, View):
    """Cancel a whole flight: refund every paid reservation and release the unpaid ones."""

    def post(self, request, pk, *args, **kwargs):
        flight = get_object_or_404(Flight, pk=pk)

        try:
            result = cancel_flight_and_refund(flight.pk)
        except ReservationError as exc:
            messages.error(request, str(exc))
        except Exception:
            logger.exception(f"خطا در لغو پرواز: flight={flight.flight_number}")
            messages.error(
                request,
                "لغو پرواز با خطا مواجه شد. دوباره تلاش کنید؛ اگر تکرار شد لاگ‌ها را بررسی کنید."
            )
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
      pending           - waiting for payment (and still inside the payment window)
      unpaid_cancelled  - cancelled WITHOUT ever being paid (timeout / user)
      refunded          - paid and cancelled later (refund issued)
      active            - pending + paid (kept for old links)
    Any other value is treated as "all".
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
    VALID_FILTERS = frozenset({'paid', 'pending', 'unpaid_cancelled', 'refunded', 'active'})

    def get_current_filter(self):
        value = self.request.GET.get('filter', '')
        return value if value in self.VALID_FILTERS else ''

    def get_queryset(self):
        expire_stale_reservations()

        queryset = (
            Reservation.objects
            .select_related('user')
            .with_flight_info()
            .order_by('-created_at')
        )

        current = self.get_current_filter()
        if current == 'active':
            queryset = queryset.active()
        elif current == 'paid':
            queryset = queryset.filter(status=STATUS.RESERVED)
        elif current == 'pending':
            queryset = queryset.filter(live_pending_q())
        elif current == 'unpaid_cancelled':
            queryset = queryset.filter(status=STATUS.CANCELLED, paid_at__isnull=True)
        elif current == 'refunded':
            queryset = queryset.filter(status=STATUS.CANCELLED, paid_at__isnull=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['filter_tabs'] = self.FILTER_TABS
        context['current_filter'] = self.get_current_filter()
        return context


class UserManageListView(StaffRequiredMixin, ListView):
    """List of all registered users for the administrator."""
    model = CustomUser
    template_name = 'dashboard/user_manage_list.html'
    context_object_name = 'users'
    paginate_by = 20

    def get_queryset(self):
        return CustomUser.objects.order_by('-date_joined')