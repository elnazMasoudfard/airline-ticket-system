import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Notification

logger = logging.getLogger('notifications')


def notify(user, kind, title, message='', *, url='', dedupe_key=''):
    """
    Create a notification for `user`. Returns it, or None when nothing was created.

    It is written in the SAME database transaction as the event it reports (the caller is
    usually inside transaction.atomic()): when the payment, cancellation ... is rolled back,
    its notification disappears with it, and a committed event can never lose its
    notification. A savepoint around the INSERT guarantees that a notification problem can
    never break the business operation that called us, so this function never raises.

    If `dedupe_key` is given and the user already has a notification for that event,
    nothing is created.
    """
    try:
        with transaction.atomic():
            return Notification.objects.create(
                user=user,
                kind=kind,
                title=title[:120],
                message=message[:400],
                url=url[:300],
                dedupe_key=dedupe_key[:80],
            )
    except IntegrityError:
        if dedupe_key and Notification.objects.filter(user=user, dedupe_key=dedupe_key).exists():
            return None  # already notified about this event: nothing to do
        logger.exception("ثبت اعلان ناموفق بود: user=%s kind=%s", getattr(user, 'pk', user), kind)
    except Exception:
        logger.exception("ثبت اعلان ناموفق بود: user=%s kind=%s", getattr(user, 'pk', user), kind)
    return None


def unread_count(user):
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def mark_read(user, pk):
    """Mark one notification of `user` as read. True if it was unread before."""
    now = timezone.now()
    updated = Notification.objects.filter(
        pk=pk, user=user, read_at__isnull=True
    ).update(read_at=now, updated_at=now)
    return bool(updated)


def mark_all_read(user):
    """Mark every unread notification of `user` as read. Returns how many were changed."""
    now = timezone.now()
    return Notification.objects.filter(user=user, read_at__isnull=True).update(
        read_at=now, updated_at=now
    )


def delete_read_older_than(days=90):
    """Housekeeping: delete notifications that were read more than `days` days ago."""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Notification.objects.filter(read_at__isnull=False, read_at__lt=cutoff).delete()
    return deleted