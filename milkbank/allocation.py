import math

from django.db.models import F

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
    """
    Nothing was left to rank. Covers every way the candidate list can
    come back empty, which is more than the name suggests: no facility
    operational, none with a capacity configured, none with room left
    (added with the capacity gate below), or -- RECIPIENT only -- none
    holding at least MINIMUM_STOCK_THRESHOLD_ML. The caller can't tell
    these apart, and deliberately doesn't try: see
    _allocation_error_response, which answers with one message covering
    all of them rather than telling a mother which internal filter
    rejected her.
    """


def eligibility_filters(request_type):
    """
    The binary eligibility gate, as queryset filters: what a facility must
    be for a request of this type to be sent to it at all.

    Written once and used twice -- to build the candidate list in
    get_ranked_facilities(), and again as the WHERE clause of the UPDATE
    in claim_slot() -- so "eligible when we ranked" and "eligible at the
    instant we booked" can never drift into two different definitions.
    """
    filters = {
        "is_operational": True,
        "capacity__gt": 0,
        "booked_count__lt": F("capacity"),
    }
    # A RECIPIENT can't be sent to a facility too low on stock to
    # actually give her milk -- that's a hard exclusion, not just a
    # tie-break. A DONOR is never excluded this way: a low-stock
    # facility is exactly who most needs a donation.
    if request_type == "RECIPIENT":
        filters["stock_level_ml__gte"] = MINIMUM_STOCK_THRESHOLD_ML
    return filters


def claim_slot(facility, request_type):
    """
    Take one booking slot at `facility`, if it still has one. Returns True
    if the slot was taken, False if the facility stopped being eligible
    between being ranked and now.

    The check and the increment are ONE statement:

        UPDATE facility SET booked_count = booked_count + 1
         WHERE id = ... AND booked_count < capacity AND ...

    That is what makes it safe when two mothers book at the same instant.
    Ranking reads the facility list, and creating the booking happens a
    moment later; done as "check there is room, then add one" in Python,
    two requests could both read 19 of 20, both decide there was room,
    and both add one -- 21 of 20, with nothing to stop it. A conditional
    UPDATE cannot do that: the database applies the two one after the
    other, and the second re-checks its WHERE against the row the first
    just wrote, finds 20 of 20, and changes nothing. The row count it
    returns is how the caller learns which of the two it was.
    """
    from .models import Facility

    taken = Facility.objects.filter(
        pk=facility.pk, **eligibility_filters(request_type)
    ).update(booked_count=F("booked_count") + 1)
    return taken == 1


def get_ranked_facilities(user, request_type):
    """
    Shared by the standalone /milkbank/allocate/ preview endpoint and
    the real booking-creation endpoint, so "how we pick a facility" only
    exists in one place.
    """
    from .models import Facility

    if user.latitude is None or user.longitude is None:
        raise LocationRequired()

    # The binary eligibility gate (step 1 of the sort). Three separate
    # reasons to be "not a candidate," full stop:
    #
    #   is_operational=False   the facility isn't running at all
    #   capacity=0             it exists but takes no bookings, ever
    #   booked_count >= capacity
    #                          it takes bookings, but has no room left
    #                          right now
    #
    # That last one used to be missing entirely. Only capacity__gt=0 was
    # checked -- "is a capacity configured", not "is there any of it
    # left" -- so a facility sitting at 30/30 stayed a candidate, and
    # MilkBankRequestCreateView assigns ranked[0] unconditionally. A full
    # facility normally sank to the bottom on its own (ratio 1.0 was the
    # worst possible score under the old ordering) but "normally" was
    # doing real work there: if it ranked first anyway -- every facility
    # full, or a one-facility candidate pool -- the booking was created
    # against it regardless and booked_count incremented straight past
    # capacity, with nothing anywhere to stop it.
    #
    # Now that distance leads the sort (see rank_facilities), that
    # accidental protection is gone for good: a full facility that
    # happens to be nearest would otherwise rank FIRST, not last. This
    # gate is what makes reordering the sort safe.
    candidates = list(Facility.objects.filter(**eligibility_filters(request_type)))
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

    Caller must already have applied the binary eligibility gate (step 1)
    in get_ranked_facilities(): operational, capacity configured, and
    with room left right now. Everything this function sees can actually
    take the booking, so the only question left is which one to prefer.

      2. distance from the mother (Haversine), ascending -- nearest
         wins.
      3. booked_count / capacity, ascending -- the *ratio*, not the raw
         count, so a 100-slot facility with 50 bookings doesn't look
         "busier" than a 10-slot facility with 8 bookings.
      4. stock_level_ml -- which direction depends on request_type:
           DONOR:     ascending  (send donors to whoever needs milk most)
           RECIPIENT: descending (send recipients to whoever has the most to give)
         The hard minimum-stock exclusion for RECIPIENT requests already
         happened in get_ranked_facilities() before this function was
         even called -- what's left to rank here is direction among the
         facilities that passed that bar. DONOR requests were never
         filtered by it; a low-stock facility is exactly who most needs
         a donation.

    WHY DISTANCE LEADS, and what that costs:

    The ratio used to be step 2 and distance step 4, which read as
    sensible load-balancing and behaved as something else entirely. With
    only four partner facilities, their ratios essentially never tie, so
    the ratio alone decided every single allocation and distance was
    never once consulted. Measured against the live facility data, every
    mother got the same facility -- the one with the lowest ratio --
    whether she was 4 km away or 79 km away. Load balancing that ignores
    a 79 km trip isn't balancing anything a mother would recognise as
    fair.

    Distance is also not a symmetric cost here. A recipient makes one
    pickup trip; a DONOR is called in repeatedly (Counseling and
    Testing, then Breastmilk Analysis, then Results -- see
    MilkBankRequest.DONOR_STAGES), so every extra kilometre is paid
    several times over, by someone who just gave birth.

    The honest cost of this ordering: distances are continuous floats, so
    steps 3 and 4 now only fire on an exact distance tie -- two
    facilities at genuinely identical coordinates. In production that is
    close to never. Load spreading is therefore no longer done by the
    sort at all; it is done by the hard capacity gate in
    get_ranked_facilities(), which is a blunter instrument: mothers fill
    the nearest facility until it is full, then the next nearest. That
    is a deliberate trade, not an oversight. If load needs to matter
    again among comparably-near facilities, the fix is to bucket
    distance into bands (say 5 km) so that step 3 has real ties to break
    -- which would mean defending a band size, exactly the kind of
    unvalidated constant MINIMUM_STOCK_THRESHOLD_ML above is already
    flagged for.

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
        return (distance, ratio, stock_sign * facility.stock_level_ml)

    return sorted(facilities, key=sort_key)
