from django import forms
from django.forms import inlineformset_factory

from flights.models import Flight, SeatClass


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
            'cancellation_penalty_percent': forms.NumberInput(attrs={'class': 'form-control'}),
            'status': forms.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # برای اینکه فرمت datetime-local موقع ویرایش هم درست نمایش داده شود
        self.fields['departure_datetime'].input_formats = ['%Y-%m-%dT%H:%M']
        self.fields['arrival_datetime'].input_formats = ['%Y-%m-%dT%H:%M']


class SeatClassForm(forms.ModelForm):
    class Meta:
        model = SeatClass
        fields = ['class_type', 'price_multiplier', 'capacity', 'available_seats']
        widgets = {
            'class_type': forms.Select(attrs={'class': 'form-control'}),
            'price_multiplier': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control'}),
            'available_seats': forms.NumberInput(attrs={'class': 'form-control'}),
        }


SeatClassFormSet = inlineformset_factory(
    Flight,
    SeatClass,
    form=SeatClassForm,
    extra=3,
    max_num=3,
    can_delete=True,
)