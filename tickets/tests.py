from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from flights.models import Airline, Airport, Flight, Route, Seat, SeatClass
from flights.services import generate_seats_for_flight

from .models import Reservation, ReservationSeat, Passenger


class BookingFlowTests(TestCase):
    """A comprehensive test of the booking flow via views (rather than directly through the model)
    —following the exact path a real user takes in the browser:
    selecting a seat, paying via the wallet, and proceeding with cancellation and a refund.
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
        
    def test_cancelled_flight_cannot_be_booked_from_reservation_create(self):
        self.flight.status = Flight.StatusChoices.CANCELLED
        self.flight.save(update_fields=['status'])

        response = self.client.post(
            reverse(
                'tickets:reservation_create',
                kwargs={'seat_class_id': self.seat_class.pk}
            ),
            {'seats_count': 1},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 0)

    def test_past_scheduled_flight_cannot_be_booked_from_reservation_create(self):
        self.flight.departure_datetime = timezone.now() - timedelta(hours=1)
        self.flight.save(update_fields=['departure_datetime'])

        response = self.client.post(
            reverse(
                'tickets:reservation_create',
                kwargs={'seat_class_id': self.seat_class.pk}
            ),
            {'seats_count': 1},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 0)


    def test_cancelled_flight_cannot_be_booked_from_seat_selection(self):
        self.flight.status = Flight.StatusChoices.CANCELLED
        self.flight.save(update_fields=['status'])

        seat = self._seats()[0]
        initial_balance = self.user.wallet_balance

        response = self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + '?count=1',
            {
                'seats_count': 1,
                'seat_ids': [seat.pk],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 0)

        seat.refresh_from_db()
        self.assertTrue(seat.is_available)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 6)

        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, initial_balance)


    def test_past_scheduled_flight_cannot_be_booked_from_seat_selection(self):
        self.flight.departure_datetime = timezone.now() - timedelta(hours=1)
        self.flight.save(update_fields=['departure_datetime'])

        seat = self._seats()[0]
        initial_balance = self.user.wallet_balance

        response = self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + '?count=1',
            {
                'seats_count': 1,
                'seat_ids': [seat.pk],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 0)

        seat.refresh_from_db()
        self.assertTrue(seat.is_available)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 6)

        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, initial_balance)
    
    
    def test_reservation_cannot_be_cancelled_after_flight_starts(self):
        reservation = self._book_seats(count=1)
        seat = reservation.reservation_seats.first().seat

        self.flight.status = Flight.StatusChoices.ACTIVE
        self.flight.save(update_fields=['status'])

        self.user.refresh_from_db()
        initial_balance = self.user.wallet_balance

        response = self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        self.assertEqual(response.status_code, 302)

        reservation.refresh_from_db()
        self.assertEqual(
            reservation.status,
            Reservation.StatusChoices.RESERVED
        )

        seat.refresh_from_db()
        self.assertFalse(seat.is_available)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 5)

        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, initial_balance)
    
    def test_reservation_cannot_be_cancelled_after_flight_completion(self):
        reservation = self._book_seats(count=1)
        seat = reservation.reservation_seats.first().seat

        self.flight.status = Flight.StatusChoices.COMPLETED
        self.flight.save(update_fields=['status'])

        self.user.refresh_from_db()
        initial_balance = self.user.wallet_balance

        response = self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        self.assertEqual(response.status_code, 302)

        reservation.refresh_from_db()
        self.assertEqual(
            reservation.status,
            Reservation.StatusChoices.RESERVED
        )

        seat.refresh_from_db()
        self.assertFalse(seat.is_available)

        self.seat_class.refresh_from_db()
        self.assertEqual(self.seat_class.available_seats, 5)

        self.user.refresh_from_db()
        self.assertEqual(self.user.wallet_balance, initial_balance)
    
    
    def test_reservation_can_be_cancelled_when_flight_is_cancelled(self):
        reservation = self._book_seats(count=1)

        self.flight.status = Flight.StatusChoices.CANCELLED
        self.flight.save(update_fields=['status'])

        self.user.refresh_from_db()
        initial_balance = self.user.wallet_balance

        response = self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        self.assertEqual(response.status_code, 302)

        reservation.refresh_from_db()
        self.assertEqual(
            reservation.status,
            Reservation.StatusChoices.CANCELLED
        )

        expected_refund = (
            reservation.total_paid_price * Decimal('0.8')
        ).quantize(Decimal('0.01'))

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.wallet_balance,
            initial_balance + expected_refund
        )
    
    def _seats(self):
        return list(
            Seat.objects.filter(seat_class=self.seat_class).order_by('row_number', 'column_letter')
        )

    def test_booking_two_adjacent_seats_succeeds_and_debits_wallet(self):
        seats = self._seats()[:2]  # Both in row 1, columns A and B – side by side

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
        non_adjacent = [seats[0], seats[2]]  # Columns A and C – not adjacent

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
        
    def test_cancelled_seat_can_be_booked_again(self):
        seat = self._seats()[0]

        # First booking
        self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + '?count=1',
            {
                'seats_count': 1,
                'seat_ids': [seat.pk],
            },
        )

        reservation = Reservation.objects.first()

        # Cancel the reservation
        self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        # The old ReservationSeat link must be deleted.
        self.assertFalse(
            ReservationSeat.objects.filter(seat=seat).exists()
        )

        # The physical seat must be available again.
        seat.refresh_from_db()
        self.assertTrue(seat.is_available)

        # Book the SAME seat again.
        response = self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + '?count=1',
            {
                'seats_count': 1,
                'seat_ids': [seat.pk],
            },
        )

        self.assertEqual(response.status_code, 302)
        # There should now be two Reservation objects:
        # the old cancelled one + the new reservation.
        self.assertEqual(Reservation.objects.count(), 2)

        new_reservation = (
            Reservation.objects
            .exclude(pk=reservation.pk)
            .first()
        )

        self.assertIsNotNone(new_reservation)
        self.assertEqual(
            new_reservation.status,
            Reservation.StatusChoices.RESERVED
        )

        self.assertTrue(
            ReservationSeat.objects.filter(
                reservation=new_reservation,
                seat=seat,
            ).exists()
        )
        
        
    def test_100_percent_cancellation_penalty_does_not_deposit_zero(self):
        seat = self._seats()[0]

        # Make cancellation penalty 100%.
        self.flight.cancellation_penalty_percent = 100
        self.flight.save(update_fields=['cancellation_penalty_percent'])

        self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + '?count=1',
            {
                'seats_count': 1,
                'seat_ids': [seat.pk],
            },
        )

        reservation = Reservation.objects.first()

        self.user.refresh_from_db()
        balance_after_booking = self.user.wallet_balance

        # This must NOT raise ValueError because refund is zero.
        response = self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        self.assertEqual(response.status_code, 302)

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.StatusChoices.CANCELLED
        )

        self.assertEqual(
            reservation.refund_amount,
            Decimal('0.00')
        )

        self.user.refresh_from_db()

        # No money should be returned.
        self.assertEqual(
            self.user.wallet_balance,
            balance_after_booking
        )
        
    def test_cancelling_already_cancelled_reservation_does_not_refund_again(self):
        seat = self._seats()[0]

        self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + '?count=1',
            {
                'seats_count': 1,
                'seat_ids': [seat.pk],
            },
        )

        reservation = Reservation.objects.first()

        # First cancellation
        self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        self.user.refresh_from_db()
        balance_after_first_cancel = self.user.wallet_balance

        # Second cancellation
        response = self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference]
            )
        )

        self.assertEqual(response.status_code, 302)

        self.user.refresh_from_db()

        # Wallet must NOT receive another refund.
        self.assertEqual(
            self.user.wallet_balance,
            balance_after_first_cancel
        )

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.StatusChoices.CANCELLED
        )
        

    def _book_seats(self, count=1):
        seats = self._seats()[:count]

        response = self.client.post(
            reverse(
                'tickets:seat_selection',
                kwargs={'seat_class_id': self.seat_class.pk}
            ) + f'?count={count}',
            {
                'seats_count': count,
                'seat_ids': [seat.pk for seat in seats],
            },
        )

        self.assertEqual(response.status_code, 302)
        return Reservation.objects.latest('created_at')


    def _passenger_post_data(self, passengers):
        data = {
            'form-TOTAL_FORMS': str(len(passengers)),
            'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': str(len(passengers)),
            'form-MAX_NUM_FORMS': str(len(passengers)),
        }

        for index, passenger in enumerate(passengers):
            data[f'form-{index}-first_name'] = passenger['first_name']
            data[f'form-{index}-last_name'] = passenger['last_name']
            data[f'form-{index}-national_id'] = passenger['national_id']

        return data

    def test_passenger_form_requires_all_passengers(self):
        reservation = self._book_seats(count=2)

        # Information for only one of the two passengers is being submitted.
        response = self.client.post(
            reverse(
                'tickets:add_passengers',
                kwargs={'booking_reference': reservation.booking_reference},
            ),
            self._passenger_post_data([
                {
                    'first_name': 'Ali',
                    'last_name': 'Ahmadi',
                    'national_id': '1234567890',
                },
                {
                    'first_name': '',
                    'last_name': '',
                    'national_id': '',
                },
            ]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Passenger.objects.filter(reservation=reservation).count(),
            0,
        )


    def test_duplicate_national_ids_are_rejected_without_partial_save(self):
        reservation = self._book_seats(count=2)

        response = self.client.post(
            reverse(
                'tickets:add_passengers',
                kwargs={'booking_reference': reservation.booking_reference},
            ),
            self._passenger_post_data([
                {
                    'first_name': 'Ali',
                    'last_name': 'Ahmadi',
                    'national_id': '1234567890',
                },
                {
                    'first_name': 'Reza',
                    'last_name': 'Ahmadi',
                    'national_id': '1234567890',
                },
            ]),
        )

        self.assertEqual(response.status_code, 200)

        # None of the passengers should be saved.
        self.assertEqual(
            Passenger.objects.filter(reservation=reservation).count(),
            0,
        )


    def test_repeated_passenger_submission_does_not_create_duplicates(self):
        reservation = self._book_seats(count=1)

        url = reverse(
            'tickets:add_passengers',
            kwargs={'booking_reference': reservation.booking_reference},
        )

        data = self._passenger_post_data([
            {
                'first_name': 'Ali',
                'last_name': 'Ahmadi',
                'national_id': '1234567890',
            },
        ])

        first_response = self.client.post(url, data)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(
            Passenger.objects.filter(reservation=reservation).count(),
            1,
        )

    
        second_response = self.client.post(url, data)

        self.assertEqual(second_response.status_code, 302)

        # A second passenger must not be created.
        self.assertEqual(
            Passenger.objects.filter(reservation=reservation).count(),
            1,
        )


    def test_cancelled_reservation_cannot_add_passengers(self):
        reservation = self._book_seats(count=1)

        self.client.post(
            reverse(
                'tickets:reservation_cancel',
                args=[reservation.booking_reference],
            )
        )

        response = self.client.get(
            reverse(
                'tickets:add_passengers',
                kwargs={'booking_reference': reservation.booking_reference},
            )
        )

        self.assertEqual(response.status_code, 404)

        self.assertEqual(
            Passenger.objects.filter(reservation=reservation).count(),
            0,
        )