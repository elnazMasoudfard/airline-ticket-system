import logging

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q

from .models import Airline, Airport, Flight, Route, Seat, SeatClass
from .services import (
    generate_seats_for_flight,
    resync_flight_seats,
    sync_seats_with_capacity,
)

logger = logging.getLogger('flights')


@admin.register(Airport)
class AirportAdmin(admin.ModelAdmin):
    list_display = ['city', 'name', 'iata_code']
    search_fields = ['city', 'name', 'iata_code']


@admin.register(Airline)
class AirlineAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ['origin', 'destination']
    list_filter = ['origin', 'destination']


# ----------------------------------------------------------------------
# Flights and seat classes
# ----------------------------------------------------------------------
class SeatClassAdminForm(forms.ModelForm):
    """
    `available_seats` can NOT be typed by hand (it is read-only in the admin): it is
    always derived from `capacity - (seats already booked)`, exactly like in the
    dashboard. Editing it manually would desync it from the real Seat rows and the
    reservations and allow overbooking.
    """

    class Meta:
        model = SeatClass
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        capacity = cleaned.get('capacity')
        if capacity is None:
            return cleaned

        booked = 0
        if self.instance.pk:
            # The instance still holds the OLD values here.
            booked = self.instance.capacity - self.instance.available_seats

        if capacity < booked:
            raise forms.ValidationError(
                f"ظرفیت نمی‌تواند کمتر از تعداد صندلی‌های رزروشده ({booked}) باشد."
            )

        self.instance.available_seats = capacity - booked
        return cleaned


class SeatClassInline(admin.TabularInline):
    model = SeatClass
    form = SeatClassAdminForm
    extra = 1
    readonly_fields = ['available_seats']


class FlightAdminForm(forms.ModelForm):
    class Meta:
        model = Flight
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        # A cancelled flight has already been refunded; re-opening it would leave users
        # with refunded (cancelled) reservations on a "live" flight.
        if (
            self.instance.pk
            and self.instance.status == Flight.StatusChoices.CANCELLED
            and cleaned.get('status') != Flight.StatusChoices.CANCELLED
        ):
            from tickets.models import Reservation

            if Reservation.objects.filter(seat_class__flight=self.instance).exists():
                self.add_error(
                    'status',
                    "این پرواز لغو شده و رزروهایش مسترد شده‌اند؛ دوباره فعال‌کردن آن ممکن نیست. "
                    "یک پرواز جدید ثبت کنید.",
                )
        return cleaned


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


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    form = FlightAdminForm
    list_display = ['flight_number', 'route', 'airline', 'departure_datetime', 'status']
    list_filter = ['status', 'airline']
    search_fields = ['flight_number']
    list_select_related = ['route__origin', 'route__destination', 'airline']
    inlines = [SeatClassInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        flight = form.instance

        # If the capacity of a class that already has seats changed, bring the seats along.
        added, removed = resync_flight_seats(flight)
        if added or removed:
            messages.info(
                request,
                f"صندلی‌ها با ظرفیت جدید هماهنگ شد: {added} صندلی اضافه و {removed} صندلی حذف شد.",
            )

        # Setting the status to "cancelled" must refund every reservation, not just flip a flag.
        if (
            change
            and 'status' in form.changed_data
            and flight.status == Flight.StatusChoices.CANCELLED
        ):
            from tickets.services import ReservationError, cancel_flight_and_refund

            try:
                result = cancel_flight_and_refund(flight.pk)
            except ReservationError as exc:
                messages.error(request, str(exc))
            except Exception:
                logger.exception(
                    f"خطا در استرداد رزروها پس از لغو پرواز از پنل ادمین: flight={flight.flight_number}"
                )
                messages.error(
                    request,
                    "وضعیت پرواز «لغو» ثبت شد ولی استرداد رزروها کامل انجام نشد. "
                    "از صفحه‌ی جزئیات پرواز در داشبورد دوباره «لغو پرواز» را بزنید."
                )
            else:
                logger.info(
                    f"لغو پرواز از پنل ادمین توسط={request.user.username}: {flight.flight_number} "
                    f"استرداد={result.refunded_count} ناموفق={result.failed}"
                )
                flash_flight_cancel_result(request, flight, result)


@admin.action(description="ساخت خودکار صندلی‌ها بر اساس ظرفیت (ردیف‌های پیوسته برای کل پرواز)")
def generate_seats(modeladmin, request, queryset):
    flight_ids = set(queryset.values_list('flight_id', flat=True))
    created_total = 0
    skipped_total = []

    for flight_id in flight_ids:
        flight = Flight.objects.get(pk=flight_id)
        created, skipped = generate_seats_for_flight(flight)
        created_total += created
        skipped_total += skipped

    if created_total:
        logger.info(
            f"ساخت خودکار صندلی از پنل ادمین توسط={request.user.username}: "
            f"مجموع ساخته‌شده={created_total}"
        )
        messages.success(request, f"{created_total} صندلی ساخته شد.")
    if skipped_total:
        messages.warning(
            request,
            "این کلاس‌های صندلی از قبل صندلی داشتند و رد شدند: " + "، ".join(skipped_total)
        )


@admin.action(description="⚠️ حذف کلاس صندلی (فقط اگر هیچ رزرو فعال یا پرداخت‌شده‌ای نداشته باشد)")
def force_delete_seat_class(modeladmin, request, queryset):
    """
    Delete a seat class together with its reservations, but ONLY when none of them
    holds seats or ever received money. Paid reservations (even cancelled and refunded
    ones) are financial records: deleting them would silently change the revenue and
    refund totals, so such a class is refused.
    """
    from tickets.models import Reservation
    from tickets.services import expire_pending_reservations

    # Overdue unpaid reservations do not hold seats any more; release them first.
    expire_pending_reservations()

    deleted_count = 0
    blocked = []

    for seat_class in queryset:
        with transaction.atomic():
            try:
                locked = SeatClass.objects.select_for_update().get(pk=seat_class.pk)
            except SeatClass.DoesNotExist:
                continue

            reservations = Reservation.objects.filter(seat_class=locked)
            blocking_count = reservations.filter(
                Q(status__in=[
                    Reservation.StatusChoices.PENDING_PAYMENT,
                    Reservation.StatusChoices.RESERVED,
                ])
                | Q(paid_at__isnull=False)
            ).count()

            if blocking_count > 0:
                blocked.append(f"{locked} ({blocking_count} رزرو فعال یا پرداخت‌شده)")
                continue

            label = str(locked)
            # Only never-paid cancelled/expired reservations are left; remove them so PROTECT
            # no longer blocks the deletion.
            reservations.delete()
            locked.delete()

        logger.warning(
            f"حذف کلاس صندلی توسط={request.user.username}: {label}"
        )
        deleted_count += 1

    if deleted_count:
        messages.success(request, f"{deleted_count} کلاس صندلی (همراه با تاریخچه‌ی بدون پرداخت‌اش) حذف شد.")
    if blocked:
        messages.error(
            request,
            "این کلاس‌ها رزرو فعال یا پرداخت‌شده دارند و حذف نشدند: " + "، ".join(blocked)
        )


@admin.register(SeatClass)
class SeatClassAdmin(admin.ModelAdmin):
    form = SeatClassAdminForm
    list_display = ['flight', 'class_type', 'capacity', 'available_seats']
    list_filter = ['class_type']
    list_select_related = ['flight']
    readonly_fields = ['available_seats']
    actions = [generate_seats, force_delete_seat_class]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        added, removed = sync_seats_with_capacity(obj)
        if added or removed:
            messages.info(
                request,
                f"صندلی‌ها با ظرفیت جدید هماهنگ شد: {added} صندلی اضافه و {removed} صندلی حذف شد.",
            )


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    """
    Seats are created by the generator and change only through bookings, so this page
    is view-only: ticking `is_available` by hand would desync it from the reservations.
    """
    list_display = ['seat_class', 'seat_number', 'is_available', 'booked_by']
    list_filter = ['is_available', 'seat_class__flight']
    search_fields = ['seat_class__flight__flight_number']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'seat_class__flight', 'reservation_seat__reservation__user'
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # A seat can not be deleted on its own (obj is given); the check WITHOUT an object is
        # what Django uses when deleting a flight or a seat class cascades to its seats.
        return obj is None

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def booked_by(self, obj):
        try:
            reservation = obj.reservation_seat.reservation
        except ObjectDoesNotExist:
            return "—"
        return f"{reservation.user.username} ({reservation.booking_reference})"

    booked_by.short_description = "رزروکننده"