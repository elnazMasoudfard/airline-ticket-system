from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from flights.models import Airline, Airport, Flight, Route, Seat, SeatClass
from flights.services import generate_seats_for_flight

from .models import Reservation, ReservationSeat


class BookingFlowTests(TestCase):
    """
    تست کامل جریان رزرو از طریق view ها (نه مستقیم مدل)، دقیقاً همان مسیری
    که یک کاربر واقعی از توی مرورگر طی می‌کند: انتخاب صندلی -> پرداخت از
    کیف پول -> کنسلی و استرداد.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(username='traveler', password='pass12345')
        self.user.deposit(Decimal('5000000'))

        origin = Airport.objects.create(name="امام خمینی", city="تهران", iata_code="IKA")
        destination = Airport.objects.create(name="شهید هاشمی‌نژاد", city="مشهد", iata_code="MHD")
        route = Route.objects.create(origin=origin, destination=destination)
        airline = Airline.objects.create(name="ایران ایر")

        self.flight = Flight.objects.create(
            flight_number="IR400",
            route=route,
            airline=airline,
            airplane_type=Flight.AirplaneTypeChoices.A320,
            departure_datetime=timezone.now() + timedelta(days=2),
            arrival_datetime=timezone.now() + timedelta(days=2, hours=2),
            base_price=Decimal('1000000'),
        )
        self.seat_class = SeatClass.objects.create(
            flight=self.flight, class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=6, available_seats=6,
        )
        generate_seats_for_flight(self.flight)

        self.client.force_login(self.user)

    def _seats(self):
        return list(
            Seat.objects.filter(seat_class=self.seat_class).order_by('row_number', 'column_letter')
        )

    def test_booking_two_adjacent_seats_succeeds_and_debits_wallet(self):
        seats = self._seats()[:2]  # هر دو در ردیف ۱، ستون‌های A و B - کنار هم

        self.client.post(
            reverse('tickets:seat_selection', kwargs={'seat_class_id': self.seat_class.pk}) + '?count=2',
            {'seats_count': 2, 'seat_ids': [seats[0].pk, seats[1].pk]},
        )

        self.assertEqual(Reservation.objects.count(), 1)
        reservation = Reservation.objects.first()
        self.assertEqual(reservation.seats_count, 2)
        self.assertEqual(ReservationSeat.objects.filter(reservation=reservation).count(), 2)

        expected_price = self.seat_class.final_price * 2
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, Decimal('5000000') - expected_price)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 4)

    def test_booking_non_adjacent_seats_is_rejected(self):
        seats = self._seats()
        non_adjacent = [seats[0], seats[2]]  # ستون A و C - کنار هم نیستند

        self.client.post(
            reverse('tickets:seat_selection', kwargs={'seat_class_id': self.seat_class.pk}) + '?count=2',
            {'seats_count': 2, 'seat_ids': [non_adjacent[0].pk, non_adjacent[1].pk]},
        )

        self.assertEqual(Reservation.objects.count(), 0)
        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 6)

    def test_cancellation_releases_seat_and_refunds_wallet(self):
        seat = self._seats()[0]
        self.client.post(
            reverse('tickets:seat_selection', kwargs={'seat_class_id': self.seat_class.pk}) + '?count=1',
            {'seats_count': 1, 'seat_ids': [seat.pk]},
        )
        reservation = Reservation.objects.first()
        balance_after_booking = CustomUser.objects.get(pk=self.user.pk).wallet_balance

        self.client.post(reverse('tickets:reservation_cancel', args=[reservation.booking_reference]))

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.StatusChoices.CANCELLED)

        seat.refresh_from_db()
        self.assertTrue(seat.is_available)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 6)

        expected_refund = (reservation.total_paid_price * Decimal('0.8')).quantize(Decimal('0.01'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, balance_after_booking + expected_refund)

    def test_insufficient_balance_blocks_reservation(self):
        poor_user = CustomUser.objects.create_user(username='poor', password='pass12345')
        self.client.force_login(poor_user)

        self.client.post(
            reverse('tickets:reservation_create', kwargs={'seat_class_id': self.seat_class.pk}),
            {'seats_count': 1},
        )

        self.assertEqual(Reservation.objects.count(), 0)