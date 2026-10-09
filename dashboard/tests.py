from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser, WalletTransaction
from flights.models import Airline, Airport, Flight, Route, SeatClass
from tickets.models import Reservation

STATUS = Reservation.StatusChoices


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
    Gross/Refunded/Net revenue on the main dashboard page – the figures the manager
    uses for financial decisions. Only reservations that were really PAID
    (paid_at is set) may count as revenue.
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
        now = timezone.now()

        # 1) Paid and still valid (1,000,000)
        Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('1000000.00'),
            status=STATUS.RESERVED, paid_at=now,
        )
        # 2) Paid, then cancelled with an 800,000 refund (20% penalty)
        Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('1000000.00'),
            status=STATUS.CANCELLED, paid_at=now, cancelled_at=now,
            refund_amount=Decimal('800000.00'),
        )
        # 3) Unpaid, still inside the payment window: pending, but NOT revenue
        Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('500000.00'),
            status=STATUS.PENDING_PAYMENT,
            payment_expires_at=now + timedelta(minutes=30),
        )
        # 4) Unpaid and cancelled by timeout: NOT revenue
        Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('700000.00'),
            status=STATUS.CANCELLED, cancelled_at=now,
            cancellation_reason=Reservation.CancellationReason.TIMEOUT,
        )

        self.client.force_login(self.staff_user)

    def test_financial_totals_count_only_paid_reservations(self):
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(response.context['total_gross'], Decimal('2000000.00'))
        self.assertEqual(response.context['total_refunded'], Decimal('800000.00'))
        self.assertEqual(response.context['total_net'], Decimal('1200000.00'))

    def test_reservation_counters(self):
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(response.context['paid_reservation_count'], 1)
        self.assertEqual(response.context['pending_reservation_count'], 1)
        self.assertEqual(response.context['pending_amount'], Decimal('500000.00'))

    def test_per_flight_table_matches_totals(self):
        response = self.client.get(reverse('dashboard:home'))
        row = list(response.context['flights_financials'])[0]
        self.assertEqual(row.gross_paid, Decimal('2000000.00'))
        self.assertEqual(row.total_refunded, Decimal('800000.00'))
        self.assertEqual(row.net_revenue, Decimal('1200000.00'))


class ReservationManageListTests(TestCase):
    """The reservation list must agree with the dashboard about what "pending" means."""

    def setUp(self):
        staff = CustomUser.objects.create_user(
            username='staffuser', password='pass12345', is_staff=True
        )
        buyer = CustomUser.objects.create_user(username='buyer', password='pass12345')
        seat_class = SeatClass.objects.create(
            flight=create_flight(), class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=10, available_seats=8,
        )
        now = timezone.now()
        self.live = Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('500000.00'),
            status=STATUS.PENDING_PAYMENT,
            payment_expires_at=now + timedelta(minutes=30),
        )
        self.expired = Reservation.objects.create(
            user=buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('500000.00'),
            status=STATUS.PENDING_PAYMENT,
            payment_expires_at=now - timedelta(minutes=5),
        )
        self.url = reverse('dashboard:reservation_manage_list')
        self.client.force_login(staff)

    def _ids(self, response):
        return [r.pk for r in response.context['reservations']]

    def test_pending_tab_lists_only_live_pending(self):
        response = self.client.get(self.url, {'filter': 'pending'})
        self.assertEqual(self._ids(response), [self.live.pk])

    def test_expired_pending_is_released_and_moves_to_unpaid_cancelled(self):
        response = self.client.get(self.url, {'filter': 'unpaid_cancelled'})
        self.assertEqual(self._ids(response), [self.expired.pk])
        self.expired.refresh_from_db()
        self.assertEqual(self.expired.status, STATUS.CANCELLED)
        self.assertEqual(
            self.expired.cancellation_reason, Reservation.CancellationReason.TIMEOUT
        )

    def test_unknown_filter_falls_back_to_all(self):
        response = self.client.get(self.url, {'filter': 'abc'})
        self.assertEqual(response.context['current_filter'], '')
        self.assertEqual(len(self._ids(response)), 2)


class FlightCancelTests(TestCase):
    """Cancelling a flight refunds paid reservations and can safely be repeated."""

    def setUp(self):
        self.staff = CustomUser.objects.create_user(
            username='staffuser', password='pass12345', is_staff=True
        )
        self.buyer = CustomUser.objects.create_user(username='buyer', password='pass12345')
        self.flight = create_flight()
        seat_class = SeatClass.objects.create(
            flight=self.flight, class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=10, available_seats=8,
        )
        now = timezone.now()
        self.paid = Reservation.objects.create(
            user=self.buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('1000000.00'),
            status=STATUS.RESERVED, paid_at=now,
        )
        self.pending = Reservation.objects.create(
            user=self.buyer, seat_class=seat_class, seats_count=1,
            total_paid_price=Decimal('500000.00'),
            status=STATUS.PENDING_PAYMENT,
            payment_expires_at=now + timedelta(minutes=30),
        )
        self.cancel_url = reverse('dashboard:flight_cancel', kwargs={'pk': self.flight.pk})
        self.detail_url = reverse('dashboard:flight_manage_detail', kwargs={'pk': self.flight.pk})
        self.client.force_login(self.staff)

    def _assert_everything_cancelled_and_refunded(self):
        self.flight.refresh_from_db()
        self.assertEqual(self.flight.status, Flight.StatusChoices.CANCELLED)
        self.paid.refresh_from_db()
        self.pending.refresh_from_db()
        self.assertEqual(self.paid.status, STATUS.CANCELLED)
        self.assertEqual(self.paid.refund_amount, Decimal('1000000.00'))
        self.assertEqual(self.pending.status, STATUS.CANCELLED)
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.wallet_balance, Decimal('1000000.00'))
        entry = WalletTransaction.objects.get(user=self.buyer)
        self.assertEqual(entry.kind, WalletTransaction.KindChoices.REFUND)
        self.assertEqual(entry.reference, self.paid.booking_reference)

    def test_cancel_refunds_paid_and_releases_pending(self):
        response = self.client.post(self.cancel_url)
        self.assertRedirects(response, self.detail_url, fetch_redirect_response=False)
        self._assert_everything_cancelled_and_refunded()

    def test_cancel_is_safe_when_status_was_already_set_to_cancelled(self):
        # e.g. the manager set the status by hand, or an earlier refund run was interrupted
        Flight.objects.filter(pk=self.flight.pk).update(status=Flight.StatusChoices.CANCELLED)
        self.client.post(self.cancel_url)
        self._assert_everything_cancelled_and_refunded()

    def test_cancelling_twice_refunds_only_once(self):
        self.client.post(self.cancel_url)
        self.client.post(self.cancel_url)
        self._assert_everything_cancelled_and_refunded()

    def test_cancel_button_stays_available_while_reservations_remain(self):
        Flight.objects.filter(pk=self.flight.pk).update(status=Flight.StatusChoices.CANCELLED)
        response = self.client.get(self.detail_url)
        self.assertTrue(response.context['can_cancel_flight'])
        self.assertTrue(response.context['needs_cancel_retry'])

        self.client.post(self.cancel_url)
        response = self.client.get(self.detail_url)
        self.assertFalse(response.context['can_cancel_flight'])
        self.assertFalse(response.context['needs_cancel_retry'])

    def test_get_is_not_allowed_on_cancel_url(self):
        self.assertEqual(self.client.get(self.cancel_url).status_code, 405)