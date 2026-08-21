from django.contrib import admin
from .models import Airport, Airline, Route, Flight, SeatClass

class SeatClassInline(admin.TabularInline):
    model = SeatClass
    extra = 1

@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ('flight_number', 'airline', 'route', 'departure_datetime', 'base_price', 'status')
    list_filter = ('status', 'airline')
    search_fields = ('flight_number', 'route__origin__city', 'route__destination__city')
    inlines = [SeatClassInline]

admin.site.register(Airport)
admin.site.register(Airline)
admin.site.register(Route)