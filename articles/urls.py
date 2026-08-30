from django.urls import path

from .views import (
    AdminArticleDetailView,
    AdminArticleListCreateView,
    ArticleCommentCreateView,
    ArticleCommentDeleteView,
    ArticleCommentReportView,
    ArticleCommentResolveView,
    ArticleDetailView,
    ArticleListView,
    ReportedCommentListView,
    ResourceLinkListView,
)

urlpatterns = [
    path("", ArticleListView.as_view(), name="article-list"),
    path("resource-links/", ResourceLinkListView.as_view(), name="resource-link-list"),
    path("admin/", AdminArticleListCreateView.as_view(), name="admin-article-list-create"),
    path("admin/<int:pk>/", AdminArticleDetailView.as_view(), name="admin-article-detail"),
    path("comments/reported/", ReportedCommentListView.as_view(), name="article-comment-reported"),
    path("<int:pk>/", ArticleDetailView.as_view(), name="article-detail"),
    path("<int:pk>/comments/", ArticleCommentCreateView.as_view(), name="article-comment-create"),
    path("comments/<int:pk>/", ArticleCommentDeleteView.as_view(), name="article-comment-delete"),
    path("comments/<int:pk>/report/", ArticleCommentReportView.as_view(), name="article-comment-report"),
    path("comments/<int:pk>/resolve/", ArticleCommentResolveView.as_view(), name="article-comment-resolve"),
]
