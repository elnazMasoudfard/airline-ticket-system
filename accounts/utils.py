import re

from django.conf import settings

_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


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


def sanitize_for_log(value, max_length=100):
    """
    Make user-supplied text safe for a log line: control characters (newlines
    included) are replaced, so nobody can forge fake log entries, and the
    length is limited.
    """
    return _CONTROL_CHARS.sub(' ', str(value)).strip()[:max_length]
