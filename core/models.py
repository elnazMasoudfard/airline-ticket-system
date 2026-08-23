from django.db import models


class TimeStampedModel(models.Model):
    """مدل پایه انتزاعی برای ثبت خودکار زمان ایجاد و آخرین ویرایش"""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="تاریخ ایجاد")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="آخرین ویرایش")

    class Meta:
        abstract = True