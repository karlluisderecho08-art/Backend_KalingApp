from datetime import datetime, timedelta

from django.db.models import Count
from django.db.models.functions import TruncMonth
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from articles.models import Article, ArticleComment
from milkbank.business_hours import BUSINESS_TZ
from milkbank.models import Facility, MilkBankRequest
from milkbank.results import FACILITY_REMOVED_REASON

# A rolling window of exactly this many months, including the current one.
# Twelve is also what keeps the "%b" labels ("Jan", "Feb", ...) unique --
# any wider window would repeat a month name and collapse two different
# months into one point on the chart, so widen the window and the label
# format together or not at all.
TREND_MONTHS = 12


def _month_start(moment):
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _trend_window():
    """
    The last TREND_MONTHS month-start datetimes, oldest first -- on the
    Manila calendar, like everything else a facility counts by (the server's
    own clock is UTC, where a booking made before 8 AM on the 1st would still
    fall in the previous month).
    """
    cursor = _month_start(datetime.now(BUSINESS_TZ))
    months = []
    for _ in range(TREND_MONTHS):
        months.append(cursor)
        # Step into the previous month by stepping back off its first day.
        cursor = _month_start(cursor - timedelta(days=1))
    months.reverse()
    return months


class AdminDashboardStatsView(APIView):
    """
    GET /dashboard/stats/

    One aggregate payload for the platform admin app's Dashboard and
    Statistics pages -- both screens want the same underlying numbers
    (booking counts, article/comment counts, per-status breakdowns),
    just laid out differently, so computing it once here beats each
    page re-deriving it from separate list endpoints.

    Deliberately its own small app (not tacked onto milkbank or
    articles): it depends on both of them, and neither of those two
    domain apps should depend on the other just to satisfy this one
    cross-cutting admin view.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        donor_requests = MilkBankRequest.objects.filter(request_type=MilkBankRequest.RequestType.DONOR)
        recipient_requests = MilkBankRequest.objects.filter(request_type=MilkBankRequest.RequestType.RECIPIENT)

        # Zero-filled over a fixed window rather than "whatever months
        # happen to have rows": a month with no bookings is a real data
        # point (a flat spot), and omitting it made the trend line join
        # January straight to April as though they were adjacent.
        window = _trend_window()
        counts_by_month = {
            (row["month"].year, row["month"].month): row["count"]
            for row in (
                MilkBankRequest.objects.filter(submitted_at__gte=window[0])
                .annotate(month=TruncMonth("submitted_at", tzinfo=BUSINESS_TZ))
                .values("month")
                .annotate(count=Count("id"))
            )
        }
        booking_trend = [
            {"month": start.strftime("%b"), "count": counts_by_month.get((start.year, start.month), 0)}
            for start in window
        ]

        def status_breakdown(queryset):
            rows = queryset.values("current_sub_status").annotate(count=Count("id"))
            counts = {row["current_sub_status"]: row["count"] for row in rows}
            return [
                {"status": label, "count": counts.get(value, 0)}
                for value, label in MilkBankRequest.Status.choices
                if counts.get(value, 0) > 0
            ]

        category_breakdown = (
            Article.objects.values("category").annotate(count=Count("id")).order_by("-count")
        )

        # Why requests were declined, most common first -- feeds the "top
        # reasons" chart. Only DECLINED (a person said no), never EXPIRED
        # (a clock ran out) or CANCELLED (she withdrew), since those are
        # different problems to fix. Nor the bookings cancelled because their
        # facility was removed from KalingApp (milkbank/removal.py): nobody
        # turned those mothers down. A decline with no recorded reason is
        # still a decline, so it is counted as "Not specified" rather than
        # silently dropped.
        decline_rows = (
            MilkBankRequest.objects.filter(current_sub_status=MilkBankRequest.Status.DECLINED)
            .exclude(decline_reason=FACILITY_REMOVED_REASON)
            .values("decline_reason")
            .annotate(count=Count("id"))
            .order_by("-count", "decline_reason")
        )
        decline_reasons = [
            {"reason": row["decline_reason"] or "Not specified", "count": row["count"]}
            for row in decline_rows
        ]

        def people(queryset):
            # Distinct owners, not raw request rows -- "how many donors/
            # recipients" means people, and one person can have more than
            # one request over time (a completed one, then a new one). A
            # mother who deleted her account still counts once, through the
            # reference her kept bookings share (accounts/deletion.py).
            current = queryset.filter(owner__isnull=False).values("owner_id").distinct().count()
            former = queryset.filter(owner__isnull=True).exclude(former_owner_ref="")
            return current + former.values("former_owner_ref").distinct().count()

        return Response({
            "total_bookings": MilkBankRequest.objects.count(),
            "total_donors": people(donor_requests),
            "total_recipients": people(recipient_requests),
            "active_facilities": Facility.objects.filter(is_operational=True).count(),
            "total_articles": Article.objects.count(),
            "pending_comment_reports": ArticleComment.objects.filter(is_reported=True).count(),
            "booking_trend": booking_trend,
            "articles_by_category": [
                {"category": row["category"], "count": row["count"]}
                for row in category_breakdown
            ],
            "donor_status_summary": status_breakdown(donor_requests),
            "recipient_status_summary": status_breakdown(recipient_requests),
            "decline_reasons": decline_reasons,
        })
