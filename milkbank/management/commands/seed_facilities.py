from django.core.management.base import BaseCommand

from core.audit import log_action
from core.models import AuditLogEntry
from milkbank.models import Facility

# Written to the audit log the one time the demo facilities are seeded, and
# checked on every run after. The audit log is append-only, so the record
# outlives the facilities themselves.
SEEDED_ACTION = "seed.facilities"


class Command(BaseCommand):
    """
    manage.py seed_facilities

    Seeds the facilities named in the Kotlin app's mock data
    (CODEBASE-1.md section 7: St. Luke's, PGH, St. Martin de Porres),
    plus Fabella -- added as a real bookable Facility per the user's
    2026-08-22 decision to reconcile it with its existing directory-only
    SupportContact entry (see roadmap gap: "two unreconciled datasets").

    Coordinates are the real, public locations of St. Luke's Medical
    Center (Quezon City), Philippine General Hospital (Manila), and
    Fabella (Sta. Cruz, Manila) -- those are just public facts, safe to
    hardcode. St. Martin de Porres' exact address isn't confirmed
    anywhere in this repo, so its coordinates are a placeholder pin in
    Metro Manila, clearly not verified. Fabella's address is the one on
    its own site (San Lazaro Compound, Tayuman St. -- it moved out of
    Lope de Vega St.), and its coordinates are OpenStreetMap's pin for
    the hospital on Tayuman Street, not a surveyed pin. A database seeded
    before that move is corrected by migration 0011_fabella_current_address;
    the values below match it so a fresh database starts out right.

    capacity / booked_count / stock_level_ml are STILL not real
    operational data -- nobody has supplied real figures (same open item
    as the roadmap's "minimum stock threshold" gap, see
    MINIMUM_STOCK_THRESHOLD_ML in allocation.py). They have been made
    plausible rather than left as round demo values: the old set had two
    facilities sitting at exactly 50% booked (20/10, 40/20) and stock in
    flat hundreds, which reads as invented the moment anyone looks at it
    on screen. The figures now vary the way real readings do, scale
    roughly with each facility's size, and keep booked_count under
    capacity.

    They are still unverified, and that has NOT changed -- do not present
    them to a panel as real facility status. Plausible test data is easier
    to demo with; it is not the same as true data.

    Seeds ONCE per database, not once per deploy. build.sh runs this on
    every deploy, and it used to get_or_create each facility by name every
    time -- so a facility an admin deleted from the dashboard was quietly
    recreated by the next deploy, and "keeps coming back" was the bug.
    The first run now leaves a SEEDED_ACTION line in the audit log and every
    later run stops there, so what an admin deletes stays deleted. (A
    database seeded before this existed gets that line from migration
    0013_mark_facilities_seeded.)
    """

    help = "Seed the demo milk bank facilities"

    def handle(self, *args, **options):
        if AuditLogEntry.objects.filter(action=SEEDED_ACTION).exists():
            self.stdout.write("Facilities were already seeded once -- leaving them as the admin has them.")
            return

        facilities = [
            {
                "name": "St. Luke's Medical Center",
                "type": Facility.FacilityType.HUMAN_MILK_BANK,
                "contact": "(02) 8723-0101",
                "address": "279 E. Rodriguez Sr. Ave, Quezon City",
                "capacity": 18,
                "booked_count": 11,
                "stock_level_ml": 740,
                "latitude": 14.6091,
                "longitude": 121.0223,
            },
            {
                "name": "Philippine General Hospital",
                "type": Facility.FacilityType.HOSPITAL_DEPOT,
                "contact": "(02) 8554-8400",
                "address": "Taft Ave, Ermita, Manila",
                "capacity": 36,
                "booked_count": 23,
                "stock_level_ml": 1620,
                "latitude": 14.5764,
                "longitude": 120.9850,
            },
            {
                "name": "St. Martin de Porres",
                "type": Facility.FacilityType.HOSPITAL_DEPOT,
                "contact": "",
                # Left EMPTY on purpose. St. Martin de Porres is a real
                # institution and nobody has confirmed its address or
                # contact number for this project. Inventing a
                # plausible-looking one for a real hospital is worse
                # than a blank field: a mother could act on it. Fill in
                # via the admin panel once verified.
                "address": "",
                "capacity": 12,
                "booked_count": 9,
                "stock_level_ml": 185,
                "latitude": 14.5794,
                "longitude": 121.0359,
            },
            {
                "name": "Dr. Jose Fabella Memorial Hospital Human Milk Bank",
                "type": Facility.FacilityType.HUMAN_MILK_BANK,
                "contact": "8866-7960",
                "address": "San Lazaro Compound, Tayuman St., Santa Cruz, Manila",
                "capacity": 24,
                "booked_count": 7,
                "stock_level_ml": 910,
                "latitude": 14.6153,
                "longitude": 120.9804,
            },
        ]
        for data in facilities:
            obj, created = Facility.objects.get_or_create(name=data["name"], defaults=data)
            verb = "Created" if created else "Already exists"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {obj.name}"))
        log_action(None, SEEDED_ACTION, "Facility: demo set")
