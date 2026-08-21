from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView, View

from flights.models import SeatClass
from .forms import PassengerForm, ReservationForm
from .models import Passenger, Reservation


class ReservationListView(LoginRequiredMixin, ListView):
    """لیست رزروهای خود کاربر (نه همه‌ی رزروهای سیستم)."""
    model = Reservation
    template_name = 'tickets/reservation_list.html'
    context_object_name = 'reservations'
    paginate_by = 10

    def get_queryset(self):
        return (
            Reservation.objects
            .filter(user=self.request.user)
            .select_related(
                'seat_class__flight__route__origin',
                'seat_class__flight__route__destination',
                'seat_class__flight__airline',
            )
            .prefetch_related('passengers')
        )


class ReservationDetailView(LoginRequiredMixin, DetailView):
    """جزئیات یک رزرو. کاربر فقط می‌تواند رزروهای خودش را ببیند."""
    model = Reservation
    template_name = 'tickets/reservation_detail.html'
    context_object_name = 'reservation'
    slug_field = 'booking_reference'
    slug_url_kwarg = 'booking_reference'

    def get_queryset(self):
        return Reservation.objects.filter(user=self.request.user).prefetch_related('passengers')


class ReservationCreateView(LoginRequiredMixin, View):
    """
    مرحله‌ی اول رزرو: انتخاب تعداد صندلی برای یک کلاس پروازی مشخص،
    کسر اتمیک از موجودی کیف پول و ثبت خود رزرو.
    اطلاعات مسافران در مرحله‌ی بعد (AddPassengersView) گرفته می‌شود.
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
            messages.error(request, "موجودی کیف پول کافی نیست. لطفاً ابتدا حساب خود را شارژ کنید.")
            return render(request, self.template_name, {'seat_class': seat_class, 'form': form})

        try:
            with transaction.atomic():
                # این متد خودش به‌صورت اتمیک است و در صورت نبود ظرفیت کافی خطا می‌دهد
                seat_class.reserve_seats(seats_count)

                reservation = Reservation.objects.create(
                    user=request.user,
                    seat_class=seat_class,
                    seats_count=seats_count,
                    total_paid_price=total_price,
                )

                request.user.withdraw(total_price)

        except ValueError as e:
            messages.error(request, str(e))
            return render(request, self.template_name, {'seat_class': seat_class, 'form': form})

        messages.success(request, f"رزرو با کد {reservation.booking_reference} ثبت شد. حالا اطلاعات مسافران را وارد کنید.")
        return redirect('tickets:add_passengers', booking_reference=reservation.booking_reference)


class AddPassengersView(LoginRequiredMixin, View):
    """
    مرحله‌ی دوم رزرو: دریافت اطلاعات مسافران به تعداد seats_count
    که در مرحله‌ی قبل مشخص شده است.
    """
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
            messages.success(request, "اطلاعات مسافران با موفقیت ثبت شد.")
            return redirect('tickets:reservation_detail', booking_reference=reservation.booking_reference)

        return render(request, self.template_name, {'reservation': reservation, 'formset': formset})


class ReservationCancelView(LoginRequiredMixin, View):
    """
    کنسل کردن یک رزرو: آزادسازی صندلی‌ها، محاسبه‌ی جریمه‌ی کنسلی
    بر اساس درصد جریمه‌ی همان پرواز، و واریز مبلغ استرداد به کیف پول.
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
            reservation.seat_class.release_seats(reservation.seats_count)
            reservation.status = Reservation.StatusChoices.CANCELLED
            reservation.cancelled_at = timezone.now()
            reservation.refund_amount = refund_amount
            reservation.save(update_fields=['status', 'cancelled_at', 'refund_amount'])
            request.user.deposit(refund_amount)

        messages.success(request, f"رزرو کنسل شد. مبلغ {refund_amount} تومان به کیف پول شما بازگشت.")
        return redirect('tickets:reservation_list')