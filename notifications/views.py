from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import ListView, View

from .models import Notification
from .services import mark_all_read, mark_read, unread_count


def safe_redirect_target(request, target, default='notifications:list'):
    """`target` only when it points to this site; otherwise the notification list."""
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(target)
    return redirect(default)


class NotificationListView(LoginRequiredMixin, ListView):
    """All notifications of the signed-in user, newest first (?filter=unread for unread only)."""
    template_name = 'notifications/notification_list.html'
    context_object_name = 'notifications'
    paginate_by = 15

    def get_current_filter(self):
        return 'unread' if self.request.GET.get('filter') == 'unread' else ''

    def get_queryset(self):
        queryset = Notification.objects.filter(user=self.request.user)
        if self.get_current_filter() == 'unread':
            queryset = queryset.filter(read_at__isnull=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['unread_count'] = unread_count(self.request.user)
        context['current_filter'] = self.get_current_filter()
        return context


class NotificationOpenView(LoginRequiredMixin, View):
    """
    Opening a notification marks it as read and goes to the page it points to (a reservation,
    a flight ...). Only the owner can open it.
    """

    def get(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        mark_read(request.user, notification.pk)
        return safe_redirect_target(request, notification.url)


class NotificationMarkReadView(LoginRequiredMixin, View):
    """Mark one notification as read and come back to the page the button was on."""

    def post(self, request, pk):
        get_object_or_404(Notification, pk=pk, user=request.user)
        mark_read(request.user, pk)
        return safe_redirect_target(request, request.POST.get('next'))


class NotificationMarkAllReadView(LoginRequiredMixin, View):
    def post(self, request):
        mark_all_read(request.user)
        return safe_redirect_target(request, request.POST.get('next'))