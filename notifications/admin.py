from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Notifications are written by the system only, so the admin is view-only."""
    list_display = ['created_at', 'user', 'kind', 'title', 'is_read']
    list_filter = ['kind', 'read_at']
    search_fields = ['user__username', 'title', 'message']
    list_select_related = ['user']
    date_hierarchy = 'created_at'

    @admin.display(boolean=True, description="خوانده‌شده")
    def is_read(self, obj):
        return obj.is_read

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False