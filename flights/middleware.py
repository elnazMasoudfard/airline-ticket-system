from django.core.cache import cache
from django.utils import timezone

from .services import sync_flight_statuses

CACHE_KEY = 'flight_status_last_sync'
SYNC_INTERVAL_SECONDS = 60


class FlightStatusSyncMiddleware:
    """
    وضعیت پروازها را بر اساس زمان به‌روزرسانی می‌کند (SCHEDULED -> ACTIVE -> COMPLETED).

    چون پروژه به Celery/cron واقعی وصل نیست، این کار را به‌جای یک تسک زمان‌بندی‌شده،
    با یک middleware سبک انجام می‌دهیم که حداکثر هر ۶۰ ثانیه یک‌بار اجرا می‌شود
    (نه در هر request، تا فشار اضافه به دیتابیس وارد نشود).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        last_sync = cache.get(CACHE_KEY)
        now = timezone.now()

        if last_sync is None or (now - last_sync).total_seconds() > SYNC_INTERVAL_SECONDS:
            sync_flight_statuses()
            cache.set(CACHE_KEY, now, timeout=None)

        return self.get_response(request)