import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.forms import BaseModelFormSet, modelformset_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from flights.models import SeatClass

from .forms import PassengerForm, ReservationForm
from .models import Passenger, Reservation
from .services import (
    MAX_PENDING_RESERVATIONS_PER_USER,
    MAX_SEATS_PER_RESERVATION,
    PAYMENT_WINDOW_MINUTES,
    AlreadyCancelledError,
    BookingError,
    CancellationNotAllowedError,
    FlightNotBookableError,
    PayResult,
    cancel_reservation,
    create_pending_reservation,
    expire_reservation_if_needed,
    get_penalty_percent,
    has_adjacent_block,
    is_flight_bookable,
    pay_reservation,
    pending_limit_reached,
)

logger = logging.getLogger('tickets')

FLIGHT_NOT_BOOKABLE_MSG = "این پرواز در حال حاضر قابل رزرو نیست."


def redirect_to_detail(reservation):
    return redirect(
        'tickets:reservation_detail',
        booking_reference=reservation.booking_reference,
    )


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------
class ReservationListView(LoginRequiredMixin, ListView):
    """List of the user's reservations (not all system reservations)."""
    model = Reservation
    template_name = 'tickets/reservation_list.html'
    context_object_name = 'reservations'
    paginate_by = 10

    def get_queryset(self):
        return (
            Reservation.objects
            .for_user(self.request.user)
            .with_flight_info()
        )


class ReservationDetailView(LoginRequiredMixin, DetailView):
    """Reservation details. Users can only view their own reservations."""
    model = Reservation
    template_name = 'tickets/reservation_detail.html'
    context_object_name = 'reservation'
    slug_field = 'booking_reference'
    slug_url_kwarg = 'booking_reference'

    def get_queryset(self):
        return (
            Reservation.objects
            .for_user(self.request.user)
            .with_flight_info()
            .prefetch_related('passengers', 'reservation_seats__seat')
        )

    def get_object(self, queryset=None):
        reservation = super().get_object(queryset)
        # Lazy expiry: an overdue pending reservation is cancelled right now,
        # so the page never offers to pay for something that has expired.
        if expire_reservation_if_needed(reservation):
            reservation = super().get_object(queryset)
        return reservation

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        reservation = context['reservation']
        context['payment_window_minutes'] = PAYMENT_WINDOW_MINUTES
        if reservation.status == Reservation.StatusChoices.RESERVED:
            context['penalty_percent'] = get_penalty_percent(
                reservation, reservation.seat_class.flight
            )
        return context


# ---------------------------------------------------------------------------
# Step 1: number of seats
# ---------------------------------------------------------------------------
class ReservationCreateView(LoginRequiredMixin, View):
    """First booking step: choosing the number of seats for a seat class."""

    template_name = 'tickets/reservation_create.html'

    def get_seat_class(self):
        return get_object_or_404(
            SeatClass.objects.select_related('flight'),
            pk=self.kwargs['seat_class_id'],
        )

    def render_form(self, request, seat_class, form):
        return render(
            request,
            self.template_name,
            {'seat_class': seat_class, 'form': form},
        )

    def check_can_book(self, request, seat_class):
        """Returns a redirect response if booking is not possible, else None."""
        if not is_flight_bookable(seat_class.flight):
            messages.error(request, FLIGHT_NOT_BOOKABLE_MSG)
            return redirect('flights:flight_detail', pk=seat_class.flight_id)

        if pending_limit_reached(request.user):
            messages.error(
                request,
                f"شما {MAX_PENDING_RESERVATIONS_PER_USER} رزرو در انتظار پرداخت دارید. "
                "ابتدا آن‌ها را پرداخت یا لغو کنید.",
            )
            return redirect('tickets:reservation_list')

        return None

    def get(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()

        blocked = self.check_can_book(request, seat_class)
        if blocked:
            return blocked

        return self.render_form(request, seat_class, ReservationForm())

    def post(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()

        blocked = self.check_can_book(request, seat_class)
        if blocked:
            return blocked

        form = ReservationForm(request.POST)
        if not form.is_valid():
            return self.render_form(request, seat_class, form)

        seats_count = form.cleaned_data['seats_count']

        # Informational pre-checks. The real, locked checks happen in the
        # seat selection step (create_pending_reservation).
        if seat_class.available_seats < seats_count:
            logger.warning(
                "insufficient capacity: user=%s seat_class=%s requested=%s available=%s",
                request.user.pk, seat_class.pk, seats_count, seat_class.available_seats,
            )
            messages.error(request, "ظرفیت کافی برای این تعداد صندلی وجود ندارد.")
            return self.render_form(request, seat_class, form)

        if not has_adjacent_block(seat_class, seats_count):
            messages.error(
                request,
                "برای این تعداد مسافر، صندلی‌های پیوسته در یک ردیف باقی نمانده است. "
                "تعداد کمتری انتخاب کنید یا چند رزرو جداگانه انجام دهید.",
            )
            return self.render_form(request, seat_class, form)

        url = reverse(
            'tickets:seat_selection',
            kwargs={'seat_class_id': seat_class.pk},
        )
        return redirect(f"{url}?count={seats_count}")


# ---------------------------------------------------------------------------
# Step 2: seat selection (creates the pending reservation)
# ---------------------------------------------------------------------------
class SeatSelectionView(LoginRequiredMixin, View):
    """
    Booking step two: selecting specific seats. The seats are held for
    PAYMENT_WINDOW_MINUTES while the user enters passengers and pays.
    """

    template_name = 'tickets/seat_selection.html'

    def get_seat_class(self):
        return get_object_or_404(
            SeatClass.objects.select_related('flight'),
            pk=self.kwargs['seat_class_id'],
        )

    def get_seats_count(self, request):
        try:
            count = int(
                request.GET.get('count')
                or request.POST.get('seats_count')
            )
        except (TypeError, ValueError):
            count = 1

        return min(max(1, count), MAX_SEATS_PER_RESERVATION)

    def build_context(self, seat_class, seats_count):
        seats = seat_class.seats.all().order_by('row_number', 'column_letter')
        return {
            'seat_class': seat_class,
            'seats': seats,
            'seats_count': seats_count,
            'payment_window_minutes': PAYMENT_WINDOW_MINUTES,
        }

    def get(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()

        if not is_flight_bookable(seat_class.flight):
            messages.error(request, FLIGHT_NOT_BOOKABLE_MSG)
            return redirect('flights:flight_detail', pk=seat_class.flight_id)

        context = self.build_context(
            seat_class, self.get_seats_count(request)
        )

        if not context['seats'].exists():
            logger.warning("seat map missing: seat_class=%s", seat_class.pk)
            messages.error(
                request,
                "برای این کلاس پروازی هنوز نقشه‌ی صندلی تعریف نشده است. "
                "لطفاً با پشتیبانی تماس بگیرید.",
            )
            return redirect('flights:flight_detail', pk=seat_class.flight_id)

        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()
        seats_count = self.get_seats_count(request)
        context = self.build_context(seat_class, seats_count)

        try:
            seat_ids = [int(sid) for sid in request.POST.getlist('seat_ids')]
        except ValueError:
            messages.error(request, "انتخاب صندلی نامعتبر است. لطفاً دوباره تلاش کنید.")
            return render(request, self.template_name, context)

        try:
            reservation = create_pending_reservation(
                user=request.user,
                seat_class_id=seat_class.pk,
                seat_ids=seat_ids,
                seats_count=seats_count,
            )
        except FlightNotBookableError as exc:
            messages.error(request, str(exc))
            return redirect('flights:flight_detail', pk=seat_class.flight_id)
        except BookingError as exc:
            messages.error(request, str(exc))
            return render(request, self.template_name, context)
        except IntegrityError:
            # e.g. a PNR collision or a seat that got linked in the meantime
            logger.exception(
                "integrity error while creating reservation: user=%s seat_class=%s",
                request.user.pk, seat_class.pk,
            )
            messages.error(
                request,
                "ثبت رزرو با خطا مواجه شد. لطفاً دوباره تلاش کنید.",
            )
            return render(request, self.template_name, context)

        messages.success(
            request,
            f"صندلی‌ها به مدت {PAYMENT_WINDOW_MINUTES} دقیقه برای شما نگه داشته شدند. "
            "حالا اطلاعات مسافران را وارد کنید؛ پرداخت در مرحله‌ی بعد انجام می‌شود.",
        )
        return redirect(
            'tickets:add_passengers',
            booking_reference=reservation.booking_reference,
        )


# ---------------------------------------------------------------------------
# Step 3: passengers
# ---------------------------------------------------------------------------
class PassengerBaseFormSet(BaseModelFormSet):
    def clean(self):
        super().clean()

        # If the separate forms contain errors,
        # there is no need to check the national ID numbers.
        if any(self.errors):
            return

        national_ids = []

        for form in self.forms:
            if not hasattr(form, 'cleaned_data') or not form.cleaned_data:
                continue
            if form.cleaned_data.get('DELETE', False):
                continue

            national_id = form.cleaned_data.get('national_id')
            if national_id:
                national_ids.append(national_id)

        # PassengerForm normalises digits, so Persian/English variants
        # of the same number are detected here as well.
        if len(national_ids) != len(set(national_ids)):
            raise forms.ValidationError(
                "کد ملی مسافران یک رزرو نباید تکراری باشد."
            )


class AddPassengersView(LoginRequiredMixin, View):
    """Step 3: Collect passenger information for all reserved seats."""

    template_name = 'tickets/add_passengers.html'

    def get_reservation(self):
        return get_object_or_404(
            Reservation,
            booking_reference=self.kwargs['booking_reference'],
            user=self.request.user,
        )

    def get_formset_class(self, seats_count, existing_count=0):
        remaining_count = max(0, seats_count - existing_count)

        return modelformset_factory(
            Passenger,
            form=PassengerForm,
            formset=PassengerBaseFormSet,
            extra=remaining_count,
            min_num=seats_count,
            max_num=seats_count,
            validate_min=True,
            validate_max=True,
        )

    def guard(self, request, reservation):
        """Redirect when the reservation can no longer receive passengers."""
        if expire_reservation_if_needed(reservation):
            messages.error(
                request,
                f"مهلت {PAYMENT_WINDOW_MINUTES} دقیقه‌ای این رزرو به پایان رسیده است. "
                "رزرو لغو شد و صندلی‌ها آزاد شدند.",
            )
            return redirect_to_detail(reservation)

        if reservation.status != Reservation.StatusChoices.PENDING_PAYMENT:
            messages.info(request, "این رزرو در وضعیت قابل ویرایش نیست.")
            return redirect_to_detail(reservation)

        return None

    def render_fresh_form(self, request):
        """Re-render an empty/current form after a failed save."""
        reservation = self.get_reservation()
        existing_passengers = reservation.passengers.all()
        formset_class = self.get_formset_class(
            reservation.seats_count,
            existing_passengers.count(),
        )
        return render(
            request,
            self.template_name,
            {
                'reservation': reservation,
                'formset': formset_class(queryset=existing_passengers),
            },
        )

    def get(self, request, *args, **kwargs):
        reservation = self.get_reservation()

        blocked = self.guard(request, reservation)
        if blocked:
            return blocked

        existing_passengers = reservation.passengers.all()
        existing_count = existing_passengers.count()

        # Everything has been entered already; nothing to show.
        if existing_count >= reservation.seats_count:
            messages.info(request, "اطلاعات همه مسافران این رزرو قبلاً ثبت شده است.")
            return redirect_to_detail(reservation)

        formset_class = self.get_formset_class(
            reservation.seats_count, existing_count
        )

        return render(
            request,
            self.template_name,
            {
                'reservation': reservation,
                'formset': formset_class(queryset=existing_passengers),
            },
        )

    def post(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                # Lock the reservation so double submits can't create duplicates.
                reservation = get_object_or_404(
                    Reservation.objects.select_for_update(),
                    booking_reference=kwargs['booking_reference'],
                    user=request.user,
                )

                if reservation.status != Reservation.StatusChoices.PENDING_PAYMENT:
                    messages.info(request, "این رزرو در وضعیت قابل ویرایش نیست.")
                    return redirect_to_detail(reservation)

                if reservation.is_payment_expired:
                    # The detail page performs the actual expiry (lazy expiry).
                    messages.error(
                        request,
                        f"مهلت {PAYMENT_WINDOW_MINUTES} دقیقه‌ای این رزرو به پایان رسیده است.",
                    )
                    return redirect_to_detail(reservation)

                existing_passengers = reservation.passengers.all()
                existing_count = existing_passengers.count()

                if existing_count >= reservation.seats_count:
                    messages.info(request, "اطلاعات مسافران این رزرو قبلاً ثبت شده است.")
                    return redirect_to_detail(reservation)

                formset_class = self.get_formset_class(
                    reservation.seats_count, existing_count
                )
                formset = formset_class(
                    request.POST, queryset=existing_passengers
                )

                if not formset.is_valid():
                    return render(
                        request,
                        self.template_name,
                        {'reservation': reservation, 'formset': formset},
                    )

                for passenger in formset.save(commit=False):
                    passenger.reservation = reservation
                    passenger.save()

                if reservation.passengers.count() != reservation.seats_count:
                    raise ValueError(
                        "تعداد مسافران ثبت‌شده با تعداد صندلی‌ها مطابقت ندارد."
                    )

        except IntegrityError:
            logger.exception(
                "db error while saving passengers: ref=%s user=%s",
                kwargs['booking_reference'], request.user.pk,
            )
            messages.error(
                request,
                "ثبت اطلاعات مسافران به دلیل تکراری بودن یا تداخل اطلاعات انجام نشد. "
                "لطفاً اطلاعات را بررسی و دوباره تلاش کنید.",
            )
            return self.render_fresh_form(request)

        except ValueError:
            logger.exception(
                "passenger count mismatch: ref=%s", kwargs['booking_reference']
            )
            messages.error(request, "ثبت اطلاعات کامل نشد. لطفاً دوباره تلاش کنید.")
            return self.render_fresh_form(request)

        logger.info(
            "passengers saved: ref=%s count=%s",
            reservation.booking_reference, reservation.seats_count,
        )
        messages.success(request, "اطلاعات مسافران با موفقیت ثبت شد.")
        return redirect(
            'tickets:reservation_payment',
            booking_reference=reservation.booking_reference,
        )


# ---------------------------------------------------------------------------
# Step 4: payment
# ---------------------------------------------------------------------------
class ReservationPaymentView(LoginRequiredMixin, View):
    """Finalize a pending reservation by atomically charging the user's wallet."""

    template_name = 'tickets/reservation_payment.html'

    def get_reservation(self):
        return get_object_or_404(
            Reservation.objects.select_related('seat_class__flight'),
            booking_reference=self.kwargs['booking_reference'],
            user=self.request.user,
        )

    def get(self, request, *args, **kwargs):
        reservation = self.get_reservation()

        if expire_reservation_if_needed(reservation):
            messages.error(
                request,
                f"مهلت {PAYMENT_WINDOW_MINUTES} دقیقه‌ای پرداخت این رزرو به پایان رسیده است. "
                "رزرو لغو شد و صندلی‌ها آزاد شدند.",
            )
            return redirect_to_detail(reservation)

        if reservation.status == Reservation.StatusChoices.RESERVED:
            messages.info(request, "این رزرو قبلاً پرداخت و نهایی شده است.")
            return redirect_to_detail(reservation)

        if reservation.status != Reservation.StatusChoices.PENDING_PAYMENT:
            messages.error(request, "این رزرو در وضعیت قابل پرداخت نیست.")
            return redirect_to_detail(reservation)

        if reservation.passengers.count() != reservation.seats_count:
            messages.error(request, "ابتدا اطلاعات تمام مسافران را تکمیل کنید.")
            return redirect(
                'tickets:add_passengers',
                booking_reference=reservation.booking_reference,
            )

        return render(
            request,
            self.template_name,
            {
                'reservation': reservation,
                'payment_window_minutes': PAYMENT_WINDOW_MINUTES,
            },
        )

    def post(self, request, *args, **kwargs):
        try:
            result, reservation = pay_reservation(
                booking_reference=kwargs['booking_reference'],
                user=request.user,
            )
        except Reservation.DoesNotExist:
            raise Http404

        if result is PayResult.PAID:
            messages.success(
                request, "پرداخت با موفقیت انجام شد و رزرو شما قطعی شد."
            )
            return redirect_to_detail(reservation)

        if result is PayResult.ALREADY_PAID:
            messages.info(request, "این رزرو قبلاً پرداخت شده است.")
            return redirect_to_detail(reservation)

        if result is PayResult.EXPIRED:
            messages.error(
                request,
                f"مهلت {PAYMENT_WINDOW_MINUTES} دقیقه‌ای پرداخت این رزرو به پایان رسیده است. "
                "رزرو لغو شد و صندلی‌ها آزاد شدند.",
            )
            return redirect_to_detail(reservation)

        if result is PayResult.PASSENGERS_INCOMPLETE:
            messages.error(request, "ابتدا اطلاعات تمام مسافران را تکمیل کنید.")
            return redirect(
                'tickets:add_passengers',
                booking_reference=reservation.booking_reference,
            )

        if result is PayResult.FLIGHT_NOT_BOOKABLE:
            messages.error(
                request, "این پرواز دیگر قابل پرداخت نیست؛ رزرو را لغو کنید."
            )
            return redirect_to_detail(reservation)

        if result is PayResult.INSUFFICIENT_BALANCE:
            messages.error(
                request,
                "موجودی کیف پول کافی نیست. رزرو شما همچنان در انتظار پرداخت است.",
            )
            return redirect(
                'tickets:reservation_payment',
                booking_reference=reservation.booking_reference,
            )

        # PayResult.NOT_PAYABLE
        messages.error(request, "این رزرو در وضعیت قابل پرداخت نیست.")
        return redirect_to_detail(reservation)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
class ReservationCancelView(LoginRequiredMixin, View):
    """
    Cancel a reservation safely.

    Allowed when the flight is still scheduled in the future, or when the
    flight itself has been cancelled (then the refund is 100%).
    """

    def post(self, request, *args, **kwargs):
        booking_reference = kwargs['booking_reference']

        try:
            outcome = cancel_reservation(
                booking_reference=booking_reference,
                user=request.user,
            )
        except Reservation.DoesNotExist:
            raise Http404
        except AlreadyCancelledError as exc:
            messages.warning(request, str(exc))
            return redirect(
                'tickets:reservation_detail', booking_reference=booking_reference
            )
        except CancellationNotAllowedError as exc:
            messages.error(request, str(exc))
            return redirect(
                'tickets:reservation_detail', booking_reference=booking_reference
            )

        if outcome.refund_amount > 0:
            messages.success(
                request,
                f"رزرو کنسل شد و مبلغ {outcome.refund_amount:,.0f} تومان به کیف پول شما بازگشت.",
            )
        else:
            messages.success(request, "رزرو با موفقیت لغو شد.")

        return redirect('tickets:reservation_list')