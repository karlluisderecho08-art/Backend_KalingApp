from django.urls import path

from .views import (
    AcceptCounterOfferView,
    AllMilkBankRequestsView,
    ConfirmAttendanceView,
    DonorQuestionnaireView,
    FacilityDetailView,
    FacilityListView,
    MilkBankRequestCreateView,
    MilkBankRequestDetailView,
    MyMilkBankRequestsView,
    MyTransactionsView,
    RejectCounterOfferView,
    SerologyPhotoView,
    SmartAllocationView,
    StaffAcceptView,
    StaffConfirmCompletionView,
    StaffDeclineView,
    StaffExpireView,
    StaffProposeCounterOfferView,
)

urlpatterns = [
    path("facilities/", FacilityListView.as_view(), name="facility-list"),
    path("facilities/<int:pk>/", FacilityDetailView.as_view(), name="facility-detail"),
    path("allocate/", SmartAllocationView.as_view(), name="smart-allocate"),

    path("requests/", MilkBankRequestCreateView.as_view(), name="request-create"),
    path("requests/mine/", MyMilkBankRequestsView.as_view(), name="request-mine"),
    path("requests/all/", AllMilkBankRequestsView.as_view(), name="request-all"),
    path("requests/<int:pk>/", MilkBankRequestDetailView.as_view(), name="request-detail"),

    path("requests/<int:pk>/confirm-attendance/", ConfirmAttendanceView.as_view(), name="request-confirm-attendance"),
    path("requests/<int:pk>/accept-counter-offer/", AcceptCounterOfferView.as_view(), name="request-accept-counter-offer"),
    path("requests/<int:pk>/reject-counter-offer/", RejectCounterOfferView.as_view(), name="request-reject-counter-offer"),

    path("requests/<int:pk>/accept/", StaffAcceptView.as_view(), name="request-staff-accept"),
    path("requests/<int:pk>/decline/", StaffDeclineView.as_view(), name="request-staff-decline"),
    path("requests/<int:pk>/expire/", StaffExpireView.as_view(), name="request-staff-expire"),
    path("requests/<int:pk>/propose-counter-offer/", StaffProposeCounterOfferView.as_view(), name="request-staff-propose-counter-offer"),
    path("requests/<int:pk>/confirm-completion/", StaffConfirmCompletionView.as_view(), name="request-staff-confirm-completion"),

    path("requests/<int:pk>/donor-questionnaire/", DonorQuestionnaireView.as_view(), name="donor-questionnaire"),
    path("requests/<int:pk>/serology-photo/", SerologyPhotoView.as_view(), name="serology-photo"),

    path("transactions/mine/", MyTransactionsView.as_view(), name="transactions-mine"),
]
