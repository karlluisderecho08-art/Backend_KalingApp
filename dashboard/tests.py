from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from milkbank.models import Facility, MilkBankRequest

from .views import TREND_MONTHS, _month_start, _trend_window


def make_facility(**overrides):
    defaults = dict(
        name="Test Facility",
        type=Facility.FacilityType.HOSPITAL_DEPOT,
        contact="000-0000",
        address="Somewhere",
        is_operational=True,
        capacity=10,
        latitude=14.6,
        longitude=121.0,
    )
    defaults.update(overrides)
    return Facility.objects.create(**defaults)


class TrendWindowTests(APITestCase):
    def test_window_is_twelve_months_ending_this_month(self):
        window = _trend_window()
        self.assertEqual(len(window), TREND_MONTHS)
        self.assertEqual(window[-1], _month_start(timezone.now()))
        self.assertEqual(window, sorted(window))

    def test_month_labels_are_unique(self):
        # The charts use "%b" with no year, so a window that repeated a
        # month name would collapse two months onto one point.
        labels = [start.strftime("%b") for start in _trend_window()]
        self.assertEqual(len(set(labels)), TREND_MONTHS)


class DashboardStatsViewTests(APITestCase):
    def setUp(self):
        self.url = reverse("admin-dashboard-stats")
        self.facility = make_facility()
        self.admin = User.objects.create_superuser(email="admin@example.com", password="pw12345!")
        self.mother = User.objects.create_user(email="mother@example.com", password="pw12345!")

    def book(self, months_ago, *, request_type=MilkBankRequest.RequestType.DONOR, status=None):
        booking = MilkBankRequest.objects.create(
            owner=self.mother,
            request_type=request_type,
            allocated_facility=self.facility,
            preferred_date="2026-12-01",
            preferred_time="10:00 AM",
            **({"current_sub_status": status} if status else {}),
        )
        # submitted_at is auto_now_add, so backdate it after the insert.
        target = _trend_window()[TREND_MONTHS - 1 - months_ago] + timedelta(days=2)
        MilkBankRequest.objects.filter(pk=booking.pk).update(submitted_at=target)
        return booking

    def get_stats(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_requires_admin(self):
        self.client.force_authenticate(self.mother)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_trend_is_zero_filled_across_the_whole_window(self):
        self.book(months_ago=0)
        self.book(months_ago=3)
        trend = self.get_stats()["booking_trend"]

        self.assertEqual(len(trend), TREND_MONTHS)
        self.assertEqual([row["month"] for row in trend], [s.strftime("%b") for s in _trend_window()])
        # Empty months are present as real zero points, not skipped.
        self.assertEqual(trend[-1]["count"], 1)
        self.assertEqual(trend[-4]["count"], 1)
        self.assertEqual(sum(row["count"] for row in trend), 2)
        self.assertEqual([row["count"] for row in trend[-3:-1]], [0, 0])

    def test_trend_months_are_distinct_keys(self):
        # The Statistics page renders this list with month as the React
        # key, so a duplicate label would be a duplicate key.
        months = [row["month"] for row in self.get_stats()["booking_trend"]]
        self.assertEqual(len(set(months)), len(months))

    def test_bookings_older_than_the_window_are_excluded_from_the_trend(self):
        booking = self.book(months_ago=0)
        old = _trend_window()[0] - timedelta(days=40)
        MilkBankRequest.objects.filter(pk=booking.pk).update(submitted_at=old)

        stats = self.get_stats()
        self.assertEqual(sum(row["count"] for row in stats["booking_trend"]), 0)
        # ...but it still counts toward the all-time total shown alongside.
        self.assertEqual(stats["total_bookings"], 1)

    def test_status_summaries_use_human_labels_the_charts_colour_by(self):
        self.book(months_ago=0, status=MilkBankRequest.Status.PENDING)
        self.book(months_ago=1, status=MilkBankRequest.Status.COMPLETED)
        self.book(
            months_ago=0,
            request_type=MilkBankRequest.RequestType.RECIPIENT,
            status=MilkBankRequest.Status.SCHEDULED,
        )
        stats = self.get_stats()

        self.assertEqual(
            {row["status"]: row["count"] for row in stats["donor_status_summary"]},
            {"Pending": 1, "Completed": 1},
        )
        self.assertEqual(
            {row["status"]: row["count"] for row in stats["recipient_status_summary"]},
            {"Scheduled": 1},
        )
        # Zero-count statuses are left out rather than drawn as empty slices.
        self.assertNotIn("Declined", [row["status"] for row in stats["donor_status_summary"]])

    def test_empty_database_still_returns_a_full_chartable_payload(self):
        stats = self.get_stats()
        self.assertEqual(len(stats["booking_trend"]), TREND_MONTHS)
        self.assertTrue(all(row["count"] == 0 for row in stats["booking_trend"]))
        self.assertEqual(stats["donor_status_summary"], [])
        self.assertEqual(stats["articles_by_category"], [])

    def decline(self, reason):
        booking = self.book(months_ago=0, status=MilkBankRequest.Status.DECLINED)
        MilkBankRequest.objects.filter(pk=booking.pk).update(decline_reason=reason)
        return booking

    def test_decline_reasons_are_grouped_and_ordered_most_common_first(self):
        self.decline("Failed breastmilk analysis")
        self.decline("Failed breastmilk analysis")
        self.decline("Outdated Serological Test")
        self.assertEqual(
            self.get_stats()["decline_reasons"],
            [
                {"reason": "Failed breastmilk analysis", "count": 2},
                {"reason": "Outdated Serological Test", "count": 1},
            ],
        )

    def test_a_decline_with_no_reason_is_counted_as_not_specified(self):
        self.decline("")
        self.assertEqual(self.get_stats()["decline_reasons"], [{"reason": "Not specified", "count": 1}])

    def test_expired_and_open_bookings_are_not_decline_reasons(self):
        expired = self.book(months_ago=0, status=MilkBankRequest.Status.EXPIRED)
        MilkBankRequest.objects.filter(pk=expired.pk).update(decline_reason="Failed breastmilk analysis")
        self.book(months_ago=0, status=MilkBankRequest.Status.PENDING)
        self.assertEqual(self.get_stats()["decline_reasons"], [])

    def test_declined_bookings_appear_in_the_status_summary_too(self):
        self.decline("Failed breastmilk analysis")
        summary = {row["status"]: row["count"] for row in self.get_stats()["donor_status_summary"]}
        self.assertEqual(summary["Declined"], 1)
