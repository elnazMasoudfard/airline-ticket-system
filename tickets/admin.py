from django.contrib import admin

from .models import Passenger, Reservation, ReservationSeat


class PassengerInline(admin.TabularInline):
    model = Passenger
    extra = 0


class ReservationSeatInline(admin.TabularInline):
    model = ReservationSeat
    extra = 0
    readonly_fields = ['seat']


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ['booking_reference', 'user', 'seat_class', 'seats_count', 'status', 'total_paid_price']
    list_filter = ['status']
    search_fields = ['booking_reference', 'user__username']
    readonly_fields = ['booking_reference', 'status', 'cancelled_at', 'refund_amount']
    inlines = [ReservationSeatInline, PassengerInline]


@admin.register(Passenger)
class PassengerAdmin(admin.ModelAdmin):
    list_display = ['first_name', 'last_name', 'national_id', 'reservation']
    search_fields = ['first_name', 'last_name', 'national_id']