from django import forms
from django.utils import timezone

from .models import Airport


class FlightSearchForm(forms.Form):
    origin = forms.ModelChoiceField(
        queryset=Airport.objects.all(),
        widget=forms.Select(attrs={'class': 'form-control'}),
        label="مبدا"
    )
    destination = forms.ModelChoiceField(
        queryset=Airport.objects.all(),
        widget=forms.Select(attrs={'class': 'form-control'}),
        label="مقصد"
    )
    departure_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        label="تاریخ حرکت"
    )
    passengers = forms.IntegerField(
        required=False,
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={'class': 'form-control'}),
        label="تعداد مسافر"
    )

    def clean_departure_date(self):
        departure_date = self.cleaned_data.get('departure_date')
        if departure_date and departure_date < timezone.now().date():
            raise forms.ValidationError("تاریخ حرکت نمی‌تواند در گذشته باشد.")
        return departure_date

    def clean(self):
        cleaned_data = super().clean()
        origin = cleaned_data.get('origin')
        destination = cleaned_data.get('destination')
        if origin and destination and origin == destination:
            raise forms.ValidationError("مبدا و مقصد نمی‌توانند یکسان باشند.")
        return cleaned_data