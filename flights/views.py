from django.utils import timezone
from django.views.generic import DetailView, ListView

from .forms import FlightSearchForm
from .models import Flight


class FlightListView(ListView):
    """
    لیست پروازهای آینده (برنامه‌ریزی‌شده) با امکان جستجو بر اساس
    مبدا، مقصد و تاریخ حرکت.
    """
    model = Flight
    template_name = 'flights/flight_list.html'
    context_object_name = 'flights'
    paginate_by = 10

    def get_queryset(self):
        queryset = (
            Flight.objects
            .filter(
                status=Flight.StatusChoices.SCHEDULED,
                departure_datetime__gt=timezone.now(),
            )
            .select_related('route__origin', 'route__destination', 'airline')
            .prefetch_related('seat_classes')
        )

        form = FlightSearchForm(self.request.GET or None)
        if form.is_valid():
            origin = form.cleaned_data.get('origin')
            destination = form.cleaned_data.get('destination')
            departure_date = form.cleaned_data.get('departure_date')

            if origin:
                queryset = queryset.filter(route__origin=origin)
            if destination:
                queryset = queryset.filter(route__destination=destination)
            if departure_date:
                queryset = queryset.filter(departure_datetime__date=departure_date)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_form'] = FlightSearchForm(self.request.GET or None)
        return context


class FlightDetailView(DetailView):
    """جزئیات یک پرواز به همراه کلاس‌های صندلی و قیمت نهایی هر کدام."""
    model = Flight
    template_name = 'flights/flight_detail.html'
    context_object_name = 'flight'

    def get_queryset(self):
        return (
            Flight.objects
            .select_related('route__origin', 'route__destination', 'airline')
            .prefetch_related('seat_classes')
        )