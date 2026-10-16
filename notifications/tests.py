from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.core.management import call_command
from django.db import transaction
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser

from .admin import NotificationAdmin
from .models import Notification
from .services import (
    delete_read_older_than,
    mark_all_read,
    mark_read,
    notify,
    unread_count,
)
from .templatetags.notification_tags import (
    recent_notifications,
    time_ago,
    unread_notification_count,
)

KIND = Notification.Kind


def make_user(username='traveler', **fields):
    return CustomUser.objects.create_user(username=username, password='pass12345', **fields)


def make_notification(user, title='عنوان', **fields):
    defaults = dict(kind=KIND.PAYMENT_SUCCESS, message='متن', url='')
    defaults.update(fields)
    return Notification.objects.create(user=user, title=title, **defaults)


# ======================================================================
# The model
# ======================================================================
class NotificationModelTests(TestCase):

    def setUp(self):
        self.user = make_user()

    def test_every_kind_has_a_label_colour(self):
        for kind in KIND.values:
            self.assertIn(kind, Notification.BADGE_CLASSES, kind)

    def test_read_state_follows_read_at(self):
        notification = make_notification(self.user)
        self.assertFalse(notification.is_read)
        notification.read_at = timezone.now()
        self.assertTrue(notification.is_read)

    def test_an_unknown_kind_falls_back_to_a_muted_badge(self):
        self.assertEqual(Notification(kind='something_new').badge_class, 'badge-muted')

    def test_newest_notification_comes_first(self):
        first = make_notification(self.user, 'اول')
        second = make_notification(self.user, 'دوم')
        self.assertEqual(list(Notification.objects.all()), [second, first])


# ======================================================================
# notify() and the counters
# ======================================================================
class NotifyServiceTests(TestCase):

    def setUp(self):
        self.user = make_user()

    def test_notify_creates_the_notification(self):
        notification = notify(
            self.user, KIND.PAYMENT_SUCCESS, 'بلیت صادر شد', 'متن پیام', url='/tickets/ABC/'
        )
        self.assertIsNotNone(notification.pk)
        self.assertEqual(notification.user, self.user)
        self.assertEqual(notification.url, '/tickets/ABC/')
        self.assertIsNone(notification.read_at)

    def test_too_long_texts_are_cut_instead_of_failing(self):
        notification = notify(self.user, KIND.REFUND, 'x' * 300, 'y' * 1000, url='/' + 'z' * 500)
        self.assertEqual(len(notification.title), 120)
        self.assertEqual(len(notification.message), 400)
        self.assertEqual(len(notification.url), 300)

    def test_the_same_event_notifies_a_user_only_once(self):
        first = notify(self.user, KIND.PAYMENT_SUCCESS, 'a', dedupe_key='paid:ABC')
        second = notify(self.user, KIND.PAYMENT_SUCCESS, 'a', dedupe_key='paid:ABC')

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(Notification.objects.count(), 1)

    def test_the_same_key_for_another_user_is_a_different_event(self):
        other = make_user('other')
        notify(self.user, KIND.PAYMENT_SUCCESS, 'a', dedupe_key='paid:ABC')
        self.assertIsNotNone(notify(other, KIND.PAYMENT_SUCCESS, 'a', dedupe_key='paid:ABC'))
        self.assertEqual(Notification.objects.count(), 2)

    def test_without_a_key_there_is_no_duplicate_protection(self):
        notify(self.user, KIND.WALLET_CHARGED, 'a')
        notify(self.user, KIND.WALLET_CHARGED, 'a')
        self.assertEqual(Notification.objects.count(), 2)

    def test_a_failure_never_raises_and_is_logged(self):
        with patch.object(Notification.objects, 'create', side_effect=RuntimeError('boom')):
            with self.assertLogs('notifications', level='ERROR'):
                result = notify(self.user, KIND.PAYMENT_SUCCESS, 'a')

        self.assertIsNone(result)
        # the surrounding transaction is still usable
        self.assertEqual(Notification.objects.count(), 0)

    def test_the_notification_is_rolled_back_with_the_event(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                notify(self.user, KIND.PAYMENT_SUCCESS, 'a')
                raise RuntimeError('the payment failed after all')

        self.assertEqual(Notification.objects.count(), 0)


class ReadStateServiceTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.other = make_user('other')

    def test_unread_count_only_counts_my_unread_notifications(self):
        make_notification(self.user)
        make_notification(self.user, read_at=timezone.now())
        make_notification(self.other)
        self.assertEqual(unread_count(self.user), 1)

    def test_mark_read_marks_only_my_notification(self):
        mine = make_notification(self.user)
        theirs = make_notification(self.other)

        self.assertTrue(mark_read(self.user, mine.pk))
        self.assertFalse(mark_read(self.user, theirs.pk))

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertIsNotNone(mine.read_at)
        self.assertIsNone(theirs.read_at)

    def test_marking_twice_keeps_the_first_read_time(self):
        notification = make_notification(self.user)
        mark_read(self.user, notification.pk)
        notification.refresh_from_db()
        first = notification.read_at

        self.assertFalse(mark_read(self.user, notification.pk))
        notification.refresh_from_db()
        self.assertEqual(notification.read_at, first)

    def test_mark_all_read_only_touches_my_unread_ones(self):
        make_notification(self.user)
        make_notification(self.user)
        make_notification(self.user, read_at=timezone.now())
        theirs = make_notification(self.other)

        self.assertEqual(mark_all_read(self.user), 2)

        self.assertEqual(unread_count(self.user), 0)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_cleanup_deletes_only_old_read_notifications(self):
        old_read = make_notification(self.user, 'old read')
        recent_read = make_notification(self.user, 'recent read')
        old_unread = make_notification(self.user, 'old unread')
        now = timezone.now()
        Notification.objects.filter(pk=old_read.pk).update(read_at=now - timedelta(days=100))
        Notification.objects.filter(pk=recent_read.pk).update(read_at=now - timedelta(days=5))
        Notification.objects.filter(pk=old_unread.pk).update(created_at=now - timedelta(days=500))

        self.assertEqual(delete_read_older_than(90), 1)

        remaining = set(Notification.objects.values_list('title', flat=True))
        self.assertEqual(remaining, {'recent read', 'old unread'})

    def test_cleanup_command(self):
        old_read = make_notification(self.user)
        Notification.objects.filter(pk=old_read.pk).update(
            read_at=timezone.now() - timedelta(days=40)
        )
        out = StringIO()

        call_command('cleanup_notifications', '--days', '30', stdout=out)

        self.assertIn('1 notification(s) deleted', out.getvalue())
        self.assertEqual(Notification.objects.count(), 0)


# ======================================================================
# Views
# ======================================================================
class NotificationListViewTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.url = reverse('notifications:list')

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_only_my_notifications_are_listed_newest_first(self):
        first = make_notification(self.user, 'قدیمی‌تر')
        second = make_notification(self.user, 'جدیدتر')
        make_notification(make_user('other'), 'مال کس دیگر')

        response = self.client.get(self.url)

        self.assertEqual(list(response.context['notifications']), [second, first])
        self.assertNotContains(response, 'مال کس دیگر')

    def test_unread_notifications_look_different_from_read_ones(self):
        make_notification(self.user, 'نخوانده')
        make_notification(self.user, 'خوانده‌شده', read_at=timezone.now())

        response = self.client.get(self.url)

        # one tinted/bold item, and its "mark as read" button
        self.assertContains(response, 'notification-unread', count=1)
        self.assertContains(response, 'خوانده شد</button>', count=1)

    def test_the_page_offers_to_mark_everything_as_read(self):
        make_notification(self.user)
        response = self.client.get(self.url)
        self.assertContains(response, reverse('notifications:mark_all_read'))
        self.assertEqual(response.context['unread_count'], 1)

    def test_no_mark_all_button_when_everything_is_read(self):
        make_notification(self.user, read_at=timezone.now())
        response = self.client.get(self.url)
        self.assertNotContains(response, reverse('notifications:mark_all_read'))

    def test_the_unread_filter(self):
        unread = make_notification(self.user, 'نخوانده')
        make_notification(self.user, 'خوانده‌شده', read_at=timezone.now())

        response = self.client.get(self.url, {'filter': 'unread'})

        self.assertEqual(list(response.context['notifications']), [unread])
        self.assertEqual(response.context['current_filter'], 'unread')

    def test_an_unknown_filter_shows_everything(self):
        make_notification(self.user)
        make_notification(self.user, read_at=timezone.now())
        response = self.client.get(self.url, {'filter': 'abc'})
        self.assertEqual(response.context['current_filter'], '')
        self.assertEqual(len(response.context['notifications']), 2)

    def test_empty_states(self):
        self.assertContains(self.client.get(self.url), 'هنوز اعلانی ندارید')
        make_notification(self.user, read_at=timezone.now())
        self.assertContains(
            self.client.get(self.url, {'filter': 'unread'}), 'همه‌ی اعلان‌ها را خوانده‌اید'
        )

    def test_the_list_is_paginated_and_pages_keep_the_filter(self):
        for index in range(20):
            make_notification(self.user, f'اعلان {index}')

        response = self.client.get(self.url, {'filter': 'unread'})

        self.assertEqual(len(response.context['notifications']), 15)
        self.assertContains(response, 'filter=unread')
        self.assertContains(response, 'page=2')


class NotificationOpenViewTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def open_url(self, notification):
        return reverse('notifications:open', kwargs={'pk': notification.pk})

    def test_opening_marks_it_as_read_and_goes_to_the_linked_page(self):
        notification = make_notification(self.user, url='/tickets/ABC12345/')

        response = self.client.get(self.open_url(notification))

        self.assertRedirects(response, '/tickets/ABC12345/', fetch_redirect_response=False)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_a_notification_without_a_link_goes_back_to_the_list(self):
        notification = make_notification(self.user, url='')
        response = self.client.get(self.open_url(notification))
        self.assertRedirects(response, reverse('notifications:list'), fetch_redirect_response=False)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_a_link_to_another_site_is_never_followed(self):
        notification = make_notification(self.user, url='https://evil.example.com/')
        response = self.client.get(self.open_url(notification))
        self.assertRedirects(response, reverse('notifications:list'), fetch_redirect_response=False)

    def test_a_protocol_relative_link_is_never_followed(self):
        notification = make_notification(self.user, url='//evil.example.com/')
        response = self.client.get(self.open_url(notification))
        self.assertRedirects(response, reverse('notifications:list'), fetch_redirect_response=False)

    def test_opening_again_keeps_the_first_read_time(self):
        notification = make_notification(self.user, url='/tickets/ABC12345/')
        self.client.get(self.open_url(notification))
        notification.refresh_from_db()
        first = notification.read_at

        self.client.get(self.open_url(notification))

        notification.refresh_from_db()
        self.assertEqual(notification.read_at, first)

    def test_nobody_can_open_someone_elses_notification(self):
        theirs = make_notification(make_user('other'), url='/tickets/ABC12345/')
        self.assertEqual(self.client.get(self.open_url(theirs)).status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_login_is_required(self):
        notification = make_notification(self.user)
        self.client.logout()
        self.assertEqual(self.client.get(self.open_url(notification)).status_code, 302)


class MarkReadViewTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_mark_one_as_read_and_return_to_the_same_page(self):
        notification = make_notification(self.user)
        url = reverse('notifications:mark_read', kwargs={'pk': notification.pk})

        response = self.client.post(url, {'next': '/accounts/profile/'})

        self.assertRedirects(response, '/accounts/profile/', fetch_redirect_response=False)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_an_outside_next_address_is_ignored(self):
        notification = make_notification(self.user)
        url = reverse('notifications:mark_read', kwargs={'pk': notification.pk})
        response = self.client.post(url, {'next': 'https://evil.example.com/'})
        self.assertRedirects(response, reverse('notifications:list'), fetch_redirect_response=False)

    def test_it_needs_post(self):
        notification = make_notification(self.user)
        url = reverse('notifications:mark_read', kwargs={'pk': notification.pk})
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_someone_elses_notification_is_not_found(self):
        theirs = make_notification(make_user('other'))
        url = reverse('notifications:mark_read', kwargs={'pk': theirs.pk})
        self.assertEqual(self.client.post(url).status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_mark_all_read(self):
        make_notification(self.user)
        make_notification(self.user)
        theirs = make_notification(make_user('other'))

        response = self.client.post(reverse('notifications:mark_all_read'))

        self.assertRedirects(response, reverse('notifications:list'), fetch_redirect_response=False)
        self.assertEqual(unread_count(self.user), 0)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_mark_all_read_needs_post_and_login(self):
        url = reverse('notifications:mark_all_read')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)


# ======================================================================
# Template tags and the item partial
# ======================================================================
class TemplateTagTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.request = RequestFactory().get('/')
        self.request.user = self.user

    def test_the_unread_counter_asks_the_database_once_per_request(self):
        make_notification(self.user)
        make_notification(self.user)
        context = {'request': self.request}

        with self.assertNumQueries(1):
            first = unread_notification_count(context)
            second = unread_notification_count(context)

        self.assertEqual((first, second), (2, 2))

    def test_a_visitor_has_no_notifications(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        self.assertEqual(unread_notification_count({'request': request}), 0)
        self.assertEqual(recent_notifications({'request': request}), [])
        self.assertEqual(unread_notification_count({}), 0)

    def test_recent_notifications_are_the_newest_ones_up_to_the_limit(self):
        for index in range(7):
            make_notification(self.user, f'n{index}')

        recent = recent_notifications({'request': self.request}, 3)

        self.assertEqual([n.title for n in recent], ['n6', 'n5', 'n4'])

    def test_time_ago(self):
        now = timezone.now()
        self.assertEqual(time_ago(now - timedelta(seconds=30)), 'همین الان')
        self.assertEqual(time_ago(now + timedelta(minutes=5)), 'همین الان')
        self.assertEqual(time_ago(now - timedelta(minutes=5, seconds=1)), '5 دقیقه پیش')
        self.assertEqual(time_ago(now - timedelta(hours=3, minutes=1)), '3 ساعت پیش')
        self.assertEqual(time_ago(now - timedelta(days=2, minutes=1)), '2 روز پیش')
        old = now - timedelta(days=10)
        self.assertEqual(time_ago(old), timezone.localtime(old).strftime('%Y/%m/%d'))
        self.assertEqual(time_ago(None), '')


class ItemPartialTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.request = RequestFactory().get('/accounts/profile/')
        self.request.user = self.user

    def render(self, notification):
        return render_to_string(
            'notifications/_notification_item.html', {'n': notification}, request=self.request
        )

    def test_an_unread_item_is_marked_and_has_a_mark_read_button(self):
        html = self.render(make_notification(self.user, 'نخوانده'))
        self.assertIn('notification-unread', html)
        self.assertIn('(خوانده‌نشده)', html)  # for screen readers
        self.assertIn('خوانده شد', html)
        self.assertIn('/accounts/profile/', html)  # "next": back to the same page

    def test_a_read_item_is_plain(self):
        html = self.render(make_notification(self.user, 'خوانده', read_at=timezone.now()))
        self.assertNotIn('notification-unread', html)
        self.assertNotIn('(خوانده‌نشده)', html)
        self.assertNotIn('خوانده شد', html)

    def test_a_linked_item_goes_through_the_open_view(self):
        notification = make_notification(self.user, url='/tickets/ABC12345/')
        html = self.render(notification)
        self.assertIn(reverse('notifications:open', kwargs={'pk': notification.pk}), html)

    def test_an_item_without_a_link_is_not_a_link(self):
        notification = make_notification(self.user, url='')
        html = self.render(notification)
        self.assertNotIn(reverse('notifications:open', kwargs={'pk': notification.pk}), html)

    def test_the_kind_is_shown_with_its_colour(self):
        html = self.render(make_notification(self.user, kind=KIND.FLIGHT_CANCELLED))
        self.assertIn('badge-danger', html)
        self.assertIn('لغو پرواز', html)


class ProfileSectionTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.request = RequestFactory().get('/accounts/profile/')
        self.request.user = self.user

    def render(self):
        return render_to_string(
            'notifications/_profile_section.html', {}, request=self.request
        )

    def test_it_lists_the_latest_notifications_and_the_unread_count(self):
        make_notification(self.user, 'اعلان اول')
        make_notification(self.user, 'اعلان دوم', read_at=timezone.now())

        html = self.render()

        self.assertIn('اعلان اول', html)
        self.assertIn('اعلان دوم', html)
        self.assertIn('1 خوانده‌نشده', html)
        self.assertIn(reverse('notifications:list'), html)

    def test_it_shows_only_five(self):
        for index in range(8):
            make_notification(self.user, f'اعلان-{index}')
        html = self.render()
        self.assertEqual(html.count('<li class="notification'), 5)

    def test_empty(self):
        self.assertIn('هنوز اعلانی ندارید', self.render())


# ======================================================================
# The bell in the navbar
# ======================================================================
class NavbarBellTests(TestCase):

    def setUp(self):
        self.user = make_user()
        self.page = reverse('flights:flight_list')

    def test_a_visitor_has_no_bell(self):
        self.assertNotContains(self.client.get(self.page), 'notification-bell')

    def test_the_bell_is_quiet_when_everything_is_read(self):
        make_notification(self.user, read_at=timezone.now())
        self.client.force_login(self.user)

        response = self.client.get(self.page)

        self.assertContains(response, 'notification-bell')
        self.assertContains(response, reverse('notifications:list'))
        self.assertNotContains(response, 'notification-badge')
        self.assertNotContains(response, 'has-unread')

    def test_the_bell_shows_how_many_are_unread(self):
        for _ in range(3):
            make_notification(self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.page)

        self.assertContains(response, 'has-unread')
        self.assertContains(response, 'notification-badge')
        self.assertContains(response, '3 مورد خوانده‌نشده')

    def test_many_unread_are_shown_as_nine_plus(self):
        for _ in range(12):
            make_notification(self.user)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.page), '9+')

    def test_the_count_drops_after_reading(self):
        notification = make_notification(self.user, url='/tickets/ABC12345/')
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.page), 'has-unread')

        self.client.get(reverse('notifications:open', kwargs={'pk': notification.pk}))

        self.assertNotContains(self.client.get(self.page), 'has-unread')


# ======================================================================
# Admin
# ======================================================================
class NotificationAdminTests(TestCase):

    def test_the_admin_is_view_only(self):
        model_admin = NotificationAdmin(Notification, admin.site)
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_the_read_flag_is_shown(self):
        user = make_user()
        read = make_notification(user, read_at=timezone.now())
        unread = make_notification(user)
        model_admin = NotificationAdmin(Notification, admin.site)
        self.assertTrue(model_admin.is_read(read))
        self.assertFalse(model_admin.is_read(unread))