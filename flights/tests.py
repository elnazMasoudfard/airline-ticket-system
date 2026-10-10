from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.models import CustomUser
from tickets.models import Reservation
from tickets.services import create_pending_reservation

from .admin import (
    FlightAdmin,
    FlightAdminForm,
    SeatAdmin,
    SeatClassAdminForm,
    force_delete_seat_class,
    generate_seats,
)
from .forms import FlightSearchForm
from .middleware import FlightStatusSyncMiddleware
from .models import Airline, Airport, Flight, Route, Seat, SeatClass
from .services import (
    generate_seats_for_flight,
    resync_flight_seats,
    sync_flight_statuses,
    sync_seats_with_capacity,
)
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

# ============================================================
# Keeping Seat rows in step with the capacity of a class
# ============================================================

def make_seat_class(flight, class_type=SeatClass.ClassTypeChoices.ECONOMY, capacity=6):
    return SeatClass.objects.create(
        flight=flight, class_type=class_type, capacity=capacity, available_seats=capacity,
    )


def set_capacity(seat_class, capacity):
    """Change the capacity the way the dashboard/admin forms do (available follows)."""
    SeatClass.objects.filter(pk=seat_class.pk).update(capacity=capacity, available_seats=capacity)
    seat_class.refresh_from_db()


def seat_positions(seat_class):
    return list(
        Seat.objects.filter(seat_class=seat_class)
        .order_by('row_number', 'column_letter')
        .values_list('row_number', 'column_letter')
    )


class SeatCapacitySyncTests(TestCase):

    def setUp(self):
        self.flight = create_flight()

    def test_class_without_seats_is_left_alone(self):
        seat_class = make_seat_class(self.flight, capacity=6)
        set_capacity(seat_class, 10)

        self.assertEqual(sync_seats_with_capacity(seat_class), (0, 0))
        self.assertEqual(Seat.objects.filter(seat_class=seat_class).count(), 0)

    def test_nothing_changes_when_capacity_matches(self):
        seat_class = make_seat_class(self.flight, capacity=8)
        generate_seats_for_flight(self.flight)

        self.assertEqual(sync_seats_with_capacity(seat_class), (0, 0))
        self.assertEqual(Seat.objects.filter(seat_class=seat_class).count(), 8)

    def test_capacity_increase_continues_the_last_row(self):
        seat_class = make_seat_class(self.flight, capacity=8)  # row 1: A-F, row 2: A-B
        generate_seats_for_flight(self.flight)
        set_capacity(seat_class, 10)

        self.assertEqual(sync_seats_with_capacity(seat_class), (2, 0))

        positions = seat_positions(seat_class)
        self.assertEqual(len(positions), 10)
        self.assertIn((2, 'C'), positions)
        self.assertIn((2, 'D'), positions)

    def test_capacity_increase_goes_to_new_rows_when_another_class_follows(self):
        economy = make_seat_class(self.flight, SeatClass.ClassTypeChoices.ECONOMY, capacity=6)
        business = make_seat_class(self.flight, SeatClass.ClassTypeChoices.BUSINESS, capacity=4)
        generate_seats_for_flight(self.flight)  # economy: row 1, business: row 2
        set_capacity(economy, 8)

        self.assertEqual(sync_seats_with_capacity(economy), (2, 0))

        economy_rows = {row for row, _ in seat_positions(economy)}
        business_rows = {row for row, _ in seat_positions(business)}
        self.assertEqual(len(seat_positions(economy)), 8)
        self.assertEqual(business_rows, {2})
        self.assertEqual(economy_rows, {1, 3})  # new seats start after the last row of the flight
        self.assertFalse(economy_rows & business_rows)

    def test_capacity_decrease_removes_free_seats_from_the_back(self):
        seat_class = make_seat_class(self.flight, capacity=8)
        generate_seats_for_flight(self.flight)
        set_capacity(seat_class, 5)

        self.assertEqual(sync_seats_with_capacity(seat_class), (0, 3))

        self.assertEqual(
            seat_positions(seat_class),
            [(1, 'A'), (1, 'B'), (1, 'C'), (1, 'D'), (1, 'E')],
        )

    def test_booked_seats_are_never_removed(self):
        seat_class = make_seat_class(self.flight, capacity=8)
        generate_seats_for_flight(self.flight)
        seats = list(Seat.objects.filter(seat_class=seat_class).order_by('row_number', 'column_letter'))
        booked = seats[6]  # (2, 'A'): near the back, but booked
        Seat.objects.filter(pk=booked.pk).update(is_available=False)
        set_capacity(seat_class, 6)

        self.assertEqual(sync_seats_with_capacity(seat_class), (0, 2))

        self.assertTrue(Seat.objects.filter(pk=booked.pk).exists())
        self.assertEqual(Seat.objects.filter(seat_class=seat_class).count(), 6)

    def test_decrease_fails_when_there_are_not_enough_free_seats(self):
        seat_class = make_seat_class(self.flight, capacity=8)
        generate_seats_for_flight(self.flight)
        Seat.objects.filter(
            pk__in=list(Seat.objects.filter(seat_class=seat_class).values_list('pk', flat=True)[:5])
        ).update(is_available=False)
        set_capacity(seat_class, 2)  # would need to remove 6 seats, only 3 are free

        with self.assertRaises(ValueError):
            sync_seats_with_capacity(seat_class)
        self.assertEqual(Seat.objects.filter(seat_class=seat_class).count(), 8)

    def test_resync_flight_seats_handles_every_class(self):
        economy = make_seat_class(self.flight, SeatClass.ClassTypeChoices.ECONOMY, capacity=6)
        business = make_seat_class(self.flight, SeatClass.ClassTypeChoices.BUSINESS, capacity=4)
        generate_seats_for_flight(self.flight)
        set_capacity(economy, 7)
        set_capacity(business, 2)

        self.assertEqual(resync_flight_seats(self.flight), (1, 2))
        self.assertEqual(Seat.objects.filter(seat_class=economy).count(), 7)
        self.assertEqual(Seat.objects.filter(seat_class=business).count(), 2)


# ============================================================
# Pricing helpers and totals
# ============================================================

class SeatClassPricingTests(TestCase):

    def test_final_price_is_rounded_half_up(self):
        flight = create_flight()
        Flight.objects.filter(pk=flight.pk).update(base_price=Decimal('100.05'))
        seat_class = SeatClass.objects.create(
            flight=Flight.objects.get(pk=flight.pk),
            class_type=SeatClass.ClassTypeChoices.BUSINESS,
            price_multiplier=Decimal('1.50'),
            capacity=4, available_seats=4,
        )
        self.assertEqual(seat_class.final_price, Decimal('150.08'))  # 150.075 -> 150.08

    def test_total_available_seats_adds_up_all_classes(self):
        flight = create_flight()
        make_seat_class(flight, SeatClass.ClassTypeChoices.ECONOMY, capacity=6)
        make_seat_class(flight, SeatClass.ClassTypeChoices.BUSINESS, capacity=4)
        self.assertEqual(Flight.objects.get(pk=flight.pk).total_available_seats, 10)


# ============================================================
# Search by number of passengers
# ============================================================

class PassengerSearchTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        route, _, _ = create_route()
        airline = Airline.objects.create(name="ایران ایر")

        self.roomy = create_flight("IR601", route=route, airline=airline)
        make_seat_class(self.roomy, capacity=6)

        self.tight = create_flight("IR602", route=route, airline=airline)
        tight_class = make_seat_class(self.tight, capacity=6)
        SeatClass.objects.filter(pk=tight_class.pk).update(available_seats=1)

        self.both = create_flight("IR603", route=route, airline=airline)
        make_seat_class(self.both, SeatClass.ClassTypeChoices.ECONOMY, capacity=5)
        make_seat_class(self.both, SeatClass.ClassTypeChoices.BUSINESS, capacity=4)

    def get_queryset(self, params=None):
        request = self.factory.get("/flights/", data=params or {})
        view = FlightListView()
        view.setup(request)
        return view.get_queryset()

    def test_only_flights_that_can_seat_the_whole_group_are_listed(self):
        flights = list(self.get_queryset({"passengers": 2}))
        self.assertIn(self.roomy, flights)
        self.assertIn(self.both, flights)
        self.assertNotIn(self.tight, flights)

    def test_a_flight_with_two_matching_classes_is_listed_once(self):
        flights = list(self.get_queryset({"passengers": 2}))
        self.assertEqual(flights.count(self.both), 1)

    def test_one_passenger_matches_every_flight_with_a_free_seat(self):
        flights = list(self.get_queryset({"passengers": 1}))
        self.assertEqual({f.pk for f in flights}, {self.roomy.pk, self.tight.pk, self.both.pk})

    def test_seats_of_different_classes_are_not_added_up(self):
        # 5 economy + 4 business free seats, but 7 people cannot sit in one class
        flights = list(self.get_queryset({"passengers": 7}))
        self.assertNotIn(self.both, flights)

    def test_without_passengers_nothing_is_filtered(self):
        flights = list(self.get_queryset())
        self.assertEqual({f.pk for f in flights}, {self.roomy.pk, self.tight.pk, self.both.pk})


# ============================================================
# Middleware
# ============================================================

class FlightStatusSyncMiddlewareTests(TestCase):

    def setUp(self):
        self.request = RequestFactory().get("/")
        self.middleware = FlightStatusSyncMiddleware(lambda request: HttpResponse("ok"))

    def test_first_request_syncs_statuses_and_passes_the_response_through(self):
        flight = create_flight(
            departure_datetime=timezone.now() + timedelta(minutes=30),
            arrival_datetime=timezone.now() + timedelta(hours=3),
        )

        response = self.middleware(self.request)

        self.assertEqual(response.status_code, 200)
        flight.refresh_from_db()
        self.assertEqual(flight.status, Flight.StatusChoices.ACTIVE)

    def test_sync_runs_at_most_once_per_interval(self):
        with patch("flights.middleware.sync_flight_statuses") as mocked_sync, \
                patch("flights.middleware.time") as fake_time:
            fake_time.monotonic.side_effect = [1000.0, 1030.0, 1061.0]
            self.middleware(self.request)  # t=1000: syncs
            self.middleware(self.request)  # t=1030: too early
            self.middleware(self.request)  # t=1061: a minute has passed
        self.assertEqual(mocked_sync.call_count, 2)

    def test_a_failing_sync_never_breaks_the_page(self):
        with patch("flights.middleware.sync_flight_statuses", side_effect=RuntimeError("boom")):
            with self.assertLogs("flights", level="ERROR"):
                response = self.middleware(self.request)
        self.assertEqual(response.status_code, 200)


# ============================================================
# Management commands
# ============================================================

class SyncFlightStatusesCommandTests(TestCase):

    def test_command_updates_statuses_and_reports_the_counts(self):
        flight = create_flight(
            departure_datetime=timezone.now() + timedelta(minutes=30),
            arrival_datetime=timezone.now() + timedelta(hours=3),
        )
        out = StringIO()

        call_command("sync_flight_statuses", stdout=out)

        flight.refresh_from_db()
        self.assertEqual(flight.status, Flight.StatusChoices.ACTIVE)
        self.assertIn("1 پرواز فعال شد", out.getvalue())


class RecalculateSeatAvailabilityCommandTests(TestCase):
    """
    The command must treat a live PENDING reservation exactly like a paid one: it holds
    seats. (It used to count only paid reservations and would have released held seats.)
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="traveler", password="pass12345")
        self.flight = create_flight()
        self.seat_class = make_seat_class(self.flight, capacity=6)
        generate_seats_for_flight(self.flight)
        self.seats = list(
            Seat.objects.filter(seat_class=self.seat_class).order_by("row_number", "column_letter")
        )

        self.pending = self.hold(self.seats[0])                     # live, unpaid
        self.paid = self.hold(self.seats[1])
        Reservation.objects.filter(pk=self.paid.pk).update(
            status=Reservation.StatusChoices.RESERVED, paid_at=timezone.now()
        )
        self.overdue = self.hold(self.seats[2])
        Reservation.objects.filter(pk=self.overdue.pk).update(
            payment_expires_at=timezone.now() - timedelta(minutes=1)
        )

    def hold(self, seat):
        create_pending_reservation(
            user=self.user, seat_class_id=self.seat_class.pk, seat_ids=[seat.pk], seats_count=1,
        )
        return Reservation.objects.order_by("-pk").first()

    def corrupt(self):
        """
        Pretend the data was edited by hand: every seat looks free and the counter is too
        high. (5, not 6: releasing the overdue reservation adds one back, and the counter
        may never exceed the capacity.)
        """
        SeatClass.objects.filter(pk=self.seat_class.pk).update(available_seats=5)
        Seat.objects.filter(seat_class=self.seat_class).update(is_available=True)

    def free_seat_ids(self):
        return set(
            Seat.objects.filter(seat_class=self.seat_class, is_available=True).values_list("pk", flat=True)
        )

    def test_pending_and_paid_reservations_keep_their_seats(self):
        self.corrupt()
        out = StringIO()

        call_command("recalculate_seat_availability", stdout=out)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 4)  # 6 - pending - paid
        free = self.free_seat_ids()
        self.assertNotIn(self.seats[0].pk, free)  # live pending still holds its seat
        self.assertNotIn(self.seats[1].pk, free)  # paid still holds its seat
        self.assertIn(self.seats[2].pk, free)     # overdue reservation released its seat

    def test_overdue_reservations_are_released_first(self):
        self.corrupt()
        call_command("recalculate_seat_availability", stdout=StringIO())

        self.overdue.refresh_from_db()
        self.assertEqual(self.overdue.status, Reservation.StatusChoices.CANCELLED)
        self.pending.refresh_from_db()
        self.paid.refresh_from_db()
        self.assertEqual(self.pending.status, Reservation.StatusChoices.PENDING_PAYMENT)
        self.assertEqual(self.paid.status, Reservation.StatusChoices.RESERVED)

    def test_dry_run_reports_differences_but_changes_nothing(self):
        self.corrupt()
        out = StringIO()

        call_command("recalculate_seat_availability", "--dry-run", stdout=out)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 5)
        self.assertEqual(len(self.free_seat_ids()), 6)
        self.overdue.refresh_from_db()
        self.assertEqual(self.overdue.status, Reservation.StatusChoices.PENDING_PAYMENT)
        self.assertIn("5 -> 4", out.getvalue())
        self.assertIn("حالت آزمایشی", out.getvalue())

    def test_consistent_data_is_left_untouched(self):
        out = StringIO()
        call_command("recalculate_seat_availability", stdout=out)
        # after the sweep the data is consistent; a second run changes nothing
        out = StringIO()
        call_command("recalculate_seat_availability", stdout=out)
        self.assertIn("0 کلاس صندلی و 0 صندلی اصلاح شد", out.getvalue())


# ============================================================
# Admin
# ============================================================

def admin_request(user):
    """A request that supports messages, for calling admin actions directly."""
    request = RequestFactory().post("/admin/")
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def flashed(request):
    return [str(message) for message in get_messages(request)]


class AdminActionTests(TestCase):

    def setUp(self):
        self.staff = CustomUser.objects.create_user(
            username="staff", password="pass12345", is_staff=True, is_superuser=True
        )
        self.buyer = CustomUser.objects.create_user(username="buyer", password="pass12345")
        self.flight = create_flight()
        self.seat_class = make_seat_class(self.flight, capacity=6)
        self.request = admin_request(self.staff)
        self.seat_class_admin = admin.site._registry[SeatClass]

    def reservation(self, **fields):
        defaults = dict(
            user=self.buyer, seat_class=self.seat_class, seats_count=1,
            total_paid_price=Decimal("1000000.00"),
        )
        defaults.update(fields)
        return Reservation.objects.create(**defaults)

    # ----- generate seats ------------------------------------------------
    def test_generate_seats_action_creates_the_seats(self):
        generate_seats(self.seat_class_admin, self.request, SeatClass.objects.filter(pk=self.seat_class.pk))

        self.assertEqual(Seat.objects.filter(seat_class=self.seat_class).count(), 6)
        self.assertTrue(any("6 صندلی ساخته شد" in message for message in flashed(self.request)))

    # ----- deleting a seat class ----------------------------------------
    def delete(self):
        force_delete_seat_class(
            self.seat_class_admin, self.request, SeatClass.objects.filter(pk=self.seat_class.pk)
        )

    def test_class_without_reservations_is_deleted(self):
        self.delete()
        self.assertFalse(SeatClass.objects.filter(pk=self.seat_class.pk).exists())

    def test_class_with_a_live_pending_reservation_is_refused(self):
        self.reservation(
            status=Reservation.StatusChoices.PENDING_PAYMENT,
            payment_expires_at=timezone.now() + timedelta(minutes=20),
        )
        self.delete()
        self.assertTrue(SeatClass.objects.filter(pk=self.seat_class.pk).exists())
        self.assertEqual(Reservation.objects.count(), 1)

    def test_class_with_a_paid_reservation_is_refused(self):
        self.reservation(status=Reservation.StatusChoices.RESERVED, paid_at=timezone.now())
        self.delete()
        self.assertTrue(SeatClass.objects.filter(pk=self.seat_class.pk).exists())

    def test_cancelled_but_paid_history_is_never_deleted(self):
        # a paid reservation that was cancelled and refunded is a financial record
        self.reservation(
            status=Reservation.StatusChoices.CANCELLED, paid_at=timezone.now(),
            refund_amount=Decimal("800000.00"),
        )
        self.delete()
        self.assertTrue(SeatClass.objects.filter(pk=self.seat_class.pk).exists())
        self.assertEqual(Reservation.objects.count(), 1)

    def test_never_paid_history_is_deleted_together_with_the_class(self):
        self.reservation(
            status=Reservation.StatusChoices.CANCELLED,
            cancellation_reason=Reservation.CancellationReason.TIMEOUT,
        )
        self.delete()
        self.assertFalse(SeatClass.objects.filter(pk=self.seat_class.pk).exists())
        self.assertEqual(Reservation.objects.count(), 0)

    def test_an_overdue_pending_reservation_does_not_block_the_deletion(self):
        self.reservation(
            status=Reservation.StatusChoices.PENDING_PAYMENT,
            payment_expires_at=timezone.now() - timedelta(minutes=5),
        )
        self.delete()
        self.assertFalse(SeatClass.objects.filter(pk=self.seat_class.pk).exists())


class SeatClassAdminFormTests(TestCase):

    def setUp(self):
        self.flight = create_flight()
        self.seat_class = make_seat_class(self.flight, capacity=6)
        SeatClass.objects.filter(pk=self.seat_class.pk).update(available_seats=4)  # 2 booked
        self.seat_class.refresh_from_db()
        # the admin leaves read-only fields out of the form
        self.form_class = modelform_factory(
            SeatClass, form=SeatClassAdminForm, exclude=["available_seats"]
        )

    def data(self, capacity):
        return {
            "flight": self.flight.pk,
            "class_type": SeatClass.ClassTypeChoices.ECONOMY,
            "price_multiplier": "1.00",
            "capacity": str(capacity),
        }

    def test_available_seats_follow_the_capacity(self):
        form = self.form_class(self.data(8), instance=self.seat_class)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.available_seats, 6)  # 8 - 2 booked

    def test_capacity_below_the_booked_seats_is_rejected(self):
        form = self.form_class(self.data(1), instance=self.seat_class)
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_a_new_class_starts_with_every_seat_free(self):
        form = self.form_class({
            "flight": create_flight("IR777", route=self.flight.route, airline=self.flight.airline).pk,
            "class_type": SeatClass.ClassTypeChoices.ECONOMY,
            "price_multiplier": "1.00",
            "capacity": "9",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.available_seats, 9)


class FlightAdminTests(TestCase):

    def setUp(self):
        self.staff = CustomUser.objects.create_user(
            username="staff", password="pass12345", is_staff=True, is_superuser=True
        )
        self.buyer = CustomUser.objects.create_user(username="buyer", password="pass12345")
        self.flight = create_flight()
        self.seat_class = make_seat_class(self.flight, capacity=6)
        self.flight_admin = FlightAdmin(Flight, admin.site)
        self.form_class = modelform_factory(Flight, form=FlightAdminForm, fields="__all__")

    def form_data(self, flight, status):
        def local(value):
            return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")

        return {
            "flight_number": flight.flight_number,
            "route": flight.route_id,
            "airline": flight.airline_id,
            "airplane_type": flight.airplane_type,
            "departure_datetime": local(flight.departure_datetime),
            "arrival_datetime": local(flight.arrival_datetime),
            "base_price": "1000000",
            "cancellation_penalty_percent": "20",
            "status": status,
        }

    def test_a_cancelled_flight_with_reservations_cannot_be_reopened(self):
        Flight.objects.filter(pk=self.flight.pk).update(status=Flight.StatusChoices.CANCELLED)
        Reservation.objects.create(
            user=self.buyer, seat_class=self.seat_class, seats_count=1,
            total_paid_price=Decimal("1000000.00"),
            status=Reservation.StatusChoices.CANCELLED,
        )
        flight = Flight.objects.get(pk=self.flight.pk)

        form = self.form_class(self.form_data(flight, Flight.StatusChoices.SCHEDULED), instance=flight)

        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)

    def test_a_cancelled_flight_without_reservations_can_be_reopened(self):
        Flight.objects.filter(pk=self.flight.pk).update(status=Flight.StatusChoices.CANCELLED)
        flight = Flight.objects.get(pk=self.flight.pk)

        form = self.form_class(self.form_data(flight, Flight.StatusChoices.SCHEDULED), instance=flight)

        self.assertTrue(form.is_valid(), form.errors)

    def test_setting_the_status_to_cancelled_in_the_admin_refunds_the_reservations(self):
        reservation = Reservation.objects.create(
            user=self.buyer, seat_class=self.seat_class, seats_count=1,
            total_paid_price=Decimal("1000000.00"),
            status=Reservation.StatusChoices.RESERVED, paid_at=timezone.now(),
        )
        Flight.objects.filter(pk=self.flight.pk).update(status=Flight.StatusChoices.CANCELLED)
        flight = Flight.objects.get(pk=self.flight.pk)
        form = SimpleNamespace(instance=flight, changed_data=["status"], save_m2m=lambda: None)
        request = admin_request(self.staff)

        self.flight_admin.save_related(request, form, [], True)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.StatusChoices.CANCELLED)
        self.assertEqual(reservation.refund_amount, Decimal("1000000.00"))
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.wallet_balance, Decimal("1000000.00"))

    def test_other_changes_do_not_cancel_anything(self):
        reservation = Reservation.objects.create(
            user=self.buyer, seat_class=self.seat_class, seats_count=1,
            total_paid_price=Decimal("1000000.00"),
            status=Reservation.StatusChoices.RESERVED, paid_at=timezone.now(),
        )
        flight = Flight.objects.get(pk=self.flight.pk)
        form = SimpleNamespace(instance=flight, changed_data=["base_price"], save_m2m=lambda: None)

        self.flight_admin.save_related(admin_request(self.staff), form, [], True)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.StatusChoices.RESERVED)


class SeatAdminPermissionTests(TestCase):

    def setUp(self):
        self.staff = CustomUser.objects.create_user(
            username="staff", password="pass12345", is_staff=True, is_superuser=True
        )
        self.seat_admin = SeatAdmin(Seat, admin.site)
        self.request = admin_request(self.staff)

    def test_seats_cannot_be_added_changed_or_deleted_one_by_one(self):
        flight = create_flight()
        seat_class = make_seat_class(flight, capacity=6)
        generate_seats_for_flight(flight)
        seat = Seat.objects.filter(seat_class=seat_class).first()

        self.assertFalse(self.seat_admin.has_add_permission(self.request))
        self.assertFalse(self.seat_admin.has_change_permission(self.request, seat))
        self.assertFalse(self.seat_admin.has_delete_permission(self.request, seat))

    def test_cascading_deletes_of_a_flight_or_class_are_still_allowed(self):
        # Django asks without an object when a flight / seat class takes its seats along
        self.assertTrue(self.seat_admin.has_delete_permission(self.request))

    def test_bulk_delete_action_is_removed(self):
        self.assertNotIn("delete_selected", self.seat_admin.get_actions(self.request))