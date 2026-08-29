from django import template

register = template.Library()

RESERVATION_BADGE_CLASSES = {
    'reserved': 'badge-success',
    'cancelled': 'badge-danger',
}

FLIGHT_BADGE_CLASSES = {
    'scheduled': 'badge-success',
    'active': 'badge-success',
    'completed': 'badge-muted',
    'cancelled': 'badge-danger',
}


@register.inclusion_tag('partials/_status_badge.html')
def reservation_status_badge(reservation):
    """بج وضعیت رزرو (رزرو شده / کنسل شده) با رنگ مناسب."""
    css_class = RESERVATION_BADGE_CLASSES.get(reservation.status, 'badge-muted')
    return {'css_class': css_class, 'label': reservation.get_status_display()}


@register.inclusion_tag('partials/_status_badge.html')
def flight_status_badge(flight):
    """بج وضعیت پرواز (برنامه‌ریزی‌شده / در حال انجام / انجام‌شده / لغوشده) با رنگ مناسب."""
    css_class = FLIGHT_BADGE_CLASSES.get(flight.status, 'badge-muted')
    return {'css_class': css_class, 'label': flight.get_status_display()}
