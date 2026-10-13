import re

_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


def sanitize_for_log(value, max_length=100):
    """
    Make user-supplied text safe for a log line: control characters (newlines
    included) are replaced, so nobody can forge fake log entries, and the
    length is limited.

    Lives in `core` because every app may need it and `core` depends on nothing.
    """
    return _CONTROL_CHARS.sub(' ', str(value)).strip()[:max_length]