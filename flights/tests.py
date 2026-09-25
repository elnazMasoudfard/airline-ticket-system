from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from .forms import FlightSearchForm
from .models import Airline, Airport, Flight, Route, Seat, SeatClass
from .services import generate_seats_for_flight, sync_flight_statuses
from .views import FlightDetailView, FlightListView


def create_route():
    origin = Airport.objects.create(
        name="امام خمینی",
        city="تهران",
        iata_code="IKA",
    )
    destination = Airport.objects.create(
        name="شهید هاشمی‌نژاد",
        city="مشهد",
        iata_code="MHD",
    )
    route = Route.objects.create(
        origin=origin,
        destination=destination,
    )
    return route, origin, destination


def create_flight(
    flight_number="IR100",
    departure_datetime=None,
    arrival_datetime=None,
    status=Flight.StatusChoices.SCHEDULED,
    route=None,
    airline=None,
):
    if route is None:
        route, _, _ = create_route()

    if airline is None:
        airline = Airline.objects.create(name=f"ایران ایر {flight_number}")

    if departure_datetime is None:
        departure_datetime = timezone.now() + timedelta(days=1)

    if arrival_datetime is None:
        arrival_datetime = departure_datetime + timedelta(hours=2)

    return Flight.objects.create(
        flight_number=flight_number,
        route=route,
        airline=airline,
        airplane_type=Flight.AirplaneTypeChoices.A320,
        departure_datetime=departure_datetime,
        arrival_datetime=arrival_datetime,
        base_price=Decimal("1000000"),
        status=status,
    )


# ============================================================
# Route constraints
# ============================================================

class RouteConstraintTests(TestCase):

    def test_self_route_is_rejected(self):
        origin = Airport.objects.create(
            name="امام خمینی",
            city="تهران",
            iata_code="IKA",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Route.objects.create(
                    origin=origin,
                    destination=origin,
                )

    def test_duplicate_route_is_rejected(self):
        route, origin, destination = create_route()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Route.objects.create(
                    origin=origin,
                    destination=destination,
                )


# ============================================================
# Flight constraints
# ============================================================

class FlightConstraintTests(TestCase):

    def setUp(self):
        self.route, _, _ = create_route()
        self.airline = Airline.objects.create(name="ایران ایر")

    def test_arrival_before_departure_is_rejected(self):
        now = timezone.now()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Flight.objects.create(
                    flight_number="IR100",
                    route=self.route,
                    airline=self.airline,
                    airplane_type=Flight.AirplaneTypeChoices.A320,
                    departure_datetime=now + timedelta(hours=2),
                    arrival_datetime=now + timedelta(hours=1),
                    base_price=Decimal("1000000"),
                )


# ============================================================
# Flight QuerySet tests
# ============================================================

class FlightQuerySetTests(TestCase):

    def setUp(self):
        self.route, self.origin, self.destination = create_route()
        self.other_airport = Airport.objects.create(
            name="فرودگاه شهید مدنی",
            city="تبریز",
            iata_code="TBZ",
        )

        self.airline = Airline.objects.create(name="ایران ایر")

        self.future_flight = create_flight(
            flight_number="IR101",
            departure_datetime=timezone.now() + timedelta(days=2),
            route=self.route,
            airline=self.airline,
        )

        self.past_flight = create_flight(
            flight_number="IR102",
            departure_datetime=timezone.now() - timedelta(days=1),
            arrival_datetime=timezone.now() - timedelta(hours=22),
            route=self.route,
            airline=self.airline,
        )

        self.active_flight = create_flight(
            flight_number="IR103",
            departure_datetime=timezone.now() + timedelta(hours=1),
            arrival_datetime=timezone.now() + timedelta(hours=3),
            status=Flight.StatusChoices.ACTIVE,
            route=self.route,
            airline=self.airline,
        )

        other_route = Route.objects.create(
            origin=self.destination,
            destination=self.other_airport,
        )

        self.other_route_flight = create_flight(
            flight_number="IR104",
            departure_datetime=timezone.now() + timedelta(days=3),
            route=other_route,
            airline=self.airline,
        )

    def test_upcoming_returns_only_future_scheduled_flights(self):
        flights = Flight.objects.upcoming()

        self.assertIn(self.future_flight, flights)
        self.assertNotIn(self.past_flight, flights)
        self.assertNotIn(self.active_flight, flights)

    def test_by_route_can_filter_by_origin_only(self):
        flights = Flight.objects.by_route(origin=self.origin)

        self.assertIn(self.future_flight, flights)
        self.assertNotIn(self.other_route_flight, flights)

    def test_by_route_can_filter_by_destination_only(self):
        flights = Flight.objects.by_route(destination=self.destination)

        self.assertIn(self.future_flight, flights)
        self.assertNotIn(self.other_route_flight, flights)

    def test_by_route_can_filter_by_both_origin_and_destination(self):
        flights = Flight.objects.by_route(
            origin=self.origin,
            destination=self.destination,
        )

        self.assertIn(self.future_flight, flights)
        self.assertNotIn(self.other_route_flight, flights)

    def test_on_date_filters_by_departure_date(self):
        target_date = (
            self.future_flight.departure_datetime
            .astimezone(timezone.get_current_timezone())
            .date()
        )

        flights = Flight.objects.on_date(target_date)

        self.assertIn(self.future_flight, flights)
        self.assertNotIn(self.other_route_flight, flights)


# ============================================================
# SeatClass reservation tests
# ============================================================

class SeatClassReservationTests(TestCase):
    """
    Tests for atomic seat availability and final price calculation.
    """

    def setUp(self):
        self.route, _, _ = create_route()
        self.airline = Airline.objects.create(name="ایران ایر")

        self.flight = create_flight(
            flight_number="IR200",
            route=self.route,
            airline=self.airline,
        )

        self.seat_class = SeatClass.objects.create(
            flight=self.flight,
            class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=10,
            available_seats=10,
        )

    def test_reserve_seats_decrements_availability(self):
        self.seat_class.reserve_seats(3)

        self.seat_class.refresh_from_db()

        self.assertEqual(
            self.seat_class.available_seats,
            7,
        )

    def test_reserve_seats_fails_when_insufficient_capacity(self):
        with self.assertRaises(ValueError):
            self.seat_class.reserve_seats(11)

        self.seat_class.refresh_from_db()

        self.assertEqual(
            self.seat_class.available_seats,
            10,
        )

    def test_reserve_zero_seats_is_rejected(self):
        with self.assertRaises(ValueError):
            self.seat_class.reserve_seats(0)

    def test_reserve_negative_seats_is_rejected(self):
        with self.assertRaises(ValueError):
            self.seat_class.reserve_seats(-1)

    def test_release_seats_increments_availability(self):
        self.seat_class.reserve_seats(3)
        self.seat_class.release_seats(3)

        self.seat_class.refresh_from_db()

        self.assertEqual(
            self.seat_class.available_seats,
            10,
        )

    def test_release_zero_seats_is_rejected(self):
        with self.assertRaises(ValueError):
            self.seat_class.release_seats(0)

    def test_release_negative_seats_is_rejected(self):
        with self.assertRaises(ValueError):
            self.seat_class.release_seats(-1)

    def test_final_price_applies_multiplier(self):
        self.seat_class.price_multiplier = Decimal("1.50")
        self.seat_class.save()

        self.assertEqual(
            self.seat_class.final_price,
            Decimal("1500000.00"),
        )


# ============================================================
# Seat generation tests
# ============================================================

class SeatGenerationTests(TestCase):
    """
    Tests automatic seat generation, continuous rows,
    duplicate prevention and transaction rollback.
    """

    def setUp(self):
        route, _, _ = create_route()
        airline = Airline.objects.create(name="ایران ایر")

        self.flight = create_flight(
            flight_number="IR300",
            route=route,
            airline=airline,
        )

        self.economy = SeatClass.objects.create(
            flight=self.flight,
            class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=8,
            available_seats=8,
        )

        self.business = SeatClass.objects.create(
            flight=self.flight,
            class_type=SeatClass.ClassTypeChoices.BUSINESS,
            capacity=4,
            available_seats=4,
        )

    def test_seats_created_with_correct_count(self):
        created, skipped = generate_seats_for_flight(self.flight)

        self.assertEqual(created, 12)
        self.assertEqual(skipped, [])

        self.assertEqual(
            Seat.objects.filter(seat_class=self.economy).count(),
            8,
        )

        self.assertEqual(
            Seat.objects.filter(seat_class=self.business).count(),
            4,
        )

    def test_seat_numbers_are_generated_correctly(self):
        generate_seats_for_flight(self.flight)

        economy_seats = list(
            Seat.objects
            .filter(seat_class=self.economy)
            .order_by("row_number", "column_letter")
        )

        self.assertEqual(
            [seat.seat_number for seat in economy_seats],
            [
                "1A",
                "1B",
                "1C",
                "1D",
                "1E",
                "1F",
                "2A",
                "2B",
            ],
        )

    def test_row_numbers_are_continuous_across_classes(self):
        generate_seats_for_flight(self.flight)

        economy_max_row = (
            Seat.objects
            .filter(seat_class=self.economy)
            .order_by("-row_number")
            .first()
            .row_number
        )

        business_min_row = (
            Seat.objects
            .filter(seat_class=self.business)
            .order_by("row_number")
            .first()
            .row_number
        )

        self.assertEqual(
            business_min_row,
            economy_max_row + 1,
        )

    def test_skips_classes_that_already_have_seats(self):
        generate_seats_for_flight(self.flight)

        created_again, skipped = generate_seats_for_flight(self.flight)

        self.assertEqual(created_again, 0)
        self.assertEqual(len(skipped), 2)

        self.assertEqual(
            Seat.objects.filter(seat_class=self.economy).count(),
            8,
        )

        self.assertEqual(
            Seat.objects.filter(seat_class=self.business).count(),
            4,
        )

    def test_generation_is_atomic_when_creation_fails(self):
        """
        If seat creation fails after one class has already been processed,
        the transaction must roll back all seats created by the operation.
        """

        real_bulk_create = Seat.objects.bulk_create
        call_count = 0

        def bulk_create_with_failure(*args, **kwargs):
            nonlocal call_count

            call_count += 1

            if call_count == 1:
                return real_bulk_create(*args, **kwargs)

            raise RuntimeError("Simulated seat creation failure")

        with patch(
            "flights.services.Seat.objects.bulk_create",
            side_effect=bulk_create_with_failure,
        ):
            with self.assertRaises(RuntimeError):
                generate_seats_for_flight(self.flight)

        self.assertEqual(
            Seat.objects.filter(seat_class=self.economy).count(),
            0,
        )

        self.assertEqual(
            Seat.objects.filter(seat_class=self.business).count(),
            0,
        )


# ============================================================
# Flight search form tests
# ============================================================

class FlightSearchFormTests(TestCase):

    def setUp(self):
        self.route, self.origin, self.destination = create_route()

    def test_form_is_valid_without_origin_or_destination(self):
        form = FlightSearchForm({})

        self.assertTrue(form.is_valid())

    def test_form_accepts_origin_without_destination(self):
        form = FlightSearchForm({
            "origin": self.origin.pk,
        })

        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data["origin"],
            self.origin,
        )
        self.assertIsNone(
            form.cleaned_data["destination"],
        )

    def test_form_accepts_destination_without_origin(self):
        form = FlightSearchForm({
            "destination": self.destination.pk,
        })

        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data["destination"],
            self.destination,
        )
        self.assertIsNone(
            form.cleaned_data["origin"],
        )

    def test_same_origin_and_destination_is_rejected(self):
        form = FlightSearchForm({
            "origin": self.origin.pk,
            "destination": self.origin.pk,
        })

        self.assertFalse(form.is_valid())

        self.assertIn(
            "مبدا و مقصد نمی‌توانند یکسان باشند.",
            form.non_field_errors(),
        )

    def test_past_departure_date_is_rejected(self):
        past_date = timezone.localdate() - timedelta(days=1)

        form = FlightSearchForm({
            "departure_date": past_date.isoformat(),
        })

        self.assertFalse(form.is_valid())

        self.assertIn(
            "تاریخ حرکت نمی‌تواند در گذشته باشد.",
            form.errors["departure_date"],
        )

    def test_today_departure_date_is_allowed(self):
        today = timezone.localdate()

        form = FlightSearchForm({
            "departure_date": today.isoformat(),
        })

        self.assertTrue(form.is_valid())

    def test_passengers_must_be_at_least_one(self):
        form = FlightSearchForm({
            "passengers": 0,
        })

        self.assertFalse(form.is_valid())

    def test_passengers_are_optional(self):
        form = FlightSearchForm({})

        self.assertTrue(form.is_valid())


# ============================================================
# Flight status synchronization tests
# ============================================================

class FlightStatusSyncTests(TestCase):

    def setUp(self):
        self.route, _, _ = create_route()
        self.airline = Airline.objects.create(name="ایران ایر")

    def test_scheduled_flight_within_one_hour_becomes_active(self):
        departure = timezone.now() + timedelta(minutes=30)

        flight = create_flight(
            flight_number="IR400",
            departure_datetime=departure,
            arrival_datetime=departure + timedelta(hours=2),
            route=self.route,
            airline=self.airline,
        )

        sync_flight_statuses()

        flight.refresh_from_db()

        self.assertEqual(
            flight.status,
            Flight.StatusChoices.ACTIVE,
        )

    def test_scheduled_flight_more_than_one_hour_away_stays_scheduled(self):
        departure = timezone.now() + timedelta(hours=3)

        flight = create_flight(
            flight_number="IR401",
            departure_datetime=departure,
            arrival_datetime=departure + timedelta(hours=2),
            route=self.route,
            airline=self.airline,
        )

        sync_flight_statuses()

        flight.refresh_from_db()

        self.assertEqual(
            flight.status,
            Flight.StatusChoices.SCHEDULED,
        )

    def test_active_flight_after_arrival_becomes_completed(self):
        arrival = timezone.now() - timedelta(minutes=30)
        departure = arrival - timedelta(hours=2)

        flight = create_flight(
            flight_number="IR402",
            departure_datetime=departure,
            arrival_datetime=arrival,
            status=Flight.StatusChoices.ACTIVE,
            route=self.route,
            airline=self.airline,
        )

        sync_flight_statuses()

        flight.refresh_from_db()

        self.assertEqual(
            flight.status,
            Flight.StatusChoices.COMPLETED,
        )

    def test_scheduled_flight_after_arrival_becomes_completed(self):
        arrival = timezone.now() - timedelta(minutes=30)
        departure = arrival - timedelta(hours=2)

        flight = create_flight(
            flight_number="IR403",
            departure_datetime=departure,
            arrival_datetime=arrival,
            status=Flight.StatusChoices.SCHEDULED,
            route=self.route,
            airline=self.airline,
        )

        sync_flight_statuses()

        flight.refresh_from_db()

        self.assertEqual(
            flight.status,
            Flight.StatusChoices.COMPLETED,
        )


# ============================================================
# Flight list view tests
# ============================================================

class FlightListViewTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()

        self.route, self.origin, self.destination = create_route()
        self.airline = Airline.objects.create(name="ایران ایر")

        self.future_flight = create_flight(
            flight_number="IR500",
            departure_datetime=timezone.now() + timedelta(days=2),
            route=self.route,
            airline=self.airline,
        )

        self.other_airport = Airport.objects.create(
            name="فرودگاه شهید مدنی",
            city="تبریز",
            iata_code="TBZ",
        )

        other_route = Route.objects.create(
            origin=self.destination,
            destination=self.other_airport,
        )

        self.other_flight = create_flight(
            flight_number="IR501",
            departure_datetime=timezone.now() + timedelta(days=3),
            route=other_route,
            airline=self.airline,
        )

    def get_queryset(self, query_params=None):
        request = self.factory.get(
            "/flights/",
            data=query_params or {},
        )

        view = FlightListView()
        view.setup(request)

        return view.get_queryset()

    def test_list_view_returns_upcoming_flights(self):
        queryset = self.get_queryset()

        self.assertIn(
            self.future_flight,
            queryset,
        )

    def test_list_view_filters_by_origin_only(self):
        queryset = self.get_queryset({
            "origin": self.origin.pk,
        })

        self.assertIn(
            self.future_flight,
            queryset,
        )

        self.assertNotIn(
            self.other_flight,
            queryset,
        )

    def test_list_view_filters_by_destination_only(self):
        queryset = self.get_queryset({
            "destination": self.destination.pk,
        })

        self.assertIn(
            self.future_flight,
            queryset,
        )

        self.assertNotIn(
            self.other_flight,
            queryset,
        )

    def test_list_view_filters_by_date(self):
        target_date = (
            self.future_flight.departure_datetime
            .astimezone(timezone.get_current_timezone())
            .date()
        )

        queryset = self.get_queryset({
            "departure_date": target_date.isoformat(),
        })

        self.assertIn(
            self.future_flight,
            queryset,
        )

    def test_ajax_request_uses_partial_template(self):
        request = self.factory.get(
            "/flights/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        view = FlightListView()
        view.setup(request)

        self.assertEqual(
            view.get_template_names(),
            ["flights/_flight_results.html"],
        )

    def test_normal_request_uses_full_template(self):
        request = self.factory.get("/flights/")

        view = FlightListView()
        view.setup(request)

        self.assertEqual(
            view.get_template_names(),
            ["flights/flight_list.html"],
        )


# ============================================================
# Flight detail view tests
# ============================================================

class FlightDetailViewTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()

        self.route, _, _ = create_route()
        self.airline = Airline.objects.create(name="ایران ایر")

        self.flight = create_flight(
            flight_number="IR600",
            route=self.route,
            airline=self.airline,
        )

        self.seat_class = SeatClass.objects.create(
            flight=self.flight,
            class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=10,
            available_seats=10,
        )

    def test_detail_view_queryset_contains_requested_flight(self):
        request = self.factory.get(
            f"/flights/{self.flight.pk}/",
        )

        view = FlightDetailView()
        view.setup(request, pk=self.flight.pk)

        queryset = view.get_queryset()

        self.assertIn(
            self.flight,
            queryset,
        )

    def test_detail_view_prefetches_seat_classes(self):
        request = self.factory.get(
            f"/flights/{self.flight.pk}/",
        )

        view = FlightDetailView()
        view.setup(request, pk=self.flight.pk)

        queryset = view.get_queryset()

        flight = queryset.get(pk=self.flight.pk)

        self.assertEqual(
            flight.seat_classes.count(),
            1,
        )