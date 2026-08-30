from django.db.models import Count
from django.db.models.functions import TruncMonth
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from articles.models import Article, ArticleComment
from milkbank.models import Facility, MilkBankRequest


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

        booking_trend = (
            MilkBankRequest.objects.annotate(month=TruncMonth("submitted_at"))
            .values("month")
            .annotate(count=Count("id"))
            .order_by("month")
        )

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

        return Response({
            "total_bookings": MilkBankRequest.objects.count(),
            # Distinct owners, not raw request rows -- "how many donors/
            # recipients" means people, and one person can have more than
            # one request over time (a completed one, then a new one).
            "total_donors": donor_requests.values("owner_id").distinct().count(),
            "total_recipients": recipient_requests.values("owner_id").distinct().count(),
            "active_facilities": Facility.objects.filter(is_operational=True).count(),
            "total_articles": Article.objects.count(),
            "pending_comment_reports": ArticleComment.objects.filter(is_reported=True).count(),
            "booking_trend": [
                {"month": row["month"].strftime("%b"), "count": row["count"]}
                for row in booking_trend
            ],
            "articles_by_category": [
                {"category": row["category"], "count": row["count"]}
                for row in category_breakdown
            ],
            "donor_status_summary": status_breakdown(donor_requests),
            "recipient_status_summary": status_breakdown(recipient_requests),
        })
