from django import template
from django.utils import timezone

from ..models import Notification
from ..services import unread_count

register = template.Library()


@register.simple_tag(takes_context=True)
def unread_notification_count(context):
    """
    The number of unread notifications of the signed-in user (0 for visitors). One query per
    request, even if the template asks several times (the navbar and the profile section).
    """
    request = context.get('request')
    user = getattr(request, 'user', None)
    if request is None or user is None or not user.is_authenticated:
        return 0

    cached = getattr(request, '_unread_notification_count', None)
    if cached is None:
        cached = unread_count(user)
        request._unread_notification_count = cached
    return cached


@register.simple_tag(takes_context=True)
def recent_notifications(context, limit=5):
    """The newest notifications of the signed-in user, for a small box on a page."""
    request = context.get('request')
    user = getattr(request, 'user', None)
    if request is None or user is None or not user.is_authenticated:
        return []
    return list(Notification.objects.filter(user=user)[:limit])


@register.filter
def time_ago(value):
    """'همین الان' / '5 دقیقه پیش' / '3 ساعت پیش' / '2 روز پیش' / the date after a week."""
    if not value:
        return ''
    seconds = int((timezone.now() - value).total_seconds())

    if seconds < 60:
        return 'همین الان'
    if seconds < 3600:
        return f'{seconds // 60} دقیقه پیش'
    if seconds < 86400:
        return f'{seconds // 3600} ساعت پیش'
    if seconds < 7 * 86400:
        return f'{seconds // 86400} روز پیش'
    return timezone.localtime(value).strftime('%Y/%m/%d')