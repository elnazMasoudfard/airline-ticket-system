from django.contrib import admin

from .models import Passenger, Reservation, ReservationSeat


class ReadOnlyAdminMixin:
    """
    Reservations, their seats and their passengers are only changed through the
    booking services (book / pay / cancel / expire). Editing or deleting them by
    hand would desync seats, `available_seats`, the wallet and the revenue
    figures, so the admin is view-only (like the wallet ledger in `accounts`).
    """

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PassengerInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = Passenger
    extra = 0


class ReservationSeatInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ReservationSeat
    extra = 0


@admin.register(Reservation)
class ReservationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        'booking_reference', 'user', 'seat_class', 'seats_count',
        'status', 'total_paid_price', 'paid_at', 'refund_amount',
    ]
    list_filter = ['status', 'cancellation_reason']
    search_fields = ['booking_reference', 'user__username']
    list_select_related = ['user', 'seat_class__flight']
    inlines = [ReservationSeatInline, PassengerInline]


@admin.register(Passenger)
class PassengerAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['first_name', 'last_name', 'national_id', 'reservation']
    search_fields = ['first_name', 'last_name', 'national_id']
    list_select_related = ['reservation']