"""
Monitoring all actions performed in the Django admin panel (create/edit/delete)
and automatically logging them within the project's logging system.

By default, Django stores every admin action in the internal `LogEntry` model;
here, we also send the event to a logger named `admin_panel`. This logger must be
defined in `settings.LOGGING` (outputting to console, `general.log`, and `security.log`);
deletions are logged at the `WARNING` level to ensure they are easily identifiable during security audits.

Two approaches are required because, in Django 5.1 and later, when an action is performed
on multiple objects (e.g., "delete selected"), `LogEntry` records are created using `bulk_create`,
and `bulk_create` does not trigger the `post_save` signal:
1. Single object -> `post_save` on `LogEntry` (see receiver below).
2. Multiple objects -> We wrap `LogEntryManager.log_actions` and manually log the
   created records (only when there is more than one record, to avoid
   logging a single record twice). In older versions of Django that lack
   `log_actions`, this part has no effect, and only path 1 operates.
"""
import functools
import logging

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry, LogEntryManager
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.utils import sanitize_for_log

logger = logging.getLogger('admin_panel')

ACTION_LABELS = {
    ADDITION: 'ایجاد',
    CHANGE: 'ویرایش',
    DELETION: 'حذف',
}


def log_admin_entry(entry):
    """Write one admin-panel action (a LogEntry) to the project's log."""
    action_label = ACTION_LABELS.get(entry.action_flag, 'نامشخص')
    model_name = entry.content_type.model if entry.content_type else 'نامشخص'
    username = sanitize_for_log(entry.user.username)
    # object_repr is free text typed by a person (e.g. an airline name): clean it so it
    # cannot inject fake lines into the log file.
    object_repr = sanitize_for_log(entry.object_repr, max_length=200)

    logger.info(
        f"اقدام پنل ادمین: کاربر={username}, عملیات={action_label}, "
        f"مدل={model_name}, شیء={object_repr}"
    )

    if entry.action_flag == DELETION:
        logger.warning(
            f"حذف از پنل ادمین: کاربر={username}, مدل={model_name}, "
            f"شیء={object_repr}"
        )


@receiver(post_save, sender=LogEntry)
def log_admin_panel_action(sender, instance, created, **kwargs):
    if created:
        log_admin_entry(instance)


def _forward_bulk_log_entries():
    """Make bulk-created LogEntry rows (several objects at once) reach the log too."""
    original = getattr(LogEntryManager, 'log_actions', None)
    if original is None or getattr(original, '_forwards_to_core_log', False):
        return

    @functools.wraps(original)
    def log_actions(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        entries = result if isinstance(result, (list, tuple)) else [result]
        # A single entry is saved with .save() and was already logged by post_save.
        if len(entries) > 1:
            for entry in entries:
                log_admin_entry(entry)
        return result

    log_actions._forwards_to_core_log = True
    LogEntryManager.log_actions = log_actions


_forward_bulk_log_entries()