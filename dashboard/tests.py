from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from flights.models import Airline, Airport, Flight, Route, SeatClass
from tickets.models import Reservation


def create_flight():
    origin = Airport.objects.create(name="امام خمینی", city="تهران", iata_code="IKA")
    destination = Airport.objects.create(name="شهید هاشمی‌نژاد", city="مشهد", iata_code="MHD")
    route = Route.objects.create(origin=origin, destination=destination)
    airline = Airline.objects.create(name="ایران ایر")
    return Flight.objects.create(
        flight_number="IR500",
        route=route,
        airline=airline,
        airplane_type=Flight.AirplaneTypeChoices.A320,
        departure_datetime=timezone.now() + timedelta(days=1),
        arrival_datetime=timezone.now() + timedelta(days=1, hours=2),
        base_price=Decimal('1000000'),
    )


class StaffAccessControlTests(TestCase):
    """ Test ensuring that only staff users have access to dashboard pages."""

    def setUp(self):
        self.regular_user = CustomUser.objects.create_user(username='regular', password='pass12345')
        self.staff_user = CustomUser.objects.create_user(
            username='staffuser', password='pass12345', is_staff=True
        )

    def test_anonymous_user_cannot_access_dashboard(self):
        response = self.client.get(reverse('dashboard:home'))
        self.assertNotEqual(response.status_code, 200)

    def test_regular_user_is_redirected_away_from_dashboard(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse('dashboard:home'))
        self.assertRedirects(response, reverse('flights:flight_list'))

    def test_regular_user_cannot_access_flight_management(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse('dashboard:flight_manage_list'))
        self.assertRedirects(response, reverse('flights:flight_list'))

    def test_regular_user_cannot_access_user_management(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse('dashboard:user_manage_list'))
        self.assertRedirects(response, reverse('flights:flight_list'))

    def test_staff_user_can_access_dashboard(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(response.status_code, 200)

    def test_staff_user_can_access_flight_management(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse('dashboard:flight_manage_list'))
        self.assertEqual(response.status_code, 200)


class FinancialSummaryTests(TestCase):
    """
    Gross/Refundable/Net revenue calculation test on the main dashboard page –
    precisely the figures the manager looks at for financial decision-making.
    """

    def setUp(self):
        self.staff_user = CustomUser.objects.create_user(
            username='staffuser', password='pass12345', is_staff=True
        )
        buyer = CustomUser.objects.create_user(username='buyer', password='pass12345')

        flight = create_flight()
        seat_class = SeatClass.objects.create(
            flight=flight, class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=10, available_seats=8,
        )

        # One active reservation (1,000,000 Tomans)
        Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('1000000.00'),
        )
        # A cancelled booking with an 800,000-toman refund (20% penalty)
        Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('1000000.00'),
            status=Reservation.StatusChoices.CANCELLED,
            refund_amount=Decimal('800000.00'),
        )

        self.client.force_login(self.staff_user)

    def test_financial_totals_are_calculated_correctly(self):
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(response.context['total_gross'], Decimal('2000000.00'))
        self.assertEqual(response.context['total_refunded'], Decimal('800000.00'))
        self.assertEqual(response.context['total_net'], Decimal('1200000.00'))

    def test_active_reservation_count_excludes_cancelled(self):
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(response.context['active_reservation_count'], 1)