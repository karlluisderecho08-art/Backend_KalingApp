from django.core.management.base import BaseCommand

from milkbank.models import Facility


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
    Metro Manila, clearly not verified. Fabella's coordinates are an
    approximate geocode of its confirmed street address, not a surveyed
    pin.

    capacity / booked_count / stock_level_ml are demo numbers, not real
    operational data from the partner facilities -- nobody has given us
    real figures yet (same open item as the roadmap's "minimum stock
    threshold" gap -- see MINIMUM_STOCK_THRESHOLD_ML in allocation.py).
    Good enough to prove the sort works; not something to show a panel
    as real facility status.
    """

    help = "Seed the demo milk bank facilities"

    def handle(self, *args, **options):
        facilities = [
            {
                "name": "St. Luke's Medical Center",
                "type": Facility.FacilityType.HUMAN_MILK_BANK,
                "contact": "(02) 8723-0101",
                "address": "279 E. Rodriguez Sr. Ave, Quezon City",
                "capacity": 20,
                "booked_count": 10,
                "stock_level_ml": 800,
                "latitude": 14.6091,
                "longitude": 121.0223,
            },
            {
                "name": "Philippine General Hospital",
                "type": Facility.FacilityType.HOSPITAL_DEPOT,
                "contact": "(02) 8554-8400",
                "address": "Taft Ave, Ermita, Manila",
                "capacity": 40,
                "booked_count": 20,
                "stock_level_ml": 1500,
                "latitude": 14.5764,
                "longitude": 120.9850,
            },
            {
                "name": "St. Martin de Porres",
                "type": Facility.FacilityType.HOSPITAL_DEPOT,
                "contact": "",
                "address": "",  # unconfirmed -- fill in via admin once verified
                "capacity": 15,
                "booked_count": 12,
                "stock_level_ml": 200,
                "latitude": 14.5794,
                "longitude": 121.0359,
            },
            {
                "name": "Dr. Jose Fabella Memorial Hospital Human Milk Bank",
                "type": Facility.FacilityType.HUMAN_MILK_BANK,
                "contact": "8866-7960",
                "address": "1003 Lope de Vega St, Santa Cruz, Manila, 1003 Metro Manila",
                "capacity": 25,
                "booked_count": 0,
                "stock_level_ml": 600,
                "latitude": 14.6169,
                "longitude": 120.9833,
            },
        ]
        for data in facilities:
            obj, created = Facility.objects.get_or_create(name=data["name"], defaults=data)
            verb = "Created" if created else "Already exists"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {obj.name}"))
