import logging
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless

from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry, LogEntryManager
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.http import QueryDict
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from accounts import utils as accounts_utils
from accounts.models import CustomUser, WalletTransaction
from flights.models import Airport, Flight
from tickets.models import Reservation

from .models import TimeStampedModel
from .templatetags.badges import (
    FLIGHT_BADGE_CLASSES,
    RESERVATION_BADGE_CLASSES,
    flight_status_badge,
    reservation_status_badge,
)
from .templatetags.ui import page_items, query_string
from .utils import sanitize_for_log

CSS_PATH = Path(settings.BASE_DIR) / 'static' / 'css' / 'main.css'


def make_airport(code='AAA', city='Tehran'):
    return Airport.objects.create(name='Airport', city=city, iata_code=code)


# ======================================================================
# TimeStampedModel
# ======================================================================
class TimeStampedModelTests(TestCase):

    def test_it_is_abstract_so_it_has_no_table_of_its_own(self):
        self.assertTrue(TimeStampedModel._meta.abstract)

    def test_the_models_of_the_project_inherit_it(self):
        for model in (Airport, Flight, Reservation, WalletTransaction):
            self.assertTrue(issubclass(model, TimeStampedModel), model)

    def test_both_timestamps_are_set_on_creation(self):
        airport = make_airport()
        self.assertIsNotNone(airport.created_at)
        self.assertIsNotNone(airport.updated_at)

    def test_save_updates_updated_at_but_not_created_at(self):
        airport = make_airport()
        created, updated = airport.created_at, airport.updated_at

        airport.city = 'Mashhad'
        airport.save()
        airport.refresh_from_db()

        self.assertEqual(airport.created_at, created)
        self.assertGreater(airport.updated_at, updated)

    def test_queryset_update_does_not_touch_updated_at(self):
        # Documents Django's behaviour: this is why code that uses .update() sets
        # updated_at by hand when the value matters.
        airport = make_airport()
        updated = airport.updated_at

        Airport.objects.filter(pk=airport.pk).update(city='Mashhad')
        airport.refresh_from_db()

        self.assertEqual(airport.updated_at, updated)


# ======================================================================
# sanitize_for_log
# ======================================================================
class SanitizeForLogTests(SimpleTestCase):

    def test_control_characters_are_replaced(self):
        cleaned = sanitize_for_log("name\nINFO | forged line\r\tend")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("\t", cleaned)
        self.assertIn("forged line", cleaned)

    def test_text_is_stripped_and_limited(self):
        self.assertEqual(sanitize_for_log("  hello  "), "hello")
        self.assertEqual(len(sanitize_for_log("x" * 500)), 100)
        self.assertEqual(len(sanitize_for_log("x" * 500, max_length=20)), 20)

    def test_non_strings_are_accepted(self):
        self.assertEqual(sanitize_for_log(12345), "12345")
        self.assertEqual(sanitize_for_log(None), "None")

    def test_accounts_still_exposes_the_same_function(self):
        self.assertIs(accounts_utils.sanitize_for_log, sanitize_for_log)


# ======================================================================
# Status badges
# ======================================================================
class BadgeMappingTests(SimpleTestCase):

    def test_every_reservation_status_has_a_colour(self):
        for value in Reservation.StatusChoices.values:
            self.assertIn(value, RESERVATION_BADGE_CLASSES, value)

    def test_every_flight_status_has_a_colour(self):
        for value in Flight.StatusChoices.values:
            self.assertIn(value, FLIGHT_BADGE_CLASSES, value)

    def test_pending_payment_stands_out_from_final_and_cancelled(self):
        classes = {
            RESERVATION_BADGE_CLASSES['pending_payment'],
            RESERVATION_BADGE_CLASSES['reserved'],
            RESERVATION_BADGE_CLASSES['cancelled'],
        }
        self.assertEqual(len(classes), 3)

    def test_an_active_flight_looks_different_from_a_bookable_one(self):
        self.assertNotEqual(FLIGHT_BADGE_CLASSES['active'], FLIGHT_BADGE_CLASSES['scheduled'])

    @skipUnless(CSS_PATH.exists(), "static/css/main.css not found")
    def test_every_badge_class_exists_in_the_stylesheet(self):
        css = CSS_PATH.read_text(encoding='utf-8')
        used = set(RESERVATION_BADGE_CLASSES.values()) | set(FLIGHT_BADGE_CLASSES.values()) | {'badge-muted'}
        for css_class in used:
            self.assertRegex(css, r'\.' + re.escape(css_class) + r'\b', css_class)


class BadgeTagTests(SimpleTestCase):

    def test_tags_return_the_class_and_the_label(self):
        reservation = SimpleNamespace(status='pending_payment', get_status_display=lambda: 'در انتظار پرداخت')
        self.assertEqual(
            reservation_status_badge(reservation),
            {'css_class': 'badge-warning', 'label': 'در انتظار پرداخت'},
        )
        flight = SimpleNamespace(status='active', get_status_display=lambda: 'در حال انجام')
        self.assertEqual(
            flight_status_badge(flight),
            {'css_class': 'badge-info', 'label': 'در حال انجام'},
        )

    def test_an_unknown_status_falls_back_to_muted(self):
        unknown = SimpleNamespace(status='something_new', get_status_display=lambda: 'x')
        self.assertEqual(reservation_status_badge(unknown)['css_class'], 'badge-muted')
        self.assertEqual(flight_status_badge(unknown)['css_class'], 'badge-muted')

    def test_the_tags_render_with_real_model_instances(self):
        template = Template(
            "{% load badges %}{% reservation_status_badge r %}|{% flight_status_badge f %}"
        )
        html = template.render(Context({
            'r': Reservation(status=Reservation.StatusChoices.PENDING_PAYMENT),
            'f': Flight(status=Flight.StatusChoices.CANCELLED),
        }))
        self.assertIn('badge badge-warning', html)
        self.assertIn('badge badge-danger', html)

    def test_the_label_is_escaped(self):
        template = Template("{% load badges %}{% reservation_status_badge r %}")
        reservation = SimpleNamespace(status='reserved', get_status_display=lambda: '<script>alert(1)</script>')
        html = template.render(Context({'r': reservation}))
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)


# ======================================================================
# Template tags for pagination links
# ======================================================================
class QueryStringTagTests(SimpleTestCase):

    def setUp(self):
        self.factory = RequestFactory()

    def params(self, result):
        return QueryDict(result)

    def test_other_parameters_are_kept_and_page_is_replaced(self):
        request = self.factory.get('/flights/', {'origin': '2', 'filter': 'upcoming', 'page': '1'})
        result = self.params(query_string({'request': request}, page=3))
        self.assertEqual(result['origin'], '2')
        self.assertEqual(result['filter'], 'upcoming')
        self.assertEqual(result.getlist('page'), ['3'])

    def test_an_empty_value_removes_the_parameter(self):
        request = self.factory.get('/flights/', {'origin': '2', 'page': '4'})
        result = self.params(query_string({'request': request}, page=None))
        self.assertNotIn('page', result)
        self.assertEqual(result['origin'], '2')

    def test_it_works_without_a_request(self):
        self.assertEqual(self.params(query_string({}, page=2)).getlist('page'), ['2'])


class PageItemsTagTests(SimpleTestCase):

    def page(self, total, number, per_page=10):
        return Paginator(list(range(total)), per_page).page(number)

    def test_long_ranges_are_shortened_with_gaps(self):
        items = page_items(self.page(100, 5))
        self.assertEqual([i['number'] for i in items if 'number' in i], [1, 4, 5, 6, 10])
        self.assertEqual(sum(1 for i in items if i.get('gap')), 2)
        self.assertEqual([i['number'] for i in items if i.get('current')], [5])

    def test_short_ranges_have_no_gaps(self):
        items = page_items(self.page(30, 2))
        self.assertEqual([i['number'] for i in items], [1, 2, 3])
        self.assertFalse(any(i.get('gap') for i in items))


class PaginationTemplateTests(SimpleTestCase):

    def render(self, page, query=None, is_paginated=True):
        request = RequestFactory().get('/flights/', query or {})
        return render_to_string(
            'partials/pagination.html',
            {'is_paginated': is_paginated, 'page_obj': page},
            request=request,
        )

    def test_links_keep_the_search_filters(self):
        page = Paginator(list(range(100)), 10).page(5)
        html = self.render(page, {'origin': '3', 'filter': 'upcoming', 'page': '5'})

        self.assertIn('origin=3', html)
        self.assertIn('filter=upcoming', html)
        self.assertIn('page=6', html)   # next
        self.assertIn('page=4', html)   # previous
        self.assertIn('rel="next"', html)
        self.assertIn('rel="prev"', html)

    def test_the_current_page_is_marked_for_screen_readers(self):
        page = Paginator(list(range(100)), 10).page(1)
        html = self.render(page)
        self.assertEqual(html.count('aria-current="page"'), 1)
        self.assertNotIn('rel="prev"', html)

    def test_nothing_is_rendered_when_there_is_only_one_page(self):
        page = Paginator(list(range(5)), 10).page(1)
        self.assertEqual(self.render(page, is_paginated=False).strip(), '')


# ======================================================================
# Base layout
# ======================================================================
class BaseLayoutTests(TestCase):

    def test_anonymous_visitor_sees_the_navigation_but_no_wallet(self):
        response = self.client.get(reverse('flights:flight_list'))

        self.assertContains(response, 'skip-link')
        self.assertContains(response, 'nav-toggle')
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, reverse('accounts:login'))
        self.assertNotContains(response, 'wallet-chip')

    def test_signed_in_user_sees_the_wallet_and_their_reservations(self):
        user = CustomUser.objects.create_user(username='traveler', password='pass12345')
        user.deposit(Decimal('1500000'))
        self.client.force_login(user)

        response = self.client.get(reverse('flights:flight_list'))

        self.assertContains(response, 'wallet-chip')
        self.assertContains(response, reverse('accounts:deposit'))
        self.assertContains(response, reverse('tickets:reservation_list'))

    def test_the_stylesheet_and_script_are_linked(self):
        response = self.client.get(reverse('flights:flight_list'))
        self.assertContains(response, 'css/main.css')
        self.assertContains(response, 'js/site.js')


# ======================================================================
# Logging of admin-panel actions
# ======================================================================
class AdminActionLogTests(TestCase):

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username='manager', password='pass12345', is_staff=True
        )
        self.airport = make_airport()
        self.content_type = ContentType.objects.get_for_model(Airport)

    def make_entry(self, action_flag=ADDITION, **fields):
        data = dict(
            user=self.user,
            content_type=self.content_type,
            object_id=str(self.airport.pk),
            object_repr='Tehran',
            action_flag=action_flag,
        )
        data.update(fields)
        return LogEntry.objects.create(**data)

    def test_a_new_entry_is_logged_with_user_action_model_and_object(self):
        with self.assertLogs('admin_panel', level='INFO') as captured:
            self.make_entry(ADDITION)

        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        for expected in ('manager', 'ایجاد', 'airport', 'Tehran'):
            self.assertIn(expected, message)

    def test_a_change_is_logged_as_an_edit(self):
        with self.assertLogs('admin_panel', level='INFO') as captured:
            self.make_entry(CHANGE)
        self.assertIn('ویرایش', captured.records[0].getMessage())

    def test_a_deletion_is_logged_and_also_raised_as_a_warning(self):
        with self.assertLogs('admin_panel', level='INFO') as captured:
            self.make_entry(DELETION)

        self.assertEqual(
            [record.levelno for record in captured.records],
            [logging.INFO, logging.WARNING],
        )
        self.assertIn('حذف', captured.records[1].getMessage())

    def test_editing_an_existing_entry_is_not_logged_again(self):
        with self.assertLogs('admin_panel', level='INFO'):
            entry = self.make_entry(ADDITION)

        with self.assertNoLogs('admin_panel', level='INFO'):
            entry.change_message = 'edited'
            entry.save()

    def test_an_entry_without_a_content_type_does_not_crash(self):
        with self.assertLogs('admin_panel', level='INFO') as captured:
            self.make_entry(ADDITION, content_type=None, object_id=None)
        self.assertIn('نامشخص', captured.records[0].getMessage())

    def test_text_typed_by_a_person_cannot_forge_log_lines(self):
        with self.assertLogs('admin_panel', level='INFO') as captured:
            self.make_entry(ADDITION, object_repr="Iran Air\nINFO | forged line")

        message = captured.records[0].getMessage()
        self.assertNotIn('\n', message)
        self.assertIn('forged line', message)

    # ---- actions on several objects at once (Django 5.1+ uses bulk_create) ----
    @skipUnless(hasattr(LogEntryManager, 'log_actions'), "needs Django 5.1+")
    def test_an_action_on_several_objects_logs_each_of_them(self):
        second = make_airport('BBB', 'Mashhad')

        with self.assertLogs('admin_panel', level='INFO') as captured:
            LogEntry.objects.log_actions(self.user.pk, [self.airport, second], DELETION)

        levels = [record.levelno for record in captured.records]
        self.assertEqual(levels.count(logging.INFO), 2)
        self.assertEqual(levels.count(logging.WARNING), 2)

    @skipUnless(hasattr(LogEntryManager, 'log_actions'), "needs Django 5.1+")
    def test_an_action_on_one_object_is_logged_exactly_once(self):
        with self.assertLogs('admin_panel', level='INFO') as captured:
            LogEntry.objects.log_actions(self.user.pk, [self.airport], ADDITION, single_object=True)
        self.assertEqual(len(captured.records), 1)

    @skipUnless(hasattr(LogEntryManager, 'log_actions'), "needs Django 5.1+")
    def test_the_wrapper_is_installed_only_once(self):
        from . import signals

        signals._forward_bulk_log_entries()  # a second call must not wrap it again
        second = make_airport('CCC', 'Shiraz')

        with self.assertLogs('admin_panel', level='INFO') as captured:
            LogEntry.objects.log_actions(self.user.pk, [self.airport, second], ADDITION)

        self.assertEqual(len(captured.records), 2)