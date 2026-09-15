from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models


class UserManager(BaseUserManager):
    """
    AbstractUser ships with a manager that creates users by username.
    Since we're logging in with email instead, we need our own
    create_user/create_superuser that don't require a username.
    """

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Our own User model, extending Django's built-in AbstractUser (which
    already gives us password hashing, is_staff, is_superuser,
    last_login, etc. for free).

    We're swapping this in as AUTH_USER_MODEL instead of using Django's
    default django.contrib.auth.User, because (a) the app logs in with
    email, not a username, and (b) we need to attach KalingApp-specific
    fields directly to the account -- this is the real-auth replacement
    for the client-side UserProfile the Kotlin app keeps in memory today
    (see CODEBASE-1.md section 3, UserProfile).
    """

    username = None
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Role(models.TextChoices):
        MOTHER = "mother", "Mother"
        FACILITY_STAFF = "facility_staff", "Facility Staff"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MOTHER)

    # Which facility a facility_staff account works at -- meaningless
    # for a mother account, always null there. Without this, EVERY
    # facility_staff login could see and act on EVERY facility's
    # bookings (that was the actual behavior until this field was
    # added -- see milkbank/permissions.py and the views it gates).
    # A real deployment needs one facility_staff account per hospital,
    # each pointed at that hospital's own Facility row.
    #
    # String reference ("milkbank.Facility"), not a direct import of
    # milkbank.models: milkbank already depends on accounts (every FK
    # to a user goes through settings.AUTH_USER_MODEL), and this
    # project deliberately keeps that a one-way dependency at the
    # Python-import level -- see accounts/views.py's IsFacilityStaff,
    # duplicated rather than imported from milkbank for the same
    # reason. A string-based FK resolves lazily at app-loading time
    # and needs no `import milkbank...` here, so it doesn't violate
    # that -- Django uses this exact pattern for AUTH_USER_MODEL itself.
    #
    # SET_NULL, not CASCADE/PROTECT: deleting a Facility shouldn't take
    # a staff account down with it (nor should it be blocked by one) --
    # it should just leave that account facility-less until reassigned.
    facility = models.ForeignKey(
        "milkbank.Facility", null=True, blank=True, on_delete=models.SET_NULL, related_name="staff",
    )

    # --- Ported verbatim from the Kotlin UserProfile data class ---
    mom_name = models.CharField(max_length=150, blank=True)
    baby_name = models.CharField(max_length=150, blank=True)
    baby_age_weeks = models.PositiveIntegerField(null=True, blank=True)
    breastfeeding_status = models.CharField(max_length=255, blank=True)
    baby_birth_date = models.DateField(null=True, blank=True)
    pediatric_clinic = models.CharField(max_length=255, blank=True)
    tracking_streaks = models.PositiveIntegerField(default=0)
    # Drives tracking_streaks: not exposed to the client directly, just
    # what CheckInView compares "today" against to decide whether to
    # advance the streak, hold it flat (already checked in today), or
    # reset it to 1 (a day was missed). Ported from nothing -- the
    # original Kotlin trackingStreaks was a static seed value with no
    # real increment logic anywhere, client or server.
    last_active_date = models.DateField(null=True, blank=True)
    total_drawn_oz = models.FloatField(default=0.0)

    # --- New: location, for Phase 3's Smart Allocation distance term ---
    # Captured via device GPS, so RA 10173 requires an explicit, logged
    # consent event before we store any coordinate -- not just a privacy
    # policy paragraph nobody reads.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    location_consent_given = models.BooleanField(default=False)
    location_consent_at = models.DateTimeField(null=True, blank=True)

    # Per-account, not per-device on purpose: the Kotlin app's onboarding
    # tour used to be a local-only flag that reset on every fresh login,
    # so a mother who'd already dismissed it saw it again next session.
    # Whether she's dismissed it is a fact about her account, so it
    # belongs here, not in device storage.
    has_seen_walkthrough = models.BooleanField(default=False)

    # True for every account created through the normal signup flow --
    # a User row now only comes into existence *after* its code was
    # entered correctly (see PendingRegistration below), so there is no
    # such thing as an unverified account any more. Kept as a field
    # rather than assumed, because accounts created by other paths
    # (createsuperuser, the seed commands) never went through email
    # verification at all and shouldn't claim they did.
    email_verified = models.BooleanField(default=False)

    # --- Password reset ("Forgot Password?") -- same shape as email
    # verification above (a 6-digit code, a sent-at timestamp that
    # doubles as both the expiry and resend-cooldown clock, an attempt
    # counter), deliberately kept as separate fields rather than reused:
    # an in-progress email verification and an in-progress password
    # reset are unrelated events that can legitimately overlap (e.g. she
    # requests a reset for an already-verified account), so one
    # shouldn't clear or expire the other. ---
    password_reset_code = models.CharField(max_length=6, blank=True)
    password_reset_sent_at = models.DateTimeField(null=True, blank=True)
    password_reset_attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return self.email


class PendingRegistration(models.Model):
    """
    A signup that hasn't proved it owns the email address yet.

    Deliberately NOT a User row. Registration used to create the account
    immediately with is_active=False and flip it on once the code was
    entered, which had two problems: every abandoned or mistyped signup
    left a permanent half-account in the users table, and "a user
    exists" stopped meaning anything useful on its own -- every query
    and every admin screen had to remember to filter on is_active to
    avoid counting people who never finished signing up.

    Holding the attempt here instead means a User row only ever comes
    into existence for someone who actually entered the right code, and
    abandoned attempts are self-cleaning: the next signup for the same
    address just replaces the pending row (email is unique), and stale
    rows can be dropped wholesale without touching real accounts.

    `password` stores the same hash User.password would -- set via
    make_password() at signup and copied across verbatim on success.
    A plaintext password is never written here.
    """

    email = models.EmailField(unique=True)
    password = models.CharField(max_length=128, help_text="Already hashed -- never a plaintext password.")
    mom_name = models.CharField(max_length=150, blank=True)
    baby_name = models.CharField(max_length=150, blank=True)

    # Both filled in by send_verification_email(), which is the single
    # owner of a code's lifecycle -- deliberately not set at creation
    # time. Having the row created with one code and the send then
    # generate another is exactly the bug this avoids: the code the
    # database held could change moments after signup returned, so
    # whichever one she was actually emailed was a race.
    # Blank/null therefore means "created, first send not done yet",
    # which the views treat the same as an expired code.
    code = models.CharField(max_length=6, blank=True)
    # Doubles as both the resend cooldown clock and the code's expiry
    # clock (see accounts/emails.py) -- one timestamp, two purposes,
    # rather than a separate field for each.
    sent_at = models.DateTimeField(null=True, blank=True)
    # Wrong-code guesses since the last code was (re)sent -- caps brute
    # forcing a 6-digit code before its 15-minute expiry; a resend
    # resets this to 0 along with issuing a new code.
    attempts = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Pending registration for {self.email}"
