import itertools
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser, WalletTransaction
from flights.models import Airline, Airport, Flight, Route, Seat, SeatClass
from flights.services import generate_seats_for_flight

from .admin import PassengerAdmin, ReservationAdmin
from .forms import PassengerForm, is_valid_iranian_national_id, normalize_digits
from .models import Passenger, Reservation, ReservationSeat
from .services import (
    MAX_PENDING_RESERVATIONS_PER_USER,
    PAYMENT_WINDOW_MINUTES,
    BookingError,
    PayResult,
    column_index,
    create_pending_reservation,
    expire_pending_reservations,
    get_penalty_percent,
    has_adjacent_block,
    pay_reservation,
)

STATUS = Reservation.StatusChoices
REASON = Reservation.CancellationReason
KIND = WalletTransaction.KindChoices


class TicketsTestCase(TestCase):
    """
    Base class: one flight (A320, economy class with 6 seats), one traveler with
    5,000,000 in the wallet, plus helpers that follow the exact path a real user
    takes in the browser:

        select seats  ->  enter passengers  ->  pay  ->  (cancel)

    A new reservation is PENDING_PAYMENT: seats are held, nothing is charged yet.
    The wallet is charged only by the payment step.
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
        self._national_ids = itertools.count(1000000000)

    # ----- small helpers -------------------------------------------------
    def new_national_id(self):
        return str(next(self._national_ids))

    def all_seats(self):
        return list(Seat.objects.filter(seat_class=self.seat_class).order_by('row_number', 'column_letter'))

    def free_seats(self):
        return list(
            Seat.objects.filter(seat_class=self.seat_class, is_available=True)
            .order_by('row_number', 'column_letter')
        )

    def wallet(self, user=None):
        user = user or self.user
        user.refresh_from_db()
        return user.wallet_balance

    def refresh_seat_class(self):
        self.seat_class.refresh_from_db()
        return self.seat_class.available_seats

    def update_flight(self, **fields):
        Flight.objects.filter(pk=self.flight.pk).update(**fields)

    def expire_now(self, reservation):
        """Move the payment deadline into the past WITHOUT running any expiry."""
        Reservation.objects.filter(pk=reservation.pk).update(
            payment_expires_at=timezone.now() - timedelta(minutes=1)
        )

    # ----- urls ----------------------------------------------------------
    def detail_url(self, reservation):
        return reverse('tickets:reservation_detail', kwargs={'booking_reference': reservation.booking_reference})

    def cancel_url(self, reservation):
        return reverse('tickets:reservation_cancel', kwargs={'booking_reference': reservation.booking_reference})

    def passengers_url(self, reservation):
        return reverse('tickets:add_passengers', kwargs={'booking_reference': reservation.booking_reference})

    def payment_url(self, reservation):
        return reverse('tickets:reservation_payment', kwargs={'booking_reference': reservation.booking_reference})

    # ----- the booking steps --------------------------------------------
    def select_seats(self, count=1, seats=None):
        seats = seats if seats is not None else self.free_seats()[:count]
        url = reverse('tickets:seat_selection', kwargs={'seat_class_id': self.seat_class.pk}) + f'?count={count}'
        return self.client.post(url, {'seats_count': count, 'seat_ids': [seat.pk for seat in seats]})

    def book(self, count=1, seats=None):
        response = self.select_seats(count, seats)
        self.assertEqual(response.status_code, 302)
        return Reservation.objects.order_by('-pk').first()

    def passenger_data(self, passengers, initial=0, ids=None):
        data = {
            'form-TOTAL_FORMS': str(len(passengers)),
            'form-INITIAL_FORMS': str(initial),
            'form-MIN_NUM_FORMS': str(len(passengers)),
            'form-MAX_NUM_FORMS': str(len(passengers)),
        }
        for index, passenger in enumerate(passengers):
            data[f'form-{index}-first_name'] = passenger['first_name']
            data[f'form-{index}-last_name'] = passenger['last_name']
            data[f'form-{index}-national_id'] = passenger['national_id']
            if ids:
                data[f'form-{index}-id'] = str(ids[index])
        return data

    def make_passengers(self, national_ids):
        return [
            {'first_name': f'Name{index}', 'last_name': 'Family', 'national_id': national_id}
            for index, national_id in enumerate(national_ids)
        ]

    def add_passengers(self, reservation, national_ids=None):
        national_ids = national_ids or [self.new_national_id() for _ in range(reservation.seats_count)]
        return self.client.post(
            self.passengers_url(reservation),
            self.passenger_data(self.make_passengers(national_ids)),
        )

    def pay(self, reservation):
        return self.client.post(self.payment_url(reservation))

    def book_with_passengers(self, count=1, seats=None):
        reservation = self.book(count, seats)
        response = self.add_passengers(reservation)
        self.assertEqual(response.status_code, 302)
        return reservation

    def book_and_pay(self, count=1):
        reservation = self.book_with_passengers(count)
        response = self.pay(reservation)
        self.assertRedirects(response, self.detail_url(reservation), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.RESERVED)
        return reservation


# ======================================================================
# Flights that cannot be booked
# ======================================================================
class FlightNotBookableTests(TicketsTestCase):
    def _assert_nothing_changed(self, initial_balance):
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(self.refresh_seat_class(), 6)
        self.assertEqual(self.wallet(), initial_balance)

    def test_cancelled_flight_cannot_be_booked_from_reservation_create(self):
        self.update_flight(status=Flight.StatusChoices.CANCELLED)
        response = self.client.post(
            reverse('tickets:reservation_create', kwargs={'seat_class_id': self.seat_class.pk}),
            {'seats_count': 1},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 0)

    def test_past_scheduled_flight_cannot_be_booked_from_reservation_create(self):
        self.update_flight(departure_datetime=timezone.now() - timedelta(hours=1))
        response = self.client.post(
            reverse('tickets:reservation_create', kwargs={'seat_class_id': self.seat_class.pk}),
            {'seats_count': 1},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 0)

    def test_cancelled_flight_cannot_be_booked_from_seat_selection(self):
        self.update_flight(status=Flight.StatusChoices.CANCELLED)
        seat = self.all_seats()[0]
        balance = self.wallet()

        response = self.select_seats(1, [seat])

        self.assertEqual(response.status_code, 302)
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)
        self._assert_nothing_changed(balance)

    def test_past_scheduled_flight_cannot_be_booked_from_seat_selection(self):
        self.update_flight(departure_datetime=timezone.now() - timedelta(hours=1))
        seat = self.all_seats()[0]
        balance = self.wallet()

        response = self.select_seats(1, [seat])

        self.assertEqual(response.status_code, 302)
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)
        self._assert_nothing_changed(balance)


# ======================================================================
# Step 2: seat selection
# ======================================================================
class SeatSelectionTests(TicketsTestCase):
    def test_booking_two_adjacent_seats_holds_them_without_charging(self):
        seats = self.all_seats()[:2]  # side by side in the first row
        balance = self.wallet()

        reservation = self.book(2, seats)

        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)
        self.assertEqual(reservation.seats_count, 2)
        self.assertEqual(ReservationSeat.objects.filter(reservation=reservation).count(), 2)
        self.assertEqual(reservation.total_paid_price, self.seat_class.final_price * 2)
        self.assertIsNone(reservation.paid_at)
        self.assertEqual(self.wallet(), balance)  # nothing is charged before payment
        self.assertEqual(self.refresh_seat_class(), 4)
        for seat in seats:
            seat.refresh_from_db()
            self.assertFalse(seat.is_available)

        remaining = reservation.payment_expires_at - timezone.now()
        self.assertLess(abs(remaining - timedelta(minutes=PAYMENT_WINDOW_MINUTES)), timedelta(minutes=1))

    def test_booking_non_adjacent_seats_is_rejected(self):
        seats = self.all_seats()
        response = self.select_seats(2, [seats[0], seats[2]])  # columns A and C

        self.assertEqual(response.status_code, 200)  # the seat map is shown again
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(self.refresh_seat_class(), 6)

    def test_selecting_the_same_seat_twice_is_rejected(self):
        seat = self.all_seats()[0]
        response = self.select_seats(2, [seat, seat])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(self.refresh_seat_class(), 6)

    def test_a_seat_held_by_someone_else_cannot_be_taken(self):
        seat = self.all_seats()[0]
        self.book(1, [seat])

        other = CustomUser.objects.create_user(username='other', password='pass12345')
        self.client.force_login(other)
        response = self.select_seats(1, [seat])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(self.refresh_seat_class(), 5)

    def test_pnr_is_short_unique_and_uppercase(self):
        first = self.book(1)
        second = self.book(1)
        for reservation in (first, second):
            self.assertEqual(len(reservation.booking_reference), 8)
            self.assertTrue(reservation.booking_reference.isalnum())
            self.assertEqual(reservation.booking_reference, reservation.booking_reference.upper())
        self.assertNotEqual(first.booking_reference, second.booking_reference)

    def test_cancelled_seat_can_be_booked_again(self):
        seat = self.all_seats()[0]
        first = self.book(1, [seat])
        self.client.post(self.cancel_url(first))

        self.assertFalse(ReservationSeat.objects.filter(seat=seat).exists())
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)

        second = self.book(1, [seat])
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.status, STATUS.PENDING_PAYMENT)
        self.assertTrue(ReservationSeat.objects.filter(reservation=second, seat=seat).exists())


# ======================================================================
# Step 4: payment
# ======================================================================
class PaymentTests(TicketsTestCase):
    def test_payment_charges_wallet_and_finalises_reservation(self):
        reservation = self.book_with_passengers()
        balance = self.wallet()

        response = self.pay(reservation)

        self.assertRedirects(response, self.detail_url(reservation), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.RESERVED)
        self.assertIsNotNone(reservation.paid_at)
        self.assertEqual(self.wallet(), balance - reservation.total_paid_price)

        entry = WalletTransaction.objects.get(user=self.user, kind=KIND.PAYMENT)
        self.assertEqual(entry.amount, -reservation.total_paid_price)
        self.assertEqual(entry.reference, reservation.booking_reference)

    def test_payment_is_refused_until_all_passengers_are_entered(self):
        reservation = self.book(2)
        balance = self.wallet()

        response = self.pay(reservation)

        self.assertRedirects(response, self.passengers_url(reservation), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)
        self.assertEqual(self.wallet(), balance)

    def test_payment_page_redirects_to_passengers_when_incomplete(self):
        reservation = self.book(1)
        response = self.client.get(self.payment_url(reservation))
        self.assertRedirects(response, self.passengers_url(reservation), fetch_redirect_response=False)

    def test_paying_twice_charges_only_once(self):
        reservation = self.book_with_passengers()
        self.pay(reservation)
        balance_after_first = self.wallet()

        response = self.pay(reservation)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.wallet(), balance_after_first)
        self.assertEqual(WalletTransaction.objects.filter(user=self.user, kind=KIND.PAYMENT).count(), 1)

    def test_payment_after_the_deadline_cancels_and_releases_the_seats(self):
        reservation = self.book_with_passengers()
        seat = reservation.reservation_seats.get().seat
        balance = self.wallet()
        self.expire_now(reservation)

        response = self.pay(reservation)

        self.assertRedirects(response, self.detail_url(reservation), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)
        self.assertEqual(reservation.cancellation_reason, REASON.TIMEOUT)
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)
        self.assertEqual(self.refresh_seat_class(), 6)
        self.assertEqual(self.wallet(), balance)

    def test_insufficient_balance_keeps_the_reservation_pending(self):
        poor = CustomUser.objects.create_user(username='poor', password='pass12345')
        self.client.force_login(poor)
        reservation = self.book_with_passengers()

        response = self.pay(reservation)

        self.assertRedirects(response, self.payment_url(reservation), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)
        self.assertEqual(self.wallet(poor), Decimal('0.00'))
        self.assertFalse(WalletTransaction.objects.filter(user=poor, kind=KIND.PAYMENT).exists())

    def test_payment_is_refused_when_the_flight_is_no_longer_bookable(self):
        reservation = self.book_with_passengers()
        balance = self.wallet()
        self.update_flight(status=Flight.StatusChoices.CANCELLED)

        self.pay(reservation)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)
        self.assertEqual(self.wallet(), balance)

    def test_other_users_cannot_pay_for_my_reservation(self):
        reservation = self.book_with_passengers()
        other = CustomUser.objects.create_user(username='other', password='pass12345')
        other.deposit(Decimal('5000000'))
        self.client.force_login(other)

        self.assertEqual(self.pay(reservation).status_code, 404)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)

    def test_pay_reservation_service_returns_result_and_reservation(self):
        reservation = self.book_with_passengers()

        result, paid = pay_reservation(booking_reference=reservation.booking_reference, user=self.user)
        self.assertIs(result, PayResult.PAID)
        self.assertEqual(paid.pk, reservation.pk)
        self.assertEqual(paid.status, STATUS.RESERVED)

        result, _ = pay_reservation(booking_reference=reservation.booking_reference, user=self.user)
        self.assertIs(result, PayResult.ALREADY_PAID)

    def test_pay_reservation_service_raises_for_a_stranger(self):
        reservation = self.book_with_passengers()
        other = CustomUser.objects.create_user(username='other', password='pass12345')
        with self.assertRaises(Reservation.DoesNotExist):
            pay_reservation(booking_reference=reservation.booking_reference, user=other)


# ======================================================================
# Cancellation and refunds
# ======================================================================
class CancellationTests(TicketsTestCase):
    def test_cancelling_a_pending_reservation_releases_seats_without_refund(self):
        reservation = self.book(1)
        seat = reservation.reservation_seats.get().seat
        balance = self.wallet()

        response = self.client.post(self.cancel_url(reservation))

        self.assertRedirects(response, reverse('tickets:reservation_list'), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)
        self.assertEqual(reservation.cancellation_reason, REASON.USER)
        self.assertEqual(reservation.refund_amount, Decimal('0.00'))
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)
        self.assertEqual(self.refresh_seat_class(), 6)
        self.assertEqual(self.wallet(), balance)

    def test_cancelling_a_paid_reservation_refunds_minus_the_penalty(self):
        self.update_flight(cancellation_penalty_percent=20)
        reservation = self.book_and_pay()
        seat = reservation.reservation_seats.get().seat
        balance = self.wallet()

        self.client.post(self.cancel_url(reservation))

        reservation.refresh_from_db()
        expected = (reservation.total_paid_price * Decimal('0.8')).quantize(Decimal('0.01'))
        self.assertEqual(reservation.status, STATUS.CANCELLED)
        self.assertEqual(reservation.refund_amount, expected)
        self.assertEqual(self.wallet(), balance + expected)
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)
        self.assertEqual(self.refresh_seat_class(), 6)

        entry = WalletTransaction.objects.get(user=self.user, kind=KIND.REFUND)
        self.assertEqual(entry.amount, expected)
        self.assertEqual(entry.reference, reservation.booking_reference)

    def test_100_percent_penalty_does_not_deposit_zero(self):
        self.update_flight(cancellation_penalty_percent=100)
        reservation = self.book_and_pay()
        balance = self.wallet()

        response = self.client.post(self.cancel_url(reservation))

        self.assertEqual(response.status_code, 302)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)
        self.assertEqual(reservation.refund_amount, Decimal('0.00'))
        self.assertEqual(self.wallet(), balance)
        self.assertFalse(WalletTransaction.objects.filter(user=self.user, kind=KIND.REFUND).exists())

    def test_cancelling_twice_refunds_only_once(self):
        self.update_flight(cancellation_penalty_percent=20)
        reservation = self.book_and_pay()

        self.client.post(self.cancel_url(reservation))
        balance_after_first = self.wallet()
        response = self.client.post(self.cancel_url(reservation))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.wallet(), balance_after_first)
        self.assertEqual(WalletTransaction.objects.filter(user=self.user, kind=KIND.REFUND).count(), 1)

    def test_paid_reservation_cannot_be_cancelled_after_the_flight_started(self):
        reservation = self.book_and_pay()
        seat = reservation.reservation_seats.get().seat
        self.update_flight(
            departure_datetime=timezone.now() - timedelta(hours=1),
            arrival_datetime=timezone.now() + timedelta(hours=1),
        )
        balance = self.wallet()

        response = self.client.post(self.cancel_url(reservation))

        self.assertEqual(response.status_code, 302)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.RESERVED)
        seat.refresh_from_db()
        self.assertFalse(seat.is_available)
        self.assertEqual(self.refresh_seat_class(), 5)
        self.assertEqual(self.wallet(), balance)

    def test_paid_reservation_cannot_be_cancelled_after_the_flight_ended(self):
        reservation = self.book_and_pay()
        self.update_flight(
            departure_datetime=timezone.now() - timedelta(hours=3),
            arrival_datetime=timezone.now() - timedelta(hours=1),
        )
        balance = self.wallet()

        self.client.post(self.cancel_url(reservation))

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.RESERVED)
        self.assertEqual(self.refresh_seat_class(), 5)
        self.assertEqual(self.wallet(), balance)

    def test_when_the_flight_is_cancelled_the_refund_is_100_percent(self):
        self.update_flight(cancellation_penalty_percent=20)  # must be ignored
        reservation = self.book_and_pay()
        self.update_flight(status=Flight.StatusChoices.CANCELLED)
        balance = self.wallet()

        self.client.post(self.cancel_url(reservation))

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)
        self.assertEqual(reservation.refund_amount, reservation.total_paid_price)
        self.assertEqual(self.wallet(), balance + reservation.total_paid_price)

    def test_other_users_cannot_cancel_my_reservation(self):
        reservation = self.book(1)
        other = CustomUser.objects.create_user(username='other', password='pass12345')
        self.client.force_login(other)

        self.assertEqual(self.client.post(self.cancel_url(reservation)).status_code, 404)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)

    def test_cancel_requires_post(self):
        reservation = self.book(1)
        self.assertEqual(self.client.get(self.cancel_url(reservation)).status_code, 405)


# ======================================================================
# Step 3: passengers (entering, editing, duplicates)
# ======================================================================
class PassengerTests(TicketsTestCase):
    def test_passenger_form_requires_all_passengers(self):
        reservation = self.book(2)
        response = self.client.post(
            self.passengers_url(reservation),
            self.passenger_data([
                {'first_name': 'Ali', 'last_name': 'Ahmadi', 'national_id': '1234567890'},
                {'first_name': '', 'last_name': '', 'national_id': ''},
            ]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Passenger.objects.filter(reservation=reservation).count(), 0)

    def test_duplicate_national_ids_are_rejected_without_partial_save(self):
        reservation = self.book(2)
        response = self.client.post(
            self.passengers_url(reservation),
            self.passenger_data([
                {'first_name': 'Ali', 'last_name': 'Ahmadi', 'national_id': '1234567890'},
                {'first_name': 'Reza', 'last_name': 'Ahmadi', 'national_id': '1234567890'},
            ]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Passenger.objects.filter(reservation=reservation).count(), 0)

    def test_persian_and_english_digits_count_as_the_same_national_id(self):
        reservation = self.book(2)
        response = self.add_passengers(reservation, ['۱۲۳۴۵۶۷۸۹۰', '1234567890'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Passenger.objects.filter(reservation=reservation).count(), 0)

    def test_persian_digits_are_saved_as_english_digits(self):
        reservation = self.book(1)
        response = self.add_passengers(reservation, ['۱۲۳۴۵۶۷۸۹۰'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(reservation.passengers.get().national_id, '1234567890')

    def test_repeated_submission_does_not_create_duplicates(self):
        reservation = self.book(1)
        data = self.passenger_data(self.make_passengers(['1234567890']))

        first = self.client.post(self.passengers_url(reservation), data)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(Passenger.objects.filter(reservation=reservation).count(), 1)

        # The same (stale) form submitted again, e.g. by a double click.
        second = self.client.post(self.passengers_url(reservation), data)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Passenger.objects.filter(reservation=reservation).count(), 1)

    def test_passengers_can_be_edited_while_the_reservation_is_pending(self):
        reservation = self.book_with_passengers()
        passenger = reservation.passengers.get()

        page = self.client.get(self.passengers_url(reservation))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['is_edit'])
        self.assertEqual(len(page.context['formset'].forms), 1)

        response = self.client.post(
            self.passengers_url(reservation),
            self.passenger_data(
                [{'first_name': 'Fixed', 'last_name': 'Name', 'national_id': passenger.national_id}],
                initial=1, ids=[passenger.pk],
            ),
        )

        self.assertRedirects(response, self.payment_url(reservation), fetch_redirect_response=False)
        passenger.refresh_from_db()
        self.assertEqual(passenger.first_name, 'Fixed')
        self.assertEqual(reservation.passengers.count(), 1)

    def test_passengers_can_still_be_corrected_after_payment(self):
        reservation = self.book_and_pay()
        passenger = reservation.passengers.get()

        response = self.client.post(
            self.passengers_url(reservation),
            self.passenger_data(
                [{'first_name': 'Corrected', 'last_name': 'Name', 'national_id': passenger.national_id}],
                initial=1, ids=[passenger.pk],
            ),
        )

        self.assertRedirects(response, self.detail_url(reservation), fetch_redirect_response=False)
        passenger.refresh_from_db()
        self.assertEqual(passenger.first_name, 'Corrected')
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.RESERVED)
        self.assertEqual(reservation.passengers.count(), 1)

    def test_passengers_cannot_be_edited_after_the_flight_departed(self):
        reservation = self.book_and_pay()
        passenger = reservation.passengers.get()
        old_name = passenger.first_name
        self.update_flight(
            departure_datetime=timezone.now() - timedelta(hours=1),
            arrival_datetime=timezone.now() + timedelta(hours=1),
        )

        response = self.client.post(
            self.passengers_url(reservation),
            self.passenger_data(
                [{'first_name': 'Late', 'last_name': 'Edit', 'national_id': passenger.national_id}],
                initial=1, ids=[passenger.pk],
            ),
        )

        self.assertEqual(response.status_code, 302)
        passenger.refresh_from_db()
        self.assertEqual(passenger.first_name, old_name)

    def test_cancelled_reservation_cannot_receive_passengers(self):
        reservation = self.book(1)
        self.client.post(self.cancel_url(reservation))

        get_response = self.client.get(self.passengers_url(reservation))
        self.assertRedirects(get_response, self.detail_url(reservation), fetch_redirect_response=False)

        post_response = self.add_passengers(reservation)
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(Passenger.objects.filter(reservation=reservation).count(), 0)

    def test_other_users_cannot_open_the_passenger_page(self):
        reservation = self.book(1)
        other = CustomUser.objects.create_user(username='other', password='pass12345')
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.passengers_url(reservation)).status_code, 404)

    # ----- one person, one seat per flight -------------------------------
    def test_same_national_id_cannot_hold_two_seats_on_one_flight(self):
        shared = self.new_national_id()
        first = self.book(1)
        self.assertEqual(self.add_passengers(first, [shared]).status_code, 302)

        second = self.book(1)
        response = self.add_passengers(second, [shared])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(second.passengers.count(), 0)

    def test_a_paid_reservation_also_blocks_the_national_id(self):
        shared = self.new_national_id()
        first = self.book(1)
        self.add_passengers(first, [shared])
        self.pay(first)

        second = self.book(1)
        response = self.add_passengers(second, [shared])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(second.passengers.count(), 0)

    def test_the_block_applies_across_different_users(self):
        shared = self.new_national_id()
        first = self.book(1)
        self.add_passengers(first, [shared])

        other = CustomUser.objects.create_user(username='other', password='pass12345')
        self.client.force_login(other)
        second = self.book(1)
        response = self.add_passengers(second, [shared])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(second.passengers.count(), 0)

    def test_cancelling_the_first_reservation_frees_the_national_id(self):
        shared = self.new_national_id()
        first = self.book(1)
        self.add_passengers(first, [shared])
        second = self.book(1)

        self.client.post(self.cancel_url(first))
        response = self.add_passengers(second, [shared])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(second.passengers.count(), 1)

    def test_an_expired_reservation_does_not_block_the_national_id(self):
        shared = self.new_national_id()
        first = self.book(1)
        self.add_passengers(first, [shared])
        self.expire_now(first)  # overdue, but nobody has swept it yet

        second = self.book(1)
        response = self.add_passengers(second, [shared])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(second.passengers.count(), 1)

    def test_the_same_national_id_is_fine_on_a_different_flight(self):
        shared = self.new_national_id()
        first = self.book(1)
        self.add_passengers(first, [shared])

        later_flight = Flight.objects.create(
            flight_number="IR401",
            route=self.flight.route,
            airline=self.flight.airline,
            airplane_type=Flight.AirplaneTypeChoices.A320,
            departure_datetime=timezone.now() + timedelta(days=5),
            arrival_datetime=timezone.now() + timedelta(days=5, hours=2),
            base_price=Decimal('1000000'),
        )
        other_class = SeatClass.objects.create(
            flight=later_flight, class_type=SeatClass.ClassTypeChoices.ECONOMY,
            capacity=6, available_seats=6,
        )
        generate_seats_for_flight(later_flight)
        seat = Seat.objects.filter(seat_class=other_class).order_by('row_number', 'column_letter').first()
        response = self.client.post(
            reverse('tickets:seat_selection', kwargs={'seat_class_id': other_class.pk}) + '?count=1',
            {'seats_count': 1, 'seat_ids': [seat.pk]},
        )
        self.assertEqual(response.status_code, 302)
        second = Reservation.objects.order_by('-pk').first()

        response = self.add_passengers(second, [shared])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(second.passengers.count(), 1)


class NationalIdTests(TicketsTestCase):
    def test_normalize_digits(self):
        self.assertEqual(normalize_digits('۰۱۲۳۴۵۶۷۸۹'), '0123456789')
        self.assertEqual(normalize_digits('٠١٢٣٤٥٦٧٨٩'), '0123456789')
        self.assertEqual(normalize_digits('12ab'), '12ab')

    def test_checksum_function(self):
        self.assertTrue(is_valid_iranian_national_id('1234567891'))
        self.assertFalse(is_valid_iranian_national_id('1234567890'))  # wrong check digit
        self.assertFalse(is_valid_iranian_national_id('1111111111'))  # all digits equal
        self.assertFalse(is_valid_iranian_national_id('12345'))
        self.assertFalse(is_valid_iranian_national_id('abcdefghij'))

    @override_settings(TICKETS_VALIDATE_NATIONAL_ID_CHECKSUM=True)
    def test_checksum_is_enforced_when_enabled(self):
        bad = PassengerForm({'first_name': 'A', 'last_name': 'B', 'national_id': '1234567890'})
        self.assertFalse(bad.is_valid())
        self.assertIn('national_id', bad.errors)

        good = PassengerForm({'first_name': 'A', 'last_name': 'B', 'national_id': '1234567891'})
        self.assertTrue(good.is_valid())

    @override_settings(TICKETS_VALIDATE_NATIONAL_ID_CHECKSUM=False)
    def test_checksum_is_not_enforced_by_default(self):
        form = PassengerForm({'first_name': 'A', 'last_name': 'B', 'national_id': '1234567890'})
        self.assertTrue(form.is_valid())

    def test_national_id_must_be_exactly_ten_digits(self):
        for value in ('123456789', '12345678901', 'abcdefghij', '1234 67890'):
            form = PassengerForm({'first_name': 'A', 'last_name': 'B', 'national_id': value})
            self.assertFalse(form.is_valid(), value)


# ======================================================================
# Expiry of unpaid reservations
# ======================================================================
class ExpiryTests(TicketsTestCase):
    def _hold(self, seat):
        create_pending_reservation(
            user=self.user, seat_class_id=self.seat_class.pk, seat_ids=[seat.pk], seats_count=1,
        )
        return Reservation.objects.order_by('-pk').first()

    def test_sweep_cancels_overdue_reservations_and_keeps_live_ones(self):
        seats = self.free_seats()
        overdue = self._hold(seats[0])
        live = self._hold(seats[1])
        self.expire_now(overdue)

        count = expire_pending_reservations()

        self.assertEqual(count, 1)
        overdue.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(overdue.status, STATUS.CANCELLED)
        self.assertEqual(overdue.cancellation_reason, REASON.TIMEOUT)
        self.assertIsNotNone(overdue.cancelled_at)
        self.assertEqual(live.status, STATUS.PENDING_PAYMENT)
        seats[0].refresh_from_db()
        seats[1].refresh_from_db()
        self.assertTrue(seats[0].is_available)
        self.assertFalse(seats[1].is_available)
        self.assertEqual(self.refresh_seat_class(), 5)

    def test_a_pending_reservation_without_a_deadline_is_expired_too(self):
        Reservation.objects.create(
            user=self.user, seat_class=self.seat_class, seats_count=1,
            total_paid_price=Decimal('1000000.00'), status=STATUS.PENDING_PAYMENT,
        )
        self.assertEqual(expire_pending_reservations(), 1)

    def test_sweep_never_touches_paid_reservations(self):
        reservation = self.book_and_pay()
        Reservation.objects.filter(pk=reservation.pk).update(
            payment_expires_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(expire_pending_reservations(), 0)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.RESERVED)

    def test_management_command_expires_overdue_reservations(self):
        reservation = self._hold(self.free_seats()[0])
        self.expire_now(reservation)

        out = StringIO()
        call_command('expire_reservations', stdout=out)

        self.assertIn('1 reservation(s) expired', out.getvalue())
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)

    def test_detail_page_expires_an_overdue_reservation(self):
        reservation = self.book(1)
        self.expire_now(reservation)

        self.client.get(self.detail_url(reservation))

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)
        self.assertEqual(self.refresh_seat_class(), 6)

    def test_list_page_expires_overdue_reservations(self):
        reservation = self.book(1)
        self.expire_now(reservation)

        self.client.get(reverse('tickets:reservation_list'))

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)

    def test_payment_page_expires_an_overdue_reservation(self):
        reservation = self.book_with_passengers()
        self.expire_now(reservation)

        response = self.client.get(self.payment_url(reservation))

        self.assertRedirects(response, self.detail_url(reservation), fetch_redirect_response=False)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, STATUS.CANCELLED)

    # ----- the limit of pending reservations -----------------------------
    def test_pending_reservation_limit(self):
        seats = self.free_seats()
        for seat in seats[:MAX_PENDING_RESERVATIONS_PER_USER]:
            self._hold(seat)

        extra = seats[MAX_PENDING_RESERVATIONS_PER_USER]
        with self.assertRaises(BookingError):
            create_pending_reservation(
                user=self.user, seat_class_id=self.seat_class.pk, seat_ids=[extra.pk], seats_count=1,
            )

        extra.refresh_from_db()
        self.assertTrue(extra.is_available)
        self.assertEqual(
            Reservation.objects.filter(user=self.user).count(), MAX_PENDING_RESERVATIONS_PER_USER
        )

    def test_overdue_reservations_do_not_count_towards_the_limit(self):
        seats = self.free_seats()
        holds = [self._hold(seat) for seat in seats[:MAX_PENDING_RESERVATIONS_PER_USER]]
        self.expire_now(holds[0])  # overdue, but not swept yet

        reservation = self._hold(seats[MAX_PENDING_RESERVATIONS_PER_USER])
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)

    def test_paying_or_cancelling_frees_a_slot(self):
        seats = self.free_seats()
        holds = [self._hold(seat) for seat in seats[:MAX_PENDING_RESERVATIONS_PER_USER]]
        self.client.post(self.cancel_url(holds[0]))

        reservation = self._hold(seats[MAX_PENDING_RESERVATIONS_PER_USER])
        self.assertEqual(reservation.status, STATUS.PENDING_PAYMENT)


# ======================================================================
# Reservation list and detail pages
# ======================================================================
class ReservationPagesTests(TicketsTestCase):
    def test_detail_is_visible_only_to_the_owner(self):
        reservation = self.book(1)
        self.assertEqual(self.client.get(self.detail_url(reservation)).status_code, 200)

        other = CustomUser.objects.create_user(username='other', password='pass12345')
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.detail_url(reservation)).status_code, 404)

    def test_detail_tells_whether_passengers_can_be_edited(self):
        reservation = self.book(1)
        self.assertTrue(self.client.get(self.detail_url(reservation)).context['can_edit_passengers'])

        self.client.post(self.cancel_url(reservation))
        self.assertFalse(self.client.get(self.detail_url(reservation)).context['can_edit_passengers'])

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse('tickets:reservation_list'))
        self.assertEqual(response.status_code, 302)

    def test_list_tabs_counts_and_filters(self):
        pending = self.book(1)
        cancelled = self.book(1)
        self.client.post(self.cancel_url(cancelled))

        response = self.client.get(reverse('tickets:reservation_list'))
        counts = {tab['key']: tab['count'] for tab in response.context['filter_tabs']}
        self.assertEqual(counts, {'': 2, 'upcoming': 1, 'past': 0, 'cancelled': 1})

        def ids(filter_key):
            page = self.client.get(reverse('tickets:reservation_list'), {'filter': filter_key})
            return [r.pk for r in page.context['reservations']]

        self.assertEqual(ids('upcoming'), [pending.pk])
        self.assertEqual(ids('cancelled'), [cancelled.pk])
        self.assertEqual(ids('past'), [])
        self.assertCountEqual(ids(''), [pending.pk, cancelled.pk])

    def test_unknown_filter_falls_back_to_all(self):
        self.book(1)
        response = self.client.get(reverse('tickets:reservation_list'), {'filter': 'abc'})
        self.assertEqual(response.context['current_filter'], '')
        self.assertEqual(len(response.context['reservations']), 1)

    def test_paid_reservation_of_a_departed_flight_is_in_the_past_tab(self):
        reservation = self.book_and_pay()
        self.update_flight(
            departure_datetime=timezone.now() - timedelta(hours=3),
            arrival_datetime=timezone.now() - timedelta(hours=1),
        )
        page = self.client.get(reverse('tickets:reservation_list'), {'filter': 'past'})
        self.assertEqual([r.pk for r in page.context['reservations']], [reservation.pk])


# ======================================================================
# Small pure pieces of the business logic
# ======================================================================
class ServiceUnitTests(TicketsTestCase):
    def test_column_index(self):
        self.assertEqual(column_index('A'), 1)
        self.assertEqual(column_index('b'), 2)
        self.assertEqual(column_index('Z'), 26)
        self.assertEqual(column_index('AA'), 27)

    def test_get_penalty_percent(self):
        paid = SimpleNamespace(status=STATUS.RESERVED)
        unpaid = SimpleNamespace(status=STATUS.PENDING_PAYMENT)
        active_flight = SimpleNamespace(status=Flight.StatusChoices.SCHEDULED, cancellation_penalty_percent=20)

        self.assertEqual(get_penalty_percent(paid, active_flight), Decimal('20'))
        self.assertEqual(get_penalty_percent(unpaid, active_flight), Decimal('0'))

        cancelled_flight = SimpleNamespace(status=Flight.StatusChoices.CANCELLED, cancellation_penalty_percent=20)
        self.assertEqual(get_penalty_percent(paid, cancelled_flight), Decimal('0'))

        for raw, expected in ((150, Decimal('100')), (-5, Decimal('0')), (None, Decimal('0'))):
            flight = SimpleNamespace(status=Flight.StatusChoices.SCHEDULED, cancellation_penalty_percent=raw)
            self.assertEqual(get_penalty_percent(paid, flight), expected)

    def test_is_payment_expired(self):
        now = timezone.now()
        self.assertFalse(Reservation(status=STATUS.PENDING_PAYMENT, payment_expires_at=now + timedelta(minutes=5)).is_payment_expired)
        self.assertTrue(Reservation(status=STATUS.PENDING_PAYMENT, payment_expires_at=now - timedelta(minutes=5)).is_payment_expired)
        # no deadline at all: treated as expired so it can never hold seats forever
        self.assertTrue(Reservation(status=STATUS.PENDING_PAYMENT, payment_expires_at=None).is_payment_expired)
        # only pending reservations can expire
        self.assertFalse(Reservation(status=STATUS.RESERVED, payment_expires_at=None).is_payment_expired)
        self.assertFalse(Reservation(status=STATUS.CANCELLED, payment_expires_at=None).is_payment_expired)

    def test_has_adjacent_block(self):
        self.assertTrue(has_adjacent_block(self.seat_class, 2))

        # hold every other seat: no two free seats are left side by side
        seats = self.all_seats()
        for index in (0, 2, 4):
            create_pending_reservation(
                user=self.user, seat_class_id=self.seat_class.pk,
                seat_ids=[seats[index].pk], seats_count=1,
            )
        self.seat_class.refresh_from_db()

        self.assertFalse(has_adjacent_block(self.seat_class, 2))
        self.assertTrue(has_adjacent_block(self.seat_class, 1))


# ======================================================================
# Admin is view-only
# ======================================================================
class AdminReadOnlyTests(TestCase):
    def test_reservation_and_passenger_admins_are_view_only(self):
        for model, model_admin_class in ((Reservation, ReservationAdmin), (Passenger, PassengerAdmin)):
            model_admin = model_admin_class(model, admin.site)
            self.assertFalse(model_admin.has_add_permission(None))
            self.assertFalse(model_admin.has_change_permission(None))
            self.assertFalse(model_admin.has_delete_permission(None))

    def test_inlines_are_view_only(self):
        for inline_class in ReservationAdmin.inlines:
            inline = inline_class(Reservation, admin.site)
            self.assertFalse(inline.has_add_permission(None, None))
            self.assertFalse(inline.has_change_permission(None, None))
            self.assertFalse(inline.has_delete_permission(None, None))