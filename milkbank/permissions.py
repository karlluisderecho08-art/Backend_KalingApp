from rest_framework import permissions

from accounts.models import User


class IsFacilityStaff(permissions.BasePermission):
    """
    Gates the staff-side actions (accept/decline/etc) to the
    `facility_staff` role -- this is the RBAC role from Phase 1, not
    Django's own is_staff flag (that one gates Django admin / content
    moderation instead; see articles.views.ArticleCommentResolveView).
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == User.Role.FACILITY_STAFF)


class IsRequestOwner(permissions.BasePermission):
    """Only the mother who created this booking can act on it, mother-side."""

    def has_object_permission(self, request, view, obj):
        return obj.owner_id == request.user.id
