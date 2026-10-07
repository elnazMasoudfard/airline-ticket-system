"""
Business logic of the booking flow.

Lock order used everywhere (to avoid deadlocks):

    Reservation  ->  SeatClass  ->  Seat rows (ordered by id)  ->  User (wallet)

When a reservation does not exist yet (seat selection) the order simply starts
at SeatClass. The Flight row is only read, never locked: the SeatClass lock
already serialises every booking/cancellation of the same seat class.
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import Enum

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from flights.models import Flight, Seat, SeatClass

from .models import Reservation, ReservationSeat

logger = logging.getLogger('tickets')

# ---------------------------------------------------------------------------
# Settings (can be overridden in settings.py)
# ---------------------------------------------------------------------------
PAYMENT_WINDOW_MINUTES = getattr(settings, 'TICKETS_PAYMENT_WINDOW_MINUTES', 30)
MAX_SEATS_PER_RESERVATION = getattr(settings, 'TICKETS_MAX_SEATS_PER_RESERVATION', 9)
MAX_PENDING_RESERVATIONS_PER_USER = getattr(
    settings, 'TICKETS_MAX_PENDING_RESERVATIONS_PER_USER', 3
)


# ---------------------------------------------------------------------------
# Exceptions / results
# ---------------------------------------------------------------------------
class ReservationError(Exception):
    """Base class; str(exception) is a user-facing message."""


class BookingError(ReservationError):
    """Seats could not be reserved."""


class FlightNotBookableError(BookingError):
    pass


class AlreadyCancelledError(ReservationError):
    pass


class CancellationNotAllowedError(ReservationError):
    pass


@dataclass
class FlightCancelResult:
    refunded_count: int = 0
    refunded_total: Decimal = Decimal('0.00')
    cancelled_pending_count: int = 0
    failed: list = field(default_factory=list)   # booking references that need manual handling


class PayResult(Enum):
    PAID = 'paid'
    ALREADY_PAID = 'already_paid'
    NOT_PAYABLE = 'not_payable'
    EXPIRED = 'expired'
    PASSENGERS_INCOMPLETE = 'passengers_incomplete'
    FLIGHT_NOT_BOOKABLE = 'flight_not_bookable'
    INSUFFICIENT_BALANCE = 'insufficient_balance'


@dataclass
class CancelOutcome:
    reservation: Reservation
    refund_amount: Decimal
    penalty_percent: Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_flight_bookable(flight):
    """A flight can be booked only if it is scheduled and departs in the future."""
    return (
        flight.status == Flight.StatusChoices.SCHEDULED
        and flight.departure_datetime > timezone.now()
    )


def column_index(letter):
    """'A' -> 1, 'B' -> 2 ... 'AA' -> 27 (works for multi-letter columns)."""
    value = 0
    for ch in str(letter).strip().upper():
        value = value * 26 + (ord(ch) - ord('A') + 1)
    return value


def validate_group_seats(seats):
    """Group bookings must be in one row and next to each other."""
    if len(seats) < 2:
        return

    if len({seat.row_number for seat in seats}) != 1:
        raise BookingError(
            "برای بیش از یک نفر، صندلی‌ها باید در یک ردیف و کنار هم باشند."
        )

    columns = sorted(column_index(seat.column_letter) for seat in seats)
    if columns != list(range(columns[0], columns[0] + len(columns))):
        raise BookingError(
            "صندلی‌های انتخابی کنار هم نیستند. لطفاً صندلی‌های پیوسته انتخاب کنید."
        )


def has_adjacent_block(seat_class, count):
    """True if some row still has `count` free seats next to each other."""
    if count <= 1:
        return seat_class.available_seats >= count

    rows = {}
    free_seats = seat_class.seats.filter(is_available=True).values_list(
        'row_number', 'column_letter'
    )
    for row, column in free_seats:
        rows.setdefault(row, []).append(column_index(column))

    for columns in rows.values():
        columns.sort()
        run = 1
        for previous, current in zip(columns, columns[1:]):
            run = run + 1 if current == previous + 1 else 1
            if run >= count:
                return True
    return False


def active_pending_count(user):
    return Reservation.objects.filter(
        user=user,
        status=Reservation.StatusChoices.PENDING_PAYMENT,
        payment_expires_at__gt=timezone.now(),
    ).count()


def pending_limit_reached(user):
    return active_pending_count(user) >= MAX_PENDING_RESERVATIONS_PER_USER


def get_penalty_percent(reservation, flight):
    """
    Cancellation penalty for a reservation:
    - unpaid reservations: no penalty
    - flight cancelled by the airline: no penalty (full refund)
    - otherwise: the flight's penalty, clamped to 0..100
    """
    if reservation.status != Reservation.StatusChoices.RESERVED:
        return Decimal('0')
    if flight.status == Flight.StatusChoices.CANCELLED:
        return Decimal('0')

    percent = Decimal(str(flight.cancellation_penalty_percent or 0))
    return max(Decimal('0'), min(Decimal('100'), percent))


# ---------------------------------------------------------------------------
# Creating a pending reservation (seat selection step)
# ---------------------------------------------------------------------------
def create_pending_reservation(*, user, seat_class_id, seat_ids, seats_count):
    """
    Hold the chosen seats for PAYMENT_WINDOW_MINUTES and create a pending
    reservation. Raises BookingError (or a subclass) with a user-facing
    message; nothing is written in that case.
    """
    seat_ids = list(seat_ids)

    if not 1 <= seats_count <= MAX_SEATS_PER_RESERVATION:
        raise BookingError(
            f"تعداد صندلی باید بین ۱ تا {MAX_SEATS_PER_RESERVATION} باشد."
        )
    if len(seat_ids) != seats_count:
        raise BookingError(f"لطفاً دقیقاً {seats_count} صندلی انتخاب کنید.")
    if len(set(seat_ids)) != len(seat_ids):
        raise BookingError("یک صندلی بیش از یک‌بار انتخاب شده است.")

    with transaction.atomic():
        seat_class = SeatClass.objects.select_for_update().get(pk=seat_class_id)
        flight = seat_class.flight

        if not is_flight_bookable(flight):
            raise FlightNotBookableError("این پرواز در حال حاضر قابل رزرو نیست.")

        if pending_limit_reached(user):
            raise BookingError(
                f"شما در حال حاضر {MAX_PENDING_RESERVATIONS_PER_USER} رزرو "
                "در انتظار پرداخت دارید. ابتدا آن‌ها را پرداخت یا لغو کنید."
            )

        if seat_class.available_seats < seats_count:
            raise BookingError("ظرفیت کافی برای این تعداد صندلی وجود ندارد.")

        # Seats are always locked ordered by id, so two users choosing
        # overlapping seats in a different order can never deadlock.
        seats = list(
            Seat.objects.select_for_update()
            .filter(id__in=seat_ids, seat_class=seat_class, is_available=True)
            .order_by('id')
        )
        if len(seats) != seats_count:
            logger.warning(
                "seat conflict: user=%s seat_class=%s requested=%s",
                user.pk, seat_class.pk, seat_ids,
            )
            raise BookingError(
                "متاسفانه یک یا چند صندلی انتخابی شما در دسترس نیست "
                "(ممکن است کاربر دیگری آن را رزرو کرده باشد). لطفاً دوباره انتخاب کنید."
            )

        validate_group_seats(seats)

        now = timezone.now()

        Seat.objects.filter(id__in=[seat.id for seat in seats]).update(
            is_available=False, updated_at=now
        )
        SeatClass.objects.filter(pk=seat_class.pk).update(
            available_seats=F('available_seats') - seats_count,
            updated_at=now,
        )

        reservation = Reservation.objects.create(
            user=user,
            seat_class=seat_class,
            seats_count=seats_count,
            total_paid_price=seat_class.final_price * seats_count,
            status=Reservation.StatusChoices.PENDING_PAYMENT,
            payment_expires_at=now + timedelta(minutes=PAYMENT_WINDOW_MINUTES),
        )

        ReservationSeat.objects.bulk_create([
            ReservationSeat(reservation=reservation, seat=seat)
            for seat in seats
        ])

    logger.info(
        "reservation created: ref=%s user=%s flight=%s seats=%s total=%s seat_numbers=%s",
        reservation.booking_reference, user.pk, flight.flight_number,
        seats_count, reservation.total_paid_price,
        [seat.seat_number for seat in seats],
    )
    return reservation


# ---------------------------------------------------------------------------
# Releasing seats / cancelling (always call inside transaction.atomic()
# with the reservation row already locked)
# ---------------------------------------------------------------------------
def _cancel_locked(reservation, reason, refund_amount=Decimal('0.00')):
    now = timezone.now()

    # Lock the seat class before touching its capacity (lock order!).
    SeatClass.objects.select_for_update().get(pk=reservation.seat_class_id)

    seat_ids = list(
        reservation.reservation_seats.values_list('seat_id', flat=True)
    )
    if seat_ids:
        Seat.objects.filter(id__in=seat_ids).update(
            is_available=True, updated_at=now
        )
        SeatClass.objects.filter(pk=reservation.seat_class_id).update(
            available_seats=F('available_seats') + len(seat_ids),
            updated_at=now,
        )
        reservation.reservation_seats.all().delete()

    reservation.status = Reservation.StatusChoices.CANCELLED
    reservation.cancelled_at = now
    reservation.cancellation_reason = reason
    reservation.refund_amount = refund_amount
    reservation.save(
        update_fields=[
            'status', 'cancelled_at', 'cancellation_reason',
            'refund_amount', 'updated_at',
        ]
    )


def expire_reservation(reservation_pk):
    """Cancel one pending reservation if its payment window is over."""
    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update().filter(pk=reservation_pk).first()
        )
        # Re-check everything after taking the lock: payment/cancel may have won.
        if reservation is None or not reservation.is_payment_expired:
            return False

        _cancel_locked(reservation, Reservation.CancellationReason.TIMEOUT)

    logger.info("reservation expired: ref=%s", reservation.booking_reference)
    return True


def expire_reservation_if_needed(reservation):
    """Lazy expiry used by views so users never see stale pending reservations."""
    if reservation.is_payment_expired:
        return expire_reservation(reservation.pk)
    return False


def expire_pending_reservations():
    """
    Cancel every pending reservation whose payment time is over and release
    its seats. Run it periodically (see the `expire_reservations` command).
    """
    now = timezone.now()
    expired_ids = list(
        Reservation.objects.filter(
            Q(payment_expires_at__lte=now) | Q(payment_expires_at__isnull=True),
            status=Reservation.StatusChoices.PENDING_PAYMENT,
        ).values_list('pk', flat=True)
    )

    expired_count = 0
    for pk in expired_ids:
        if expire_reservation(pk):
            expired_count += 1
    return expired_count


def cancel_reservation(*, booking_reference, user):
    """
    Cancel a pending or paid reservation, release the seats and refund
    the wallet. Raises Reservation.DoesNotExist, AlreadyCancelledError or
    CancellationNotAllowedError.
    """
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(
            booking_reference=booking_reference, user=user
        )

        if reservation.status == Reservation.StatusChoices.CANCELLED:
            raise AlreadyCancelledError("این رزرو قبلاً کنسل شده است.")

        flight = reservation.seat_class.flight
        flight_cancelled = flight.status == Flight.StatusChoices.CANCELLED

        can_cancel = flight_cancelled or (
            flight.status == Flight.StatusChoices.SCHEDULED
            and flight.departure_datetime > timezone.now()
        )
        if not can_cancel:
            raise CancellationNotAllowedError(
                "لغو این رزرو به دلیل وضعیت یا زمان پرواز امکان‌پذیر نیست."
            )

        was_paid = reservation.status == Reservation.StatusChoices.RESERVED
        penalty_percent = get_penalty_percent(reservation, flight)

        if was_paid:
            refund_amount = (
                reservation.total_paid_price
                * (Decimal('100') - penalty_percent)
                / Decimal('100')
            ).quantize(Decimal('0.01'))
        else:
            refund_amount = Decimal('0.00')

        reason = (
            Reservation.CancellationReason.FLIGHT_CANCELLED
            if (flight_cancelled and was_paid)
            else Reservation.CancellationReason.USER
        )

        _cancel_locked(reservation, reason, refund_amount)

        if refund_amount > 0:
            # Lock the wallet row exactly like the payment does.
            wallet_user = get_user_model().objects.select_for_update().get(pk=user.pk)
            wallet_user.deposit(refund_amount)

    logger.info(
        "reservation cancelled: ref=%s user=%s flight=%s penalty=%s refund=%s",
        reservation.booking_reference, user.pk, flight.flight_number,
        penalty_percent, refund_amount,
    )
    return CancelOutcome(reservation, refund_amount, penalty_percent)


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------
def pay_reservation(*, booking_reference, user):
    """
    Finalise a pending reservation by charging the user's wallet.
    Raises Reservation.DoesNotExist; every other outcome is a PayResult.
    """
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(
            booking_reference=booking_reference, user=user
        )

        if reservation.status == Reservation.StatusChoices.RESERVED:
            return PayResult.ALREADY_PAID, reservation

        if reservation.status != Reservation.StatusChoices.PENDING_PAYMENT:
            return PayResult.NOT_PAYABLE, reservation

        if reservation.is_payment_expired:
            _cancel_locked(reservation, Reservation.CancellationReason.TIMEOUT)
            return PayResult.EXPIRED, reservation

        if reservation.passengers.count() != reservation.seats_count:
            return PayResult.PASSENGERS_INCOMPLETE, reservation

        if not is_flight_bookable(reservation.seat_class.flight):
            return PayResult.FLIGHT_NOT_BOOKABLE, reservation

        wallet_user = get_user_model().objects.select_for_update().get(pk=user.pk)

        try:
            # Savepoint: a failed withdraw can never leave a half-done change.
            with transaction.atomic():
                wallet_user.withdraw(reservation.total_paid_price)
        except ValueError:
            logger.warning(
                "payment failed (insufficient balance): ref=%s user=%s",
                reservation.booking_reference, user.pk,
            )
            return PayResult.INSUFFICIENT_BALANCE, reservation

        reservation.status = Reservation.StatusChoices.RESERVED
        reservation.paid_at = timezone.now()
        reservation.save(update_fields=['status', 'paid_at', 'updated_at'])

    logger.info(
        "reservation paid: ref=%s user=%s amount=%s",
        reservation.booking_reference, user.pk, reservation.total_paid_price,
    )
    return PayResult.PAID, reservation


# ---------------------------------------------------------------------------
# Cancelling a whole flight (manager action)
# ---------------------------------------------------------------------------
def _cancel_reservation_for_cancelled_flight(reservation_pk):
    """
    Cancel ONE reservation because its flight was cancelled by the airline.
    Paid reservations get a 100% refund, unpaid (pending) ones are just released.
    Returns (was_paid, refund_amount) or None when there was nothing to do.
    """
    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update().filter(pk=reservation_pk).first()
        )
        if reservation is None or reservation.status == Reservation.StatusChoices.CANCELLED:
            return None

        was_paid = reservation.status == Reservation.StatusChoices.RESERVED
        refund_amount = reservation.total_paid_price if was_paid else Decimal('0.00')

        _cancel_locked(
            reservation,
            Reservation.CancellationReason.FLIGHT_CANCELLED,
            refund_amount,
        )

        if refund_amount > 0:
            wallet_user = get_user_model().objects.select_for_update().get(
                pk=reservation.user_id
            )
            wallet_user.deposit(refund_amount)

    return was_paid, refund_amount


def cancel_flight_and_refund(flight_pk):
    """
    Mark a flight as CANCELLED and cancel every reservation of it:
    - paid reservations: full refund to the user's wallet
    - pending reservations: released, nothing to refund

    Safe to call again (idempotent) and also when the status was already set to
    CANCELLED by hand: it then only sweeps the reservations that are left.
    Each reservation is processed in its own transaction, so one failure never
    blocks the others; failed booking references are returned in `result.failed`.

    Raises ReservationError when the flight has already been completed.
    """
    with transaction.atomic():
        flight = Flight.objects.select_for_update().get(pk=flight_pk)

        if flight.status == Flight.StatusChoices.COMPLETED:
            raise ReservationError("این پرواز انجام شده است و قابل لغو نیست.")

        if flight.status != Flight.StatusChoices.CANCELLED:
            flight.status = Flight.StatusChoices.CANCELLED
            flight.save(update_fields=['status'])

    result = FlightCancelResult()
    failed = {}

    # A few passes: a booking that was being created at the very moment the
    # status changed is caught by the next pass.
    for _ in range(3):
        rows = list(
            Reservation.objects
            .filter(
                seat_class__flight_id=flight_pk,
                status__in=[
                    Reservation.StatusChoices.PENDING_PAYMENT,
                    Reservation.StatusChoices.RESERVED,
                ],
            )
            .exclude(pk__in=list(failed))
            .values_list('pk', 'booking_reference')
        )
        if not rows:
            break

        for pk, reference in rows:
            try:
                outcome = _cancel_reservation_for_cancelled_flight(pk)
            except Exception:
                logger.exception(
                    "flight cancel: failed to cancel reservation ref=%s flight=%s",
                    reference, flight_pk,
                )
                failed[pk] = reference
                continue

            if outcome is None:
                continue

            was_paid, refund_amount = outcome
            if was_paid:
                result.refunded_count += 1
                result.refunded_total += refund_amount
            else:
                result.cancelled_pending_count += 1

    result.failed = list(failed.values())

    logger.info(
        "flight cancelled: flight=%s refunded=%s total=%s pending_released=%s failed=%s",
        flight_pk, result.refunded_count, result.refunded_total,
        result.cancelled_pending_count, result.failed,
    )
    return result