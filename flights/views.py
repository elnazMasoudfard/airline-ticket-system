from django.views.generic import DetailView, ListView

from .forms import FlightSearchForm
from .models import Flight
from .services import sync_flight_statuses


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
        sync_flight_statuses()

        queryset = Flight.objects.upcoming().with_route_info().prefetch_related('seat_classes')

        form = FlightSearchForm(self.request.GET or None)
        if form.is_valid():
            origin = form.cleaned_data.get('origin')
            destination = form.cleaned_data.get('destination')
            departure_date = form.cleaned_data.get('departure_date')

            queryset = queryset.by_route(origin, destination)
            if departure_date:
                queryset = queryset.on_date(departure_date)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_form'] = FlightSearchForm(self.request.GET or None)
        return context

    def get_template_names(self):
        # درخواست‌های Ajax فقط بخش نتایج را می‌خواهند، نه کل صفحه
        if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return ['flights/_flight_results.html']
        return [self.template_name]


class FlightDetailView(DetailView):
    """جزئیات یک پرواز به همراه کلاس‌های صندلی و قیمت نهایی هر کدام."""
    model = Flight
    template_name = 'flights/flight_detail.html'
    context_object_name = 'flight'

    def get_queryset(self):
        sync_flight_statuses()
        return Flight.objects.with_route_info().prefetch_related('seat_classes')