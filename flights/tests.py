from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import Airline, Airport, Flight, Route, Seat, SeatClass
from .services import generate_seats_for_flight


def create_route():
    origin = Airport.objects.create(name="امام خمینی", city="تهران", iata_code="IKA")
    destination = Airport.objects.create(name="شهید هاشمی‌نژاد", city="مشهد", iata_code="MHD")
    return Route.objects.create(origin=origin, destination=destination), origin, destination


class RouteConstraintTests(TestCase):
    def test_self_route_is_rejected(self):
        origin = Airport.objects.create(name="امام خمینی", city="تهران", iata_code="IKA")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Route.objects.create(origin=origin, destination=origin)

    def test_duplicate_route_is_rejected(self):
        route, origin, destination = create_route()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Route.objects.create(origin=origin, destination=destination)


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
                    base_price=Decimal('1000000'),
                )


class SeatClassReservationTests(TestCase):
    """تست منطق اتمیک رزرو/آزادسازی صندلی - قلب سیستم جلوگیری از overbooking."""

    def setUp(self):
        route, _, _ = create_route()
        airline = Airline.objects.create(name="ایران ایر")
        flight = Flight.objects.create(
            flight_number="IR200",
            route=route,
            airline=airline,
            airplane_type=Flight.AirplaneTypeChoices.A320,
            departure_datetime=timezone.now() + timedelta(days=1),
            arrival_datetime=timezone.now() + timedelta(days=1, hours=2),
            base_price=Decimal('1000000'),
        )
        self.seat_class = SeatClass.objects.create(
            flight=flight, class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=10, available_seats=10,
        )

    def test_reserve_seats_decrements_availability(self):
        self.seat_class.reserve_seats(3)
        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 7)

    def test_reserve_seats_fails_when_insufficient_capacity(self):
        with self.assertRaises(ValueError):
            self.seat_class.reserve_seats(11)
        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 10)

    def test_release_seats_increments_availability(self):
        self.seat_class.reserve_seats(3)
        self.seat_class.release_seats(3)
        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 10)

    def test_final_price_applies_multiplier(self):
        self.seat_class.price_multiplier = Decimal('1.5')
        self.seat_class.save()
        self.assertEqual(self.seat_class.final_price, Decimal('1500000.00'))


class SeatGenerationTests(TestCase):
    """تست ساخت خودکار صندلی‌ها - ردیف‌های پیوسته و جلوگیری از ساخت تکراری."""

    def setUp(self):
        route, _, _ = create_route()
        airline = Airline.objects.create(name="ایران ایر")
        self.flight = Flight.objects.create(
            flight_number="IR300",
            route=route,
            airline=airline,
            airplane_type=Flight.AirplaneTypeChoices.A320,
            departure_datetime=timezone.now() + timedelta(days=1),
            arrival_datetime=timezone.now() + timedelta(days=1, hours=2),
            base_price=Decimal('1000000'),
        )
        self.economy = SeatClass.objects.create(
            flight=self.flight, class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=8, available_seats=8,
        )
        self.business = SeatClass.objects.create(
            flight=self.flight, class_type=SeatClass.ClassTypeChoices.BUSINESS,
            capacity=4, available_seats=4,
        )

    def test_seats_created_with_correct_count(self):
        generate_seats_for_flight(self.flight)
        self.assertEqual(Seat.objects.filter(seat_class=self.economy).count(), 8)
        self.assertEqual(Seat.objects.filter(seat_class=self.business).count(), 4)

    def test_row_numbers_are_continuous_across_classes(self):
        generate_seats_for_flight(self.flight)
        economy_max_row = (
            Seat.objects.filter(seat_class=self.economy).order_by('-row_number').first().row_number
        )
        business_min_row = (
            Seat.objects.filter(seat_class=self.business).order_by('row_number').first().row_number
        )
        self.assertEqual(business_min_row, economy_max_row + 1)

    def test_skips_classes_that_already_have_seats(self):
        generate_seats_for_flight(self.flight)
        created_again, skipped = generate_seats_for_flight(self.flight)
        self.assertEqual(created_again, 0)
        self.assertEqual(len(skipped), 2)