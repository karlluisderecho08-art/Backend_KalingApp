from rest_framework import permissions

from accounts.models import User


class IsFacilityStaff(permissions.BasePermission):
    """
    Gates the staff-side actions (accept/decline/etc) to the
    `facility_staff` role -- this is the RBAC role from Phase 1, not
    Django's own is_staff flag (that one gates Django admin / content
    moderation instead; see articles.views.ArticleCommentResolveView).

    has_object_permission below additionally confines a staff member to
    bookings at their OWN facility -- St. Luke's staff must never see or
    act on a PGH booking. DRF calls this automatically for any view that
    reaches its object through get_object() (every Staff*View in
    views.py does), so adding it here is enough to cover all of them
    without touching each view individually. It does NOT help list
    views (there's no single object to check) -- those filter their own
    queryset instead; see AllMilkBankRequestsView.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == User.Role.FACILITY_STAFF)

    def has_object_permission(self, request, view, obj):
        # Fail closed for a staff account with no facility assigned yet
        # (facility=None) -- an incompletely-provisioned account should
        # see nothing, not everything.
        return request.user.facility_id is not None and request.user.facility_id == obj.allocated_facility_id


class IsRequestOwner(permissions.BasePermission):
    """Only the mother who created this booking can act on it, mother-side."""

    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id
