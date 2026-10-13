from django import template

register = template.Library()

# `core` must not import the other apps (the dependency only goes one way: they import
# `core`), so the status values are written here as plain strings. The tests in
# core/tests.py check that every status of the real models has an entry in these maps and
# that every CSS class used here exists in static/css/main.css.
RESERVATION_BADGE_CLASSES = {
    'pending_payment': 'badge-warning',   # seats are held, waiting for payment
    'reserved': 'badge-success',          # paid and final
    'cancelled': 'badge-danger',
}

FLIGHT_BADGE_CLASSES = {
    'scheduled': 'badge-success',
    'active': 'badge-info',               # boarding / in the air: no longer bookable
    'completed': 'badge-muted',
    'cancelled': 'badge-danger',
}


@register.inclusion_tag('partials/_status_badge.html')
def reservation_status_badge(reservation):
    """Reservation status badge (Pending Payment / Booked / Cancelled) with the appropriate color."""
    css_class = RESERVATION_BADGE_CLASSES.get(reservation.status, 'badge-muted')
    return {'css_class': css_class, 'label': reservation.get_status_display()}


@register.inclusion_tag('partials/_status_badge.html')
def flight_status_badge(flight):
    """Flight status badge (Scheduled / In Progress / Completed / Cancelled) with the appropriate color."""
    css_class = FLIGHT_BADGE_CLASSES.get(flight.status, 'badge-muted')
    return {'css_class': css_class, 'label': flight.get_status_display()}