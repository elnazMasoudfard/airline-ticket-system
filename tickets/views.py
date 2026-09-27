import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import F
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView, View
from django import forms
from django.forms import BaseModelFormSet

from flights.models import Seat, SeatClass, Flight
from .forms import PassengerForm, ReservationForm
from .models import Passenger, Reservation, ReservationSeat

logger = logging.getLogger('tickets')

def is_flight_bookable(flight):
    """
    A flight can be booked only if it is scheduled
    and its departure time is still in the future.
    """
    return (
        flight.status == Flight.StatusChoices.SCHEDULED
        and flight.departure_datetime > timezone.now()
    )


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
            .prefetch_related('passengers', 'reservation_seats__seat')
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
            .prefetch_related('passengers', 'reservation_seats__seat')
        )


class ReservationCreateView(LoginRequiredMixin, View):
    """
    First booking step: Selecting the number of seats
    for a specific flight class.
    """

    template_name = 'tickets/reservation_create.html'

    def get_seat_class(self):
        return get_object_or_404(
            SeatClass,
            pk=self.kwargs['seat_class_id']
        )

    def get(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()

        if not is_flight_bookable(seat_class.flight):
            messages.error(
                request,
                "این پرواز در حال حاضر قابل رزرو نیست."
            )
            return redirect(
                'flights:flight_detail',
                pk=seat_class.flight_id
            )

        form = ReservationForm()

        return render(
            request,
            self.template_name,
            {
                'seat_class': seat_class,
                'form': form,
            }
        )

    def post(self, request, *args, **kwargs):
        form = ReservationForm(request.POST)

        if not form.is_valid():
            seat_class = self.get_seat_class()
            return render(
                request,
                self.template_name,
                {
                    'seat_class': seat_class,
                    'form': form,
                }
            )

        seats_count = form.cleaned_data['seats_count']

        with transaction.atomic():
            seat_class = get_object_or_404(
                SeatClass.objects.select_for_update(),
                pk=self.kwargs['seat_class_id']
            )

            flight = Flight.objects.select_for_update().get(
                pk=seat_class.flight_id
            )

            if not is_flight_bookable(flight):
                messages.error(
                    request,
                    "این پرواز در حال حاضر قابل رزرو نیست."
                )
                return redirect(
                    'flights:flight_detail',
                    pk=flight.pk
                )

            total_price = seat_class.final_price * seats_count

            if request.user.wallet_balance < total_price:
                logger.warning(
                    f"موجودی ناکافی: user={request.user.username}, "
                    f"needed={total_price}, "
                    f"balance={request.user.wallet_balance}"
                )
                messages.error(
                    request,
                    "موجودی کیف پول کافی نیست. لطفاً ابتدا حساب خود را شارژ کنید."
                )
                return render(
                    request,
                    self.template_name,
                    {
                        'seat_class': seat_class,
                        'form': form,
                    }
                )

            if seat_class.available_seats < seats_count:
                logger.warning(
                    f"ظرفیت ناکافی: user={request.user.username}, "
                    f"seat_class={seat_class.pk}, "
                    f"requested={seats_count}, "
                    f"available={seat_class.available_seats}"
                )
                messages.error(
                    request,
                    "ظرفیت کافی برای این تعداد صندلی وجود ندارد."
                )
                return render(
                    request,
                    self.template_name,
                    {
                        'seat_class': seat_class,
                        'form': form,
                    }
                )

            url = reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': seat_class.pk}
            )

        return redirect(f"{url}?count={seats_count}")


class SeatSelectionView(LoginRequiredMixin, View):
    """
    Booking step two: Selecting specific seats.
    Only scheduled flights with future departure times
    can be booked.
    """

    template_name = 'tickets/seat_selection.html'

    def get_seat_class(self):
        return get_object_or_404(
            SeatClass,
            pk=self.kwargs['seat_class_id']
        )

    def get_seats_count(self, request):
        try:
            count = int(
                request.GET.get('count')
                or request.POST.get('seats_count')
            )
        except (TypeError, ValueError):
            count = 1

        return max(1, count)

    def get(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()

        if not is_flight_bookable(seat_class.flight):
            messages.error(
                request,
                "این پرواز در حال حاضر قابل رزرو نیست."
            )
            return redirect(
                'flights:flight_detail',
                pk=seat_class.flight_id
            )

        seats_count = self.get_seats_count(request)
        seats = seat_class.seats.all().order_by(
            'row_number',
            'column_letter'
        )

        if not seats.exists():
            logger.warning(
                f"نقشه‌ی صندلی موجود نیست: seat_class={seat_class.pk}"
            )
            messages.error(
                request,
                "برای این کلاس پروازی هنوز نقشه‌ی صندلی تعریف نشده است. "
                "لطفاً با پشتیبانی تماس بگیرید."
            )
            return redirect(
                'flights:flight_detail',
                pk=seat_class.flight_id
            )

        return render(
            request,
            self.template_name,
            {
                'seat_class': seat_class,
                'seats': seats,
                'seats_count': seats_count,
            }
        )

    def post(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()
        seats_count = self.get_seats_count(request)
        selected_ids = request.POST.getlist('seat_ids')

        seats = seat_class.seats.all().order_by(
            'row_number',
            'column_letter'
        )

        context = {
            'seat_class': seat_class,
            'seats': seats,
            'seats_count': seats_count,
        }

        if len(selected_ids) != seats_count:
            messages.error(
                request,
                f"لطفاً دقیقاً {seats_count} صندلی انتخاب کنید."
            )
            return render(
                request,
                self.template_name,
                context
            )

        try:
            with transaction.atomic():
                # Lock the seat class and flight while validating
                # and processing the booking.
                seat_class = get_object_or_404(
                    SeatClass.objects.select_for_update(),
                    pk=self.kwargs['seat_class_id']
                )

                flight = Flight.objects.select_for_update().get(
                    pk=seat_class.flight_id
                )

                context['seat_class'] = seat_class

                if not is_flight_bookable(flight):
                    messages.error(
                        request,
                        "این پرواز در حال حاضر قابل رزرو نیست."
                    )
                    return redirect(
                        'flights:flight_detail',
                        pk=flight.pk
                    )

                total_price = seat_class.final_price * seats_count

                if request.user.wallet_balance < total_price:
                    logger.warning(
                        f"موجودی ناکافی هنگام انتخاب صندلی: "
                        f"user={request.user.username}, "
                        f"needed={total_price}"
                    )
                    messages.error(
                        request,
                        "موجودی کیف پول کافی نیست."
                    )
                    return render(
                        request,
                        self.template_name,
                        context
                    )

                locked_seats = list(
                    Seat.objects.select_for_update()
                    .filter(
                        id__in=selected_ids,
                        seat_class=seat_class,
                        is_available=True
                    )
                )

                if len(locked_seats) != seats_count:
                    logger.warning(
                        f"تداخل رزرو صندلی: "
                        f"user={request.user.username}, "
                        f"seat_class={seat_class.pk}, "
                        f"requested_ids={selected_ids}"
                    )
                    messages.error(
                        request,
                        "متاسفانه یک یا چند صندلی انتخابی شما "
                        "توسط کاربر دیگری رزرو شد. لطفاً دوباره انتخاب کنید."
                    )
                    return render(
                        request,
                        self.template_name,
                        context
                    )

                if seats_count > 1:
                    rows = {
                        seat.row_number for seat in locked_seats
                    }

                    if len(rows) != 1:
                        logger.info(
                            "رد شد: صندلی‌های انتخابی هم‌ردیف نبودند: "
                            f"user={request.user.username}"
                        )
                        messages.error(
                            request,
                            "برای بیش از یک نفر، صندلی‌ها باید "
                            "در یک ردیف و کنار هم باشند."
                        )
                        return render(
                            request,
                            self.template_name,
                            context
                        )

                    columns = sorted(
                        ord(seat.column_letter)
                        for seat in locked_seats
                    )

                    expected = list(
                        range(
                            columns[0],
                            columns[0] + len(columns)
                        )
                    )

                    if columns != expected:
                        logger.info(
                            "رد شد: صندلی‌های انتخابی کنار هم نبودند: "
                            f"user={request.user.username}"
                        )
                        messages.error(
                            request,
                            "صندلی‌های انتخابی کنار هم نیستند. "
                            "لطفاً صندلی‌های پیوسته انتخاب کنید."
                        )
                        return render(
                            request,
                            self.template_name,
                            context
                        )

                Seat.objects.filter(
                    id__in=[seat.id for seat in locked_seats]
                ).update(
                    is_available=False,
                    updated_at=timezone.now()
                )

                SeatClass.objects.filter(
                    pk=seat_class.pk
                ).update(
                    available_seats=F('available_seats') - seats_count,
                    updated_at=timezone.now()
                )

                reservation = Reservation.objects.create(
                    user=request.user,
                    seat_class=seat_class,
                    seats_count=seats_count,
                    total_paid_price=total_price,
                )

                ReservationSeat.objects.bulk_create([
                    ReservationSeat(
                        reservation=reservation,
                        seat=seat
                    )
                    for seat in locked_seats
                ])

                request.user.withdraw(total_price)

        except ValueError as e:
            logger.error(
                f"خطا در برداشت از کیف پول: "
                f"user={request.user.username}, error={e}"
            )
            messages.error(request, str(e))
            return render(
                request,
                self.template_name,
                context
            )

        logger.info(
            f"رزرو جدید ثبت شد: "
            f"booking_reference={reservation.booking_reference}, "
            f"user={request.user.username}, "
            f"flight={flight.flight_number}, "
            f"seats={seats_count}, "
            f"total_price={total_price}, "
            f"seat_numbers={[s.seat_number for s in locked_seats]}"
        )

        messages.success(
            request,
            f"رزرو با کد {reservation.booking_reference} ثبت شد. "
            "حالا اطلاعات مسافران را وارد کنید."
        )

        return redirect(
            'tickets:add_passengers',
            booking_reference=reservation.booking_reference
        )
class PassengerBaseFormSet(BaseModelFormSet):
    def clean(self):
        super().clean()

        # If the separate forms contain errors,
        # there is no need to check the national ID numbers.
        if any(self.errors):
            return

        national_ids = []

        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue

            if not form.cleaned_data:
                continue

            if form.cleaned_data.get('DELETE', False):
                continue

            national_id = form.cleaned_data.get('national_id')

            if national_id:
                national_ids.append(national_id)

        # Checking for duplicate National IDs in this same reservation
        if len(national_ids) != len(set(national_ids)):
            raise forms.ValidationError(
                "کد ملی مسافران یک رزرو نباید تکراری باشد."
            )

class AddPassengersView(LoginRequiredMixin, View):
    """Step 3: Collect passenger information for all reserved seats."""

    template_name = 'tickets/add_passengers.html'

    def get_reservation(self):
        return get_object_or_404(
            Reservation.objects.filter(
                status=Reservation.StatusChoices.RESERVED
            ),
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

    def get(self, request, *args, **kwargs):
        reservation = self.get_reservation()

        existing_passengers = reservation.passengers.all()
        existing_count = existing_passengers.count()

        # If the information for all passengers has already been recorded,
        # doesn't display the information entry form again.
        if existing_count >= reservation.seats_count:
            messages.info(
                request,
                "اطلاعات همه مسافران این رزرو قبلاً ثبت شده است."
            )
            return redirect(
                'tickets:reservation_detail',
                booking_reference=reservation.booking_reference,
            )

        formset_class = self.get_formset_class(
            reservation.seats_count,
            existing_count,
        )

        formset = formset_class(queryset=existing_passengers)

        return render(
            request,
            self.template_name,
            {
                'reservation': reservation,
                'formset': formset,
            },
        )

    def post(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                # Reservation lock to prevent the simultaneous submission of multiple requests
                reservation = get_object_or_404(
                    Reservation.objects.select_for_update().filter(
                        status=Reservation.StatusChoices.RESERVED
                    ),
                    booking_reference=kwargs['booking_reference'],
                    user=request.user,
                )

                existing_passengers = reservation.passengers.all()
                existing_count = existing_passengers.count()

                # If the information has already been fully recorded,
                # a duplicate request should not create a new passenger.
                if existing_count >= reservation.seats_count:
                    messages.info(
                        request,
                        "اطلاعات مسافران این رزرو قبلاً ثبت شده است."
                    )
                    return redirect(
                        'tickets:reservation_detail',
                        booking_reference=reservation.booking_reference,
                    )

                formset_class = self.get_formset_class(
                    reservation.seats_count,
                    existing_count,
                )

                formset = formset_class(
                    request.POST,
                    queryset=existing_passengers,
                )

                if not formset.is_valid():
                    return render(
                        request,
                        self.template_name,
                        {
                            'reservation': reservation,
                            'formset': formset,
                        },
                    )

                passengers = formset.save(commit=False)

                for passenger in passengers:
                    passenger.reservation = reservation
                    passenger.save()

                # The final number of passengers must exactly match the number of seats.
                final_count = reservation.passengers.count()

                if final_count != reservation.seats_count:
                    raise ValueError(
                        "تعداد مسافران ثبت‌شده با تعداد صندلی‌ها مطابقت ندارد."
                    )

        except IntegrityError:
            logger.exception(
                "خطای دیتابیس هنگام ثبت مسافران: "
                f"booking_reference={kwargs['booking_reference']}, "
                f"user={request.user.username}"
            )
            messages.error(
                request,
                "ثبت اطلاعات مسافران به دلیل تکراری بودن یا تداخل اطلاعات انجام نشد. "
                "لطفاً اطلاعات را بررسی و دوباره تلاش کنید."
            )
            reservation = self.get_reservation()
            existing_passengers = reservation.passengers.all()
            formset_class = self.get_formset_class(
                reservation.seats_count,
                existing_passengers.count(),
            )
            formset = formset_class(queryset=existing_passengers)

            return render(
                request,
                self.template_name,
                {
                    'reservation': reservation,
                    'formset': formset,
                },
            )

        except ValueError:
            logger.exception(
                "تعداد مسافران پس از ثبت با تعداد صندلی‌ها مطابقت نداشت: "
                f"booking_reference={kwargs['booking_reference']}"
            )
            messages.error(
                request,
                "ثبت اطلاعات کامل نشد. لطفاً دوباره تلاش کنید."
            )
            reservation = self.get_reservation()
            existing_passengers = reservation.passengers.all()
            formset_class = self.get_formset_class(
                reservation.seats_count,
                existing_passengers.count(),
            )
            formset = formset_class(queryset=existing_passengers)

            return render(
                request,
                self.template_name,
                {
                    'reservation': reservation,
                    'formset': formset,
                },
            )

        logger.info(
            f"اطلاعات مسافران ثبت شد: "
            f"booking_reference={reservation.booking_reference}, "
            f"passenger_count={reservation.passengers.count()}"
        )

        messages.success(request, "اطلاعات مسافران با موفقیت ثبت شد.")

        return redirect(
            'tickets:reservation_detail',
            booking_reference=reservation.booking_reference,
        )

class ReservationCancelView(LoginRequiredMixin, View):
    """
    Cancel a reservation safely.

    Cancellation is allowed when:
    - The flight is still scheduled and departure is in the future.
    - The flight itself has been cancelled.

    Cancellation is not allowed after a flight has started
    or been completed.
    """

    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_for_update(),
                booking_reference=kwargs['booking_reference'],
                user=request.user,
            )

            if reservation.status == Reservation.StatusChoices.CANCELLED:
                messages.warning(
                    request,
                    "این رزرو قبلاً کنسل شده است."
                )
                return redirect(
                    'tickets:reservation_detail',
                    booking_reference=reservation.booking_reference
                )

            flight = Flight.objects.select_for_update().get(
                pk=reservation.seat_class.flight_id
            )

            # A cancelled flight can still have its reservation cancelled.
            # Otherwise, only future scheduled flights can be cancelled.
            can_cancel = (
                flight.status == Flight.StatusChoices.CANCELLED
                or (
                    flight.status == Flight.StatusChoices.SCHEDULED
                    and flight.departure_datetime > timezone.now()
                )
            )

            if not can_cancel:
                messages.error(
                    request,
                    "لغو این رزرو به دلیل وضعیت یا زمان پرواز امکان‌پذیر نیست."
                )
                return redirect(
                    'tickets:reservation_detail',
                    booking_reference=reservation.booking_reference
                )

            penalty_percent = flight.cancellation_penalty_percent

            refund_amount = (
                reservation.total_paid_price
                * (Decimal(100 - penalty_percent) / Decimal(100))
            ).quantize(Decimal('0.01'))

            seat_ids = list(
                reservation.reservation_seats.values_list(
                    'seat_id',
                    flat=True
                )
            )

            Seat.objects.filter(id__in=seat_ids).update(
                is_available=True,
                updated_at=timezone.now(),
            )

            SeatClass.objects.filter(
                pk=reservation.seat_class_id
            ).update(
                available_seats=F('available_seats') + reservation.seats_count,
                updated_at=timezone.now(),
            )

            reservation.reservation_seats.all().delete()

            reservation.status = Reservation.StatusChoices.CANCELLED
            reservation.cancelled_at = timezone.now()
            reservation.refund_amount = refund_amount

            reservation.save(
                update_fields=[
                    'status',
                    'cancelled_at',
                    'refund_amount',
                    'updated_at',
                ]
            )

            if refund_amount > 0:
                request.user.deposit(refund_amount)

        logger.info(
            f"رزرو کنسل شد: "
            f"booking_reference={reservation.booking_reference}, "
            f"user={request.user.username}, "
            f"flight={flight.flight_number}, "
            f"penalty_percent={penalty_percent}, "
            f"refund_amount={refund_amount}"
        )

        messages.success(
            request,
            f"رزرو کنسل شد. مبلغ {refund_amount} تومان "
            "به کیف پول شما بازگشت."
        )

        return redirect('tickets:reservation_list')