from django.conf import settings

# Kept importable from here: several views already do `from .utils import sanitize_for_log`.
from core.utils import sanitize_for_log  # noqa: F401


def get_client_ip(request):
    """
    IP address of the client, for the security log.

    X-Forwarded-For is only trusted when settings.TRUST_X_FORWARDED_FOR is True
    (i.e. the site really runs behind a reverse proxy you control), because
    otherwise any client could fake it and poison the log.
    """
    if getattr(settings, 'TRUST_X_FORWARDED_FOR', False):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or '-'