"""
What a mother is told about how her request ended -- in plain words.

Staff decline a request with a short reason label ("Failed breastmilk
analysis") and, optionally, notes. Those notes are written for the facility's
own records and can carry lab findings (bacterial counts, serology values)
that mean nothing to a mother, or alarm her without explaining anything. She
used to be shown "reason -- notes" verbatim. Now she is shown, for each reason
staff can pick, a short explanation written for her, and:

  - where the reason may point to a health concern for HER (a failed blood
    test, physical examination or milk analysis), a gentle suggestion to see
    her doctor, and the staff notes are NOT shown -- the facility explains the
    details in person;
  - where it is practical (documents, an outdated test, stock), the staff
    note IS shown as "Note from the facility", because it is usually the thing
    she needs to act on.

The labels are the ones the facility dashboard offers
(Facility_KalingApp/lib/booking.ts DECLINE_REASONS, and the Booking Request
desk's own list). A label not listed here -- "Others", or one added later --
falls back to the staff note, or a generic message if there is none.
"""

from .models import MilkBankRequest

Status = MilkBankRequest.Status

SEE_YOUR_DOCTOR = (
    "Please visit your doctor or health center so they can check this with you. "
    "The milk bank can also explain the result in person."
)

# label (lower case) -> (title, explanation, may point to a health concern)
DECLINE_RESULTS = {
    "failed blood test": (
        "Not able to donate at this time",
        "Your blood screening showed a result that means you can't donate milk right now. "
        "This rule protects the fragile babies who receive donor milk, and it doesn't "
        "always mean something is wrong with you. " + SEE_YOUR_DOCTOR,
        True,
    ),
    "failed physical examination": (
        "Not able to donate at this time",
        "The health check during your screening found something that needs attention "
        "before you can donate. " + SEE_YOUR_DOCTOR,
        True,
    ),
    "failed breastmilk analysis": (
        "Milk sample not accepted",
        "Your milk sample didn't pass the safety checks needed for donation. This can "
        "happen for many reasons, including how the milk was expressed or stored, and it "
        "is not a diagnosis. If you have breast pain, redness, a fever or feel unwell, "
        + SEE_YOUR_DOCTOR[0].lower() + SEE_YOUR_DOCTOR[1:],
        True,
    ),
    "did not pass counseling": (
        "Not able to donate at this time",
        "After your counseling session, the milk bank found that donating isn't the "
        "right fit for you at this time. This is not about anything you did wrong. "
        "Thank you for wanting to help other babies.",
        False,
    ),
    "milk did not meet quality standards": (
        "Milk not accepted",
        "The milk collected didn't meet the milk bank's quality standards, so it "
        "couldn't be used for donation. You're welcome to ask the facility about "
        "donating again.",
        False,
    ),
    "outdated serological test": (
        "A newer blood test is needed",
        "Your blood test result is older than the milk bank can accept (it must be "
        "from the last 6 months). Please get a new blood test, then submit a new request.",
        False,
    ),
    "incomplete or invalid requirements": (
        "Requirements incomplete",
        "Some of the documents needed for your request were missing or couldn't be "
        "accepted. Please check the requirements, then submit a new request.",
        False,
    ),
    "not enough milk available": (
        "Not enough donor milk right now",
        "The milk bank doesn't have enough donor milk at the moment to fill your "
        "request. Please try again later. We're sorry we couldn't help this time.",
        False,
    ),
}

GENERIC_DECLINE = (
    "Request not accepted",
    "The facility wasn't able to accept your request. Please contact them if you "
    "have questions.",
)

EXPIRED_FACILITY = (
    "Request expired",
    "The facility didn't respond to your request in time, so it expired. "
    "Please submit a new request.",
)
EXPIRED_MOTHER = (
    "Request expired",
    "Your attendance wasn't confirmed in time after the facility accepted your "
    "request, so it expired. Please submit a new request.",
)


def facility_note(req):
    """
    The staff's own words, without the reason label the dashboard puts in
    front of them ("Failed blood test -- <notes>" -> "<notes>").
    """
    message = (req.staff_message or "").strip()
    reason = (req.decline_reason or "").strip()
    if reason and message.lower().startswith(reason.lower()):
        message = message[len(reason):].lstrip(" —–-:.").strip()
    return message


def mother_result(req):
    """
    How the request ended, for her -- or None while it is still in progress.

    {"title", "message", "health_followup": bool, "facility_note": str}
    `facility_note` is "" whenever it is withheld (health-related declines) or
    there is none.
    """
    if req.current_sub_status == Status.DECLINED:
        known = DECLINE_RESULTS.get((req.decline_reason or "").strip().lower())
        note = facility_note(req)
        if known:
            title, message, health = known
            return {
                "title": title,
                "message": message,
                "health_followup": health,
                "facility_note": "" if health else note,
            }
        # "Others" or an unknown label: the staff note is the explanation.
        title, generic = GENERIC_DECLINE
        return {
            "title": title,
            "message": note or generic,
            "health_followup": False,
            "facility_note": "",
        }

    if req.current_sub_status == Status.EXPIRED:
        waiting_on_her = (
            req.stages[req.current_stage_index] == "Booking Confirmation" and not req.attendance_confirmed
        )
        title, message = EXPIRED_MOTHER if waiting_on_her else EXPIRED_FACILITY
        return {"title": title, "message": message, "health_followup": False, "facility_note": ""}

    if req.current_sub_status == Status.COMPLETED:
        if req.request_type == MilkBankRequest.RequestType.DONOR:
            message = "Thank you for donating! Your milk will help feed babies who need it."
        else:
            message = "Your request is complete. We hope your baby is doing well."
        return {"title": "Completed", "message": message, "health_followup": False, "facility_note": ""}

    return None


def decline_notification(reason, staff_message):
    """The decline notification's text: the same explanation she sees in the app."""
    req = MilkBankRequest(
        current_sub_status=Status.DECLINED, decline_reason=reason or "", staff_message=staff_message or "",
    )
    result = mother_result(req)
    text = f"{result['title']}. {result['message']}"
    if result["facility_note"]:
        text += f" Note from the facility: {result['facility_note']}"
    return text
