from django.contrib import admin

from .models import SupportContact


@admin.register(SupportContact)
class SupportContactAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "address")
