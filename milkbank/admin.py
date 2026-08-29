from django.contrib import admin

from .models import DonorQuestionnaire, Facility, MilkBankRequest, TransactionRecord


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "is_operational", "capacity", "booked_count", "stock_level_ml")
    list_filter = ("type", "is_operational")
    search_fields = ("name", "address")


@admin.register(MilkBankRequest)
class MilkBankRequestAdmin(admin.ModelAdmin):
    list_display = ("owner", "request_type", "allocated_facility", "current_sub_status", "preferred_date")
    list_filter = ("request_type", "current_sub_status")


@admin.register(TransactionRecord)
class TransactionRecordAdmin(admin.ModelAdmin):
    list_display = ("owner", "type", "facility_name", "date", "status")
    list_filter = ("type", "status")


@admin.register(DonorQuestionnaire)
class DonorQuestionnaireAdmin(admin.ModelAdmin):
    list_display = ("request", "good_general_health", "consents_to_screening", "submitted_at")
