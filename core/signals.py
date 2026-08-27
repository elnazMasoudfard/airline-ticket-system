"""
گوش‌دادن به تمام اقدامات انجام‌شده در پنل ادمین جنگو (ایجاد/ویرایش/حذف)
و ثبت خودکار آن‌ها در سیستم logging پروژه.

جنگو به‌صورت پیش‌فرض هر اقدام ادمین را در مدل داخلی LogEntry ذخیره می‌کند؛
اینجا با گوش‌دادن به ذخیره‌شدن یک LogEntry جدید، همان رویداد را به فایل‌های
لاگ خودمان (general.log / security.log) هم می‌فرستیم.
"""
import logging

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger('dashboard')

ACTION_LABELS = {
    ADDITION: 'ایجاد',
    CHANGE: 'ویرایش',
    DELETION: 'حذف',
}


@receiver(post_save, sender=LogEntry)
def log_admin_panel_action(sender, instance, created, **kwargs):
    if not created:
        return

    action_label = ACTION_LABELS.get(instance.action_flag, 'نامشخص')
    model_name = instance.content_type.model if instance.content_type else 'نامشخص'

    logger.info(
        f"اقدام پنل ادمین: کاربر={instance.user.username}, عملیات={action_label}, "
        f"مدل={model_name}, شیء={instance.object_repr}"
    )

    if instance.action_flag == DELETION:
        logger.warning(
            f"حذف از پنل ادمین: کاربر={instance.user.username}, مدل={model_name}, "
            f"شیء={instance.object_repr}"
        )