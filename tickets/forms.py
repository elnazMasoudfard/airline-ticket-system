import re

from django import forms
from django.conf import settings

from .models import Passenger
from .services import MAX_SEATS_PER_RESERVATION

# Persian (۰-۹) and Arabic-Indic (٠-٩) digits -> ASCII digits
_DIGITS_MAP = str.maketrans(
    '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩',
    '01234567890123456789',
)


def normalize_digits(value):
    return str(value).translate(_DIGITS_MAP)


def is_valid_iranian_national_id(code):
    """Checksum validation of the 10 digit Iranian national code."""
    if not re.fullmatch(r'[0-9]{10}', code) or len(set(code)) == 1:
        return False
    total = sum(int(code[i]) * (10 - i) for i in range(9))
    remainder = total % 11
    check_digit = int(code[9])
    return check_digit == remainder if remainder < 2 else check_digit == 11 - remainder


class ReservationForm(forms.Form):
    seats_count = forms.IntegerField(
        min_value=1,
        max_value=MAX_SEATS_PER_RESERVATION,
        initial=1,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'min': 1,
            'max': MAX_SEATS_PER_RESERVATION,
        }),
        label="تعداد صندلی",
        help_text=f"حداکثر {MAX_SEATS_PER_RESERVATION} صندلی در هر رزرو",
    )


class PassengerForm(forms.ModelForm):
    class Meta:
        model = Passenger
        fields = ['first_name', 'last_name', 'national_id']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'national_id': forms.TextInput(attrs={
                'class': 'form-control',
                'inputmode': 'numeric',
                'maxlength': 10,
            }),
        }

    def clean_national_id(self):
        # Normalise first so Persian digits can't bypass the duplicate check.
        national_id = normalize_digits(self.cleaned_data['national_id']).strip()

        # Optional strict check: set TICKETS_VALIDATE_NATIONAL_ID_CHECKSUM = True
        # in settings.py once your test data uses real national codes.
        if getattr(settings, 'TICKETS_VALIDATE_NATIONAL_ID_CHECKSUM', False):
            if not is_valid_iranian_national_id(national_id):
                raise forms.ValidationError("کد ملی وارد شده معتبر نیست.")

        return national_id