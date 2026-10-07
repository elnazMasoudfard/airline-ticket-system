from django import forms
from django.forms import inlineformset_factory

from flights.models import Flight, SeatClass
from tickets.models import Reservation


class FlightForm(forms.ModelForm):
    class Meta:
        model = Flight
        fields = [
            'flight_number', 'route', 'airline', 'airplane_type',
            'departure_datetime', 'arrival_datetime', 'base_price',
            'cancellation_penalty_percent', 'status',
        ]
        widgets = {
            'flight_number': forms.TextInput(attrs={'class': 'form-control'}),
            'route': forms.Select(attrs={'class': 'form-control'}),
            'airline': forms.Select(attrs={'class': 'form-control'}),
            'airplane_type': forms.Select(attrs={'class': 'form-control'}),
            'departure_datetime': forms.DateTimeInput(
                attrs={'class': 'form-control', 'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'
            ),
            'arrival_datetime': forms.DateTimeInput(
                attrs={'class': 'form-control', 'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'
            ),
            'base_price': forms.NumberInput(attrs={'class': 'form-control'}),
            'cancellation_penalty_percent': forms.NumberInput(
                attrs={'class': 'form-control', 'min': 0, 'max': 100}
            ),
            'status': forms.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # To ensure the datetime-local format displays correctly during editing as well
        self.fields['departure_datetime'].input_formats = ['%Y-%m-%dT%H:%M']
        self.fields['arrival_datetime'].input_formats = ['%Y-%m-%dT%H:%M']

    def clean_cancellation_penalty_percent(self):
        # A value above 100 would produce a negative refund.
        value = self.cleaned_data['cancellation_penalty_percent']
        if value is not None and not 0 <= value <= 100:
            raise forms.ValidationError("درصد جریمه باید بین ۰ تا ۱۰۰ باشد.")
        return value

    def clean(self):
        cleaned = super().clean()
        departure = cleaned.get('departure_datetime')
        arrival = cleaned.get('arrival_datetime')
        if departure and arrival and arrival <= departure:
            self.add_error('arrival_datetime', "زمان فرود باید بعد از زمان حرکت باشد.")

        # A cancelled flight has already been refunded; re-opening it would
        # leave users with refunded (cancelled) reservations on a "live" flight.
        if (
            self.instance.pk
            and self.instance.status == Flight.StatusChoices.CANCELLED
            and cleaned.get('status') != Flight.StatusChoices.CANCELLED
            and Reservation.objects.filter(seat_class__flight=self.instance).exists()
        ):
            self.add_error(
                'status',
                "این پرواز لغو شده و رزروهایش مسترد شده‌اند؛ دوباره فعال‌کردن آن ممکن نیست. "
                "یک پرواز جدید ثبت کنید.",
            )
        return cleaned


class SeatClassForm(forms.ModelForm):
    """
    `available_seats` is NOT editable by hand: it is derived from
    `capacity - (seats already booked)`. Editing it manually would desync the
    counter from the real Seat rows / reservations and allow overbooking.
    """

    class Meta:
        model = SeatClass
        fields = ['class_type', 'price_multiplier', 'capacity', 'available_seats']
        widgets = {
            'class_type': forms.Select(attrs={'class': 'form-control'}),
            'price_multiplier': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'available_seats': forms.NumberInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields['available_seats']
        field.disabled = True
        field.required = False
        field.help_text = "به‌صورت خودکار از روی ظرفیت و رزروها محاسبه می‌شود."

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

        cleaned['available_seats'] = capacity - booked
        return cleaned


SeatClassFormSet = inlineformset_factory(
    Flight,
    SeatClass,
    form=SeatClassForm,
    extra=3,
    max_num=3,
    validate_max=True,
    can_delete=True,
)