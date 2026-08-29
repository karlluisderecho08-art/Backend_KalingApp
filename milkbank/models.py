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
        "Status", "Booking Confirmation", "Counseling & Serology Screening",
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

    Replaces an earlier 7-question approximation with the real official
    form the user supplied on 2026-08-22, closing the standing TODO
    carried over from the Kotlin app (KalingAppViewModel.kt:230 /
    AllScreens.kt:2601: "verify against the official Fabella/PHMBA donor
    screening form"). Field names below map directly to that form's
    sections A-F. `infant_age_months` assumes months as the unit since
    the form didn't specify one -- flag it if that's wrong.

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

    # --- Section A: Identification & Consent ---
    currently_lactating_excess = models.BooleanField(
        help_text="Currently lactating and producing milk beyond own infant's needs")
    infant_age_months = models.PositiveIntegerField(help_text="Age of donor's own infant, in months")
    consents_to_screening = models.BooleanField(
        help_text="Consents to blood screening and to donating voluntarily, without payment")

    # --- Section B: General Health ---
    good_general_health = models.BooleanField()
    being_treated_for_illness = models.BooleanField(help_text="Being treated for any acute or chronic illness")
    recent_fever_or_infection = models.BooleanField(help_text="Fever or active infection in the past week")

    # --- Section C: Infectious Disease Risk ---
    tested_positive_infectious_disease = models.BooleanField(
        help_text="Ever tested positive for HIV 1/2, HTLV 1/2, Hepatitis B, Hepatitis C, or syphilis")
    partner_tested_positive_or_at_risk = models.BooleanField(
        help_text="Sexual partner ever tested positive for, or at risk of, HIV or hepatitis")
    recent_blood_transfusion = models.BooleanField(
        help_text="Blood transfusion or blood products in the past 12 months")
    recent_tattoo_piercing_needle_exposure = models.BooleanField(
        help_text="Tattoo, piercing, or accidental needle-stick exposure in the past 12 months")
    travel_to_risk_area = models.BooleanField(
        help_text="Traveled to/lived in an area with risk of relevant transmissible disease (per DOH advisories)")

    # --- Section D: Lifestyle ---
    smokes_or_tobacco = models.BooleanField()
    drinks_alcohol = models.BooleanField()
    alcohol_frequency_details = models.CharField(max_length=255, blank=True, help_text="How often and how much")
    uses_illicit_drugs = models.BooleanField()

    # --- Section E: Medications & Supplements ---
    on_prescription_medications = models.BooleanField()
    medication_list = models.TextField(blank=True)
    uses_herbal_supplements = models.BooleanField(
        help_text="Herbal supplements, megadose vitamins, or botanical products")
    uses_radioactive_or_radiologic = models.BooleanField(
        help_text="Radioactive substances or undergoing radiologic treatment")

    # --- Section F: Diet & Other ---
    vegan_without_b12 = models.BooleanField(
        help_text="Diet excludes all animal products without B12 supplementation")
    recent_live_virus_vaccine = models.BooleanField(help_text="Recently received any live-virus vaccines")

    serology_photo = models.FileField(upload_to="serology_photos/%Y/%m/", blank=True, null=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Donor questionnaire for request #{self.request_id}"
