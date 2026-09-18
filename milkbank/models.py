from django.conf import settings
from django.db import models


class Facility(models.Model):
    """
    A bookable milk bank facility. Ported from the Kotlin Facility data
    class (CODEBASE-1.md section 3), extended for Phase 3's Smart
    Allocation (roadmap gap #2): the old shape had `distance` as a
    display string and no capacity/stock/coordinates, which can't feed
    a real sort. `distance` is dropped entirely -- it's now computed
    per-mother at request time, not stored.

    unavailable_donor_dates / unavailable_recipient_dates: the roadmap
    suggests Postgres's ArrayField(DateField) for these. This project is
    still on SQLite in dev (see DATABASE_URL in settings), and
    ArrayField only exists for Postgres -- it would break `migrate`
    outright on SQLite. JSONField (a list of "YYYY-MM-DD" strings)
    behaves the same on both SQLite and Postgres, so dev and prod don't
    diverge. Worth revisiting only if a query ever needs to search
    *inside* the list at the database level, which nothing here does.
    """

    class FacilityType(models.TextChoices):
        HUMAN_MILK_BANK = "Accredited Human Milk Bank", "Accredited Human Milk Bank"
        HOSPITAL_DEPOT = "Hospital Depot", "Hospital Depot"

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=100, choices=FacilityType.choices)
    contact = models.CharField(max_length=50)
    address = models.CharField(max_length=500)
    operating_hours = models.CharField(max_length=100, default="8:00 AM - 5:00 PM (Mon-Fri)")
    donor_requirements = models.TextField(blank=True)
    recipient_requirements = models.TextField(blank=True)
    unavailable_donor_dates = models.JSONField(default=list, blank=True)
    unavailable_recipient_dates = models.JSONField(default=list, blank=True)

    # --- New in Phase 3, for Smart Allocation ---
    is_operational = models.BooleanField(default=True)
    capacity = models.PositiveIntegerField(help_text="Total booking slots this facility can handle")
    booked_count = models.PositiveIntegerField(
        default=0,
        help_text="Slots currently booked. Kept in sync automatically as requests are "
        "created/close out -- see milkbank/transitions.py -- but still editable by hand "
        "in admin, e.g. to seed a starting count before any real bookings exist.",
    )
    stock_level_ml = models.PositiveIntegerField(default=0, help_text="Current stored milk volume, in mL")
    latitude = models.FloatField()
    longitude = models.FloatField()

    def __str__(self):
        return self.name


# What staff records at StaffConfirmCompletionView is a US fluid ounce
# figure (matches how the Kotlin app and Fabella/PGH/St. Luke's staff
# actually talk about volume) -- Facility.stock_level_ml and
# accounts.User.total_drawn_oz's underlying unit are what they are for
# unrelated historical reasons, so this is the one conversion point
# between the two, not something to duplicate at each call site.
ML_PER_FLUID_OUNCE = 29.5735


class MilkBankRequest(models.Model):
    """
    The single active donor/recipient booking for a mother -- ported
    from the Kotlin `MilkBankRequest` (CODEBASE-1.md section 3).

    Unlike the Kotlin version (one in-memory instance, shared by
    whoever's using the app), this is a real row per user, so `owner`
    is new. `stages` isn't stored as a column -- it's fully determined
    by request_type, so storing it would just be a copy that could go
    stale; see the `stages` property below instead.
    """

    class RequestType(models.TextChoices):
        DONOR = "DONOR", "Donor"
        RECIPIENT = "RECIPIENT", "Recipient"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AWAITING_ATTENDANCE = "awaiting_attendance", "Awaiting Attendance"
        SCHEDULED = "scheduled", "Scheduled"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        COUNTER_OFFERED = "counter_offered", "Counter Offer"
        COMPLETED = "completed", "Completed"

    DONOR_STAGES = [
        # Index 2 renamed from "Counseling & Serology Screening" per the
        # Fabella interview: this stage covers serology, physical, AND
        # blood tests, not serology alone -- the old label named only one
        # of the three. Matches the manuscript's "Counseling and Testing"
        # wording throughout.
        "Status", "Booking Confirmation", "Counseling and Testing",
        "Breastmilk Analysis", "Results",
    ]
    RECIPIENT_STAGES = ["Requirements", "Status", "Booking Confirmation", "Results"]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="milkbank_requests")
    request_type = models.CharField(max_length=20, choices=RequestType.choices)
    # PROTECT, not CASCADE/SET_NULL: a facility with real booking history
    # attached to it should never be silently deletable.
    allocated_facility = models.ForeignKey(Facility, on_delete=models.PROTECT, related_name="requests")

    current_stage_index = models.PositiveIntegerField(default=0)
    current_sub_status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    staff_message = models.TextField(blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)
    # Fixed hourly slots displayed as strings ("10:00 AM"), same as the
    # Kotlin scheduler -- not worth a real TimeField for a closed set of
    # slots nothing does arithmetic on.
    preferred_date = models.DateField()
    preferred_time = models.CharField(max_length=20)
    attendance_confirmed = models.BooleanField(default=False)
    counter_offer_date = models.DateField(null=True, blank=True)
    counter_offer_time = models.CharField(max_length=20, blank=True)

    # Optional pickup representative -- RECIPIENT-only (the Kotlin app's
    # recipientNeedsProxy toggle on the Recipient Pathway screen), for a
    # mother who can't collect the pasteurized milk herself. Kept on this
    # model rather than a separate one: it's a handful of fields with no
    # lifecycle of its own, always read/written alongside the request.
    needs_representative = models.BooleanField(default=False)
    representative_name = models.CharField(max_length=255, blank=True)
    representative_birthday = models.DateField(null=True, blank=True)
    representative_contact_number = models.CharField(max_length=50, blank=True)

    # The 8-business-hour SLA clock (see milkbank/business_hours.py). Null
    # whenever nothing is actively pending on someone -- set the moment a
    # request starts waiting on the facility (pending) or on the mother
    # (awaiting_attendance), cleared the moment it stops waiting on anyone
    # (declined/scheduled/completed/expired/counter_offered). Read by
    # transitions.sweep_expired_requests(), never written to directly
    # outside apply_transition/the create view -- see those for why each
    # status either sets or clears it.
    response_deadline = models.DateTimeField(null=True, blank=True)

    @property
    def stages(self):
        return self.DONOR_STAGES if self.request_type == self.RequestType.DONOR else self.RECIPIENT_STAGES

    def __str__(self):
        return f"{self.owner} - {self.request_type} - {self.current_sub_status}"


class TransactionRecord(models.Model):
    """
    A completed transaction, created automatically when a
    MilkBankRequest transitions to completed -- see
    milkbank/transitions.py. facility_name is a plain string snapshot
    (not a Facility FK) on purpose: this is a historical receipt, so it
    shouldn't change if the facility is later renamed, and it should
    stay readable even if the facility row is ever removed.
    """

    class TransactionType(models.TextChoices):
        DONATION = "Donation", "Donation"
        RECEIVED = "Received", "Received"

    class TransactionStatus(models.TextChoices):
        COMPLETED = "Completed", "Completed"
        CANCELLED = "Cancelled", "Cancelled"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions")
    type = models.CharField(max_length=20, choices=TransactionType.choices)
    facility_name = models.CharField(max_length=255)
    date = models.DateField()
    status = models.CharField(max_length=20, choices=TransactionStatus.choices, default=TransactionStatus.COMPLETED)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.owner} - {self.type} - {self.facility_name}"


class DonorQuestionnaire(models.Model):
    """
    The donor eligibility screen + optional serology photo, attached to
    a DONOR-type MilkBankRequest -- kept off that model itself since it
    only applies to donors, not recipients.

    Field-for-field match of the Kotlin app's Donor Eligibility
    Questionnaire (KalingAppViewModel.kt / AllScreens.kt's DonorScreeningScreen) --
    this is the actual set of questions a donor answers on-device, so the
    model asks nothing the app doesn't, and stores nothing the app didn't
    actually ask her.

    serology_photo is saved to local disk (MEDIA_ROOT), but is
    deliberately NOT served through Django's normal "serve this folder
    publicly" URL config (see config/settings/base.py -- there's no
    static() route for MEDIA_URL). The only way to read the file back is
    milkbank.views.SerologyPhotoView, which checks "are you the owner or
    facility staff" before ever opening it. Encryption-at-rest and cloud
    storage are a later, separate decision once real hosting is chosen.
    """

    request = models.OneToOneField(
        MilkBankRequest, on_delete=models.CASCADE, related_name="donor_questionnaire",
    )

    # Q1
    good_general_health = models.BooleanField(help_text="Currently in good general health")
    # Q2
    lactating_with_excess_supply = models.BooleanField(
        help_text="Baby is under 6 months old and producing more milk than baby needs")
    # Q3
    free_of_infectious_disease = models.BooleanField(
        help_text="Free from HIV, Hepatitis B & C, and Syphilis")
    # Q4
    recent_transfusion_or_transplant = models.BooleanField(
        help_text="Received a blood transfusion or organ transplant in the last 12 months")
    # Q5
    uses_tobacco_alcohol_or_drugs = models.BooleanField(
        help_text="Smokes, drinks alcohol regularly, or uses recreational drugs")
    # Q6
    on_medication_or_supplements = models.BooleanField(
        help_text="Taking regular medications or herbal supplements")
    medication_details = models.CharField(max_length=255, blank=True, help_text="Medications / supplements")
    # Q7
    has_recent_serology_test = models.BooleanField(
        help_text="Has a serological (blood) test taken within the last 6 months")

    serology_photo = models.FileField(upload_to="serology_photos/%Y/%m/", blank=True, null=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Donor questionnaire for request #{self.request_id}"
