from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist

from .models import Airline, Airport, Flight, Route, Seat, SeatClass
from .services import generate_seats_for_flight


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


class SeatClassInline(admin.TabularInline):
    model = SeatClass
    extra = 1


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ['flight_number', 'route', 'airline', 'departure_datetime', 'status']
    list_filter = ['status', 'airline']
    search_fields = ['flight_number']
    inlines = [SeatClassInline]


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
        messages.success(request, f"{created_total} صندلی ساخته شد.")
    if skipped_total:
        messages.warning(
            request,
            "این کلاس‌های صندلی از قبل صندلی داشتند و رد شدند: " + "، ".join(skipped_total)
        )


@admin.register(SeatClass)
class SeatClassAdmin(admin.ModelAdmin):
    list_display = ['flight', 'class_type', 'capacity', 'available_seats']
    list_filter = ['class_type']
    actions = [generate_seats]


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ['seat_class', 'seat_number', 'is_available', 'booked_by']
    list_filter = ['is_available', 'seat_class__flight']
    search_fields = ['seat_class__flight__flight_number']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'seat_class__flight', 'reservation_seat__reservation__user'
        )

    def booked_by(self, obj):
        try:
            reservation = obj.reservation_seat.reservation
        except ObjectDoesNotExist:
            return "—"
        return f"{reservation.user.username} ({reservation.booking_reference})"

    booked_by.short_description = "رزروکننده"