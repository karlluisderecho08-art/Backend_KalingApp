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

    # --- Email verification (RegisterView creates the account with
    # is_active=False; nothing here changes for accounts that already
    # existed before this field was added, since is_active already
    # defaulted to True for them and this migration doesn't touch it) ---
    email_verified = models.BooleanField(default=False)
    email_verification_code = models.CharField(max_length=6, blank=True)
    # Doubles as both the resend cooldown clock and the code's expiry
    # clock (see accounts/emails.py) -- one timestamp, two purposes,
    # rather than a separate field for each.
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)
    # Wrong-code guesses since the last code was (re)sent -- caps brute
    # forcing a 6-digit code before its 15-minute expiry; resend resets
    # this back to 0 along with issuing a new code.
    email_verification_attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return self.email
