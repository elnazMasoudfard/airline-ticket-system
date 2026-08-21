from django.contrib import admin
from .models import Reservation, Passenger

class PassengerInline(admin.TabularInline):
    model = Passenger
    extra = 1

@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ('booking_reference', 'user', 'seat_class', 'seats_count', 'total_paid_price', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('booking_reference', 'user__username')
    readonly_fields = ('booking_reference', 'created_at', 'cancelled_at')  # اضافه شد
    inlines = [PassengerInline]