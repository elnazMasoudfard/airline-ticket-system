import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import F
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView, View

from flights.models import Seat, SeatClass
from .forms import PassengerForm, ReservationForm
from .models import Passenger, Reservation, ReservationSeat

logger = logging.getLogger('tickets')


class ReservationListView(LoginRequiredMixin, ListView):
    """لیست رزروهای خود کاربر (نه همه‌ی رزروهای سیستم)."""
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
    """جزئیات یک رزرو. کاربر فقط می‌تواند رزروهای خودش را ببیند."""
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
    مرحله‌ی اول رزرو: انتخاب تعداد صندلی برای یک کلاس پروازی مشخص.
    خود رزرو اینجا ساخته نمی‌شود؛ فقط بعد از تایید موجودی کیف پول،
    کاربر به صفحه‌ی انتخاب صندلی مشخص هدایت می‌شود.
    """
    template_name = 'tickets/reservation_create.html'

    def get_seat_class(self):
        return get_object_or_404(SeatClass, pk=self.kwargs['seat_class_id'])

    def get(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()
        form = ReservationForm()
        return render(request, self.template_name, {'seat_class': seat_class, 'form': form})

    def post(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()
        form = ReservationForm(request.POST)

        if not form.is_valid():
            return render(request, self.template_name, {'seat_class': seat_class, 'form': form})

        seats_count = form.cleaned_data['seats_count']
        total_price = seat_class.final_price * seats_count

        if request.user.wallet_balance < total_price:
            logger.warning(
                f"موجودی ناکافی: user={request.user.username}, needed={total_price}, "
                f"balance={request.user.wallet_balance}"
            )
            messages.error(request, "موجودی کیف پول کافی نیست. لطفاً ابتدا حساب خود را شارژ کنید.")
            return render(request, self.template_name, {'seat_class': seat_class, 'form': form})

        if seat_class.available_seats < seats_count:
            logger.warning(
                f"ظرفیت ناکافی: user={request.user.username}, seat_class={seat_class.pk}, "
                f"requested={seats_count}, available={seat_class.available_seats}"
            )
            messages.error(request, "ظرفیت کافی برای این تعداد صندلی وجود ندارد.")
            return render(request, self.template_name, {'seat_class': seat_class, 'form': form})

        url = reverse('tickets:seat_selection', kwargs={'seat_class_id': seat_class.pk})
        return redirect(f"{url}?count={seats_count}")


class SeatSelectionView(LoginRequiredMixin, View):
    """
    مرحله‌ی دوم رزرو: انتخاب صندلی‌های مشخص از روی نقشه‌ی صندلی.
    اگر بیش از ۱ صندلی انتخاب شود، باید در یک ردیف و کنار هم باشند.
    در صورت موفقیت: صندلی‌ها به‌صورت اتمیک قفل می‌شوند، رزرو ساخته می‌شود
    و مبلغ از کیف پول کسر می‌شود.
    """
    template_name = 'tickets/seat_selection.html'

    def get_seat_class(self):
        return get_object_or_404(SeatClass, pk=self.kwargs['seat_class_id'])

    def get_seats_count(self, request):
        try:
            count = int(request.GET.get('count') or request.POST.get('seats_count'))
        except (TypeError, ValueError):
            count = 1
        return max(1, count)

    def get(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()
        seats_count = self.get_seats_count(request)
        seats = seat_class.seats.all().order_by('row_number', 'column_letter')

        if not seats.exists():
            logger.warning(f"نقشه‌ی صندلی موجود نیست: seat_class={seat_class.pk}")
            messages.error(
                request,
                "برای این کلاس پروازی هنوز نقشه‌ی صندلی تعریف نشده است. لطفاً با پشتیبانی تماس بگیرید."
            )
            return redirect('flights:flight_detail', pk=seat_class.flight_id)

        return render(request, self.template_name, {
            'seat_class': seat_class,
            'seats': seats,
            'seats_count': seats_count,
        })

    def post(self, request, *args, **kwargs):
        seat_class = self.get_seat_class()
        seats_count = self.get_seats_count(request)
        selected_ids = request.POST.getlist('seat_ids')

        seats = seat_class.seats.all().order_by('row_number', 'column_letter')
        context = {'seat_class': seat_class, 'seats': seats, 'seats_count': seats_count}

        if len(selected_ids) != seats_count:
            messages.error(request, f"لطفاً دقیقاً {seats_count} صندلی انتخاب کنید.")
            return render(request, self.template_name, context)

        total_price = seat_class.final_price * seats_count
        if request.user.wallet_balance < total_price:
            logger.warning(
                f"موجودی ناکافی هنگام انتخاب صندلی: user={request.user.username}, needed={total_price}"
            )
            messages.error(request, "موجودی کیف پول کافی نیست.")
            return render(request, self.template_name, context)

        try:
            with transaction.atomic():
                # قفل کردن ردیف‌های انتخاب‌شده تا از رزرو هم‌زمان توسط دو کاربر جلوگیری شود
                locked_seats = list(
                    Seat.objects.select_for_update()
                    .filter(id__in=selected_ids, seat_class=seat_class, is_available=True)
                )

                if len(locked_seats) != seats_count:
                    logger.warning(
                        f"تداخل رزرو صندلی: user={request.user.username}, seat_class={seat_class.pk}, "
                        f"requested_ids={selected_ids}"
                    )
                    messages.error(request, "متاسفانه یک یا چند صندلی انتخابی شما توسط کاربر دیگری رزرو شد. لطفاً دوباره انتخاب کنید.")
                    return render(request, self.template_name, context)

                if seats_count > 1:
                    rows = {seat.row_number for seat in locked_seats}
                    if len(rows) != 1:
                        logger.info(
                            f"رد شد: صندلی‌های انتخابی هم‌ردیف نبودند: user={request.user.username}"
                        )
                        messages.error(request, "برای بیش از یک نفر، صندلی‌ها باید در یک ردیف و کنار هم باشند.")
                        return render(request, self.template_name, context)

                    columns = sorted(ord(seat.column_letter) for seat in locked_seats)
                    expected = list(range(columns[0], columns[0] + len(columns)))
                    if columns != expected:
                        logger.info(
                            f"رد شد: صندلی‌های انتخابی کنار هم نبودند: user={request.user.username}"
                        )
                        messages.error(request, "صندلی‌های انتخابی کنار هم نیستند. لطفاً صندلی‌های پیوسته انتخاب کنید.")
                        return render(request, self.template_name, context)

                Seat.objects.filter(id__in=[s.id for s in locked_seats]).update(is_available=False)
                SeatClass.objects.filter(pk=seat_class.pk).update(
                    available_seats=F('available_seats') - seats_count
                )

                reservation = Reservation.objects.create(
                    user=request.user,
                    seat_class=seat_class,
                    seats_count=seats_count,
                    total_paid_price=total_price,
                )

                ReservationSeat.objects.bulk_create([
                    ReservationSeat(reservation=reservation, seat=seat) for seat in locked_seats
                ])

                request.user.withdraw(total_price)

        except ValueError as e:
            logger.error(f"خطا در برداشت از کیف پول: user={request.user.username}, error={e}")
            messages.error(request, str(e))
            return render(request, self.template_name, context)

        logger.info(
            f"رزرو جدید ثبت شد: booking_reference={reservation.booking_reference}, "
            f"user={request.user.username}, flight={seat_class.flight.flight_number}, "
            f"seats={seats_count}, total_price={total_price}, "
            f"seat_numbers={[s.seat_number for s in locked_seats]}"
        )
        messages.success(
            request,
            f"رزرو با کد {reservation.booking_reference} ثبت شد. حالا اطلاعات مسافران را وارد کنید."
        )
        return redirect('tickets:add_passengers', booking_reference=reservation.booking_reference)


class AddPassengersView(LoginRequiredMixin, View):
    """مرحله‌ی سوم: دریافت اطلاعات مسافران به تعداد seats_count."""
    template_name = 'tickets/add_passengers.html'

    def get_reservation(self):
        return get_object_or_404(
            Reservation, booking_reference=self.kwargs['booking_reference'], user=self.request.user
        )

    def get_formset_class(self, seats_count):
        return modelformset_factory(Passenger, form=PassengerForm, extra=seats_count)

    def get(self, request, *args, **kwargs):
        reservation = self.get_reservation()
        formset_class = self.get_formset_class(reservation.seats_count)
        formset = formset_class(queryset=Passenger.objects.none())
        return render(request, self.template_name, {'reservation': reservation, 'formset': formset})

    def post(self, request, *args, **kwargs):
        reservation = self.get_reservation()
        formset_class = self.get_formset_class(reservation.seats_count)
        formset = formset_class(request.POST, queryset=Passenger.objects.none())

        if formset.is_valid():
            passengers = formset.save(commit=False)
            for passenger in passengers:
                passenger.reservation = reservation
                passenger.save()
            logger.info(
                f"اطلاعات مسافران ثبت شد: booking_reference={reservation.booking_reference}, "
                f"passenger_count={len(passengers)}"
            )
            messages.success(request, "اطلاعات مسافران با موفقیت ثبت شد.")
            return redirect('tickets:reservation_detail', booking_reference=reservation.booking_reference)

        return render(request, self.template_name, {'reservation': reservation, 'formset': formset})


class ReservationCancelView(LoginRequiredMixin, View):
    """
    کنسل کردن یک رزرو: آزادسازی صندلی‌های مشخص، برگرداندن شمارنده‌ی
    ظرفیت کلاس صندلی، محاسبه‌ی جریمه‌ی کنسلی و واریز مبلغ استرداد.
    """

    def post(self, request, *args, **kwargs):
        reservation = get_object_or_404(
            Reservation, booking_reference=kwargs['booking_reference'], user=request.user
        )

        if reservation.status == Reservation.StatusChoices.CANCELLED:
            messages.warning(request, "این رزرو قبلاً کنسل شده است.")
            return redirect('tickets:reservation_detail', booking_reference=reservation.booking_reference)

        penalty_percent = reservation.seat_class.flight.cancellation_penalty_percent
        refund_amount = (
            reservation.total_paid_price * (Decimal(100 - penalty_percent) / Decimal(100))
        ).quantize(Decimal('0.01'))

        with transaction.atomic():
            seat_ids = list(
                reservation.reservation_seats.values_list('seat_id', flat=True)
            )
            Seat.objects.filter(id__in=seat_ids).update(is_available=True)

            SeatClass.objects.filter(pk=reservation.seat_class_id).update(
                available_seats=F('available_seats') + reservation.seats_count
            )

            reservation.status = Reservation.StatusChoices.CANCELLED
            reservation.cancelled_at = timezone.now()
            reservation.refund_amount = refund_amount
            reservation.save(update_fields=['status', 'cancelled_at', 'refund_amount'])

            request.user.deposit(refund_amount)

        logger.info(
            f"رزرو کنسل شد: booking_reference={reservation.booking_reference}, "
            f"user={request.user.username}, penalty_percent={penalty_percent}, "
            f"refund_amount={refund_amount}"
        )
        messages.success(request, f"رزرو کنسل شد. مبلغ {refund_amount} تومان به کیف پول شما بازگشت.")
        return redirect('tickets:reservation_list')