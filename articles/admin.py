from django.contrib import admin

from .models import Article, ArticleComment, ResourceLink


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "date")
    list_filter = ("category",)
    search_fields = ("title", "teaser")


@admin.register(ArticleComment)
class ArticleCommentAdmin(admin.ModelAdmin):
    list_display = ("article", "author", "is_reported", "created_at")
    list_filter = ("is_reported",)


@admin.register(ResourceLink)
class ResourceLinkAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "url")
    list_filter = ("type",)
