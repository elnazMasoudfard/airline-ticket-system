import logging
import time

from .services import sync_flight_statuses

logger = logging.getLogger('flights')

SYNC_INTERVAL_SECONDS = 60


class FlightStatusSyncMiddleware:
    """
    Updates flight statuses based on timing (SCHEDULED -> ACTIVE -> COMPLETED).

    Since the project is not connected to a real Celery/cron setup, we handle this
    via lightweight middleware—executed at most once every 60 seconds—instead
    of a scheduled task (avoiding execution on every request to prevent
    unnecessary database load).

    The timestamp of the last execution is stored in the process's memory rather than the cache:
    - No extra queries or requests (e.g., to DatabaseCache) are required per request;
    - If multiple processes are running, each synchronizes at most once every 60 seconds,
      and since `sync_flight_statuses` is idempotent, redundant executions cause no issues.
    A synchronization error must never prevent the page from rendering.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._next_sync_at = 0.0

    def __call__(self, request):
        now = time.monotonic()

        if now >= self._next_sync_at:
            # Set first, so that concurrent requests do not all start a sync.
            self._next_sync_at = now + SYNC_INTERVAL_SECONDS
            try:
                sync_flight_statuses()
            except Exception:
                logger.exception("خطا در همگام‌سازی وضعیت پروازها (middleware)")

        return self.get_response(request)