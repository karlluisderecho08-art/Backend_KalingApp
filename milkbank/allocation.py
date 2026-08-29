import math

# PLACEHOLDER, not a researched figure. Nobody -- not the partner
# facilities, not this codebase -- has supplied a real minimum-stock
# cutoff yet (roadmap's own closing line flags this). This number only
# exists so the exclusion rule below is testable; treat it as a stand-in
# to replace the moment a real number comes from St. Luke's/PGH/Fabella.
MINIMUM_STOCK_THRESHOLD_ML = 300


class AllocationError(Exception):
    """A Smart Allocation call couldn't run at all -- a precondition
    problem (no location, no facility available), not a ranking one."""


class LocationRequired(AllocationError):
    pass


class NoOperationalFacility(AllocationError):
    pass


def get_ranked_facilities(user, request_type):
    """
    Shared by the standalone /milkbank/allocate/ preview endpoint and
    the real booking-creation endpoint, so "how we pick a facility" only
    exists in one place.
    """
    from .models import Facility

    if user.latitude is None or user.longitude is None:
        raise LocationRequired()

    # The binary eligibility gate (step 1 of the sort): not operational,
    # or no bookable capacity at all, means "not a candidate," full stop.
    candidates = Facility.objects.filter(is_operational=True, capacity__gt=0)

    # A RECIPIENT can't be sent to a facility too low on stock to
    # actually give her milk -- that's a hard exclusion, not just a
    # tie-break. A DONOR is never excluded this way: a low-stock
    # facility is exactly who most needs a donation.
    if request_type == "RECIPIENT":
        candidates = candidates.filter(stock_level_ml__gte=MINIMUM_STOCK_THRESHOLD_ML)

    candidates = list(candidates)
    if not candidates:
        raise NoOperationalFacility()

    return rank_facilities(candidates, request_type, user.latitude, user.longitude)


def haversine_km(lat1, lon1, lat2, lon2):
    """
    Straight-line ("as the crow flies") distance between two points on
    Earth, in kilometers. Not driving distance -- good enough to tell
    which of a handful of facilities is nearer, without needing a paid
    maps API.
    """
    earth_radius_km = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def rank_facilities(facilities, request_type, mother_lat, mother_lon):
    """
    The manuscript's Smart Allocation sort: a strict tie-breaker chain,
    not a weighted score -- there are no point values to defend to a
    panel, just "if step 1 ties, look at step 2."

    Caller must already have filtered to is_operational=True and
    capacity>0 -- that's the binary eligibility gate (step 1), applied
    before this function ever sees the list.

      2. booked_count / capacity, ascending -- the *ratio*, not the raw
         count, so a 100-slot facility with 50 bookings doesn't look
         "busier" than a 10-slot facility with 8 bookings.
      3. stock_level_ml -- which direction depends on request_type:
           DONOR:     ascending  (send donors to whoever needs milk most)
           RECIPIENT: descending (send recipients to whoever has the most to give)
         The hard minimum-stock exclusion for RECIPIENT requests already
         happened in get_ranked_facilities() before this function was
         even called -- what's left to rank here is direction among the
         facilities that passed that bar. DONOR requests were never
         filtered by it; a low-stock facility is exactly who most needs
         a donation.
      4. distance from the mother (Haversine), ascending -- closest wins
         any remaining tie.

    Returns the same facilities, best match first, each one carrying two
    extra attributes (booked_ratio, distance_km) so the caller/serializer
    can show its work instead of just handing back a black-box pick.
    """
    stock_sign = 1 if request_type == "DONOR" else -1

    def sort_key(facility):
        ratio = facility.booked_count / facility.capacity
        distance = haversine_km(mother_lat, mother_lon, facility.latitude, facility.longitude)
        facility.booked_ratio = ratio
        facility.distance_km = distance
        return (ratio, stock_sign * facility.stock_level_ml, distance)

    return sorted(facilities, key=sort_key)
