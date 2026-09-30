"""Business decisions independent of language-model output and external APIs."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings


def classify(facts: dict, settings: Settings, now: datetime) -> dict:
    """Return priority, routing, and the factual reason; safety overrides every route."""
    issue = facts.get("issue_type", "unknown")
    vulnerable = facts.get("elderly_present") or facts.get("medical_risk_present")
    reasons = []
    if facts.get("hazards") or facts.get("safety_latched"):
        return {
            "priority": "emergency",
            "booking_type": "safety_escalation",
            "reasons": facts.get("hazards") or ["Safety concern already reported on this call"],
        }

    temperatures = [
        v
        for v in (facts.get("reported_temperature_f"), facts.get("outside_temp_f"))
        if v is not None
    ]
    winter = now.astimezone(ZoneInfo(settings.service_timezone)).month in (12, 1, 2)
    cold = (
        facts.get("dangerously_cold")
        or facts.get("winter_reported")
        or winter
        or any(t <= settings.cold_threshold_f for t in temperatures)
    )
    hot = facts.get("dangerously_hot") or any(
        t >= settings.extreme_heat_threshold_f for t in temperatures
    )
    if issue == "no_heat" and (cold or vulnerable):
        reasons.append("No heat with cold conditions or a vulnerable resident")
    if issue == "no_ac" and (hot or vulnerable):
        reasons.append("No AC with extreme heat or a vulnerable resident")

    priority = "urgent" if reasons else "routine" if issue == "maintenance" else "standard"
    if facts.get("in_service_area") is False:
        return {
            "priority": priority,
            "booking_type": "out_of_area",
            "reasons": reasons + ["ZIP code outside configured service area"],
        }
    if facts.get("human_requested"):
        return {
            "priority": priority,
            "booking_type": "human_followup",
            "reasons": reasons + ["Caller requested a person"],
        }
    if facts.get("request_intent") in ("quote_inquiry", "team_message"):
        return {
            "priority": priority,
            "booking_type": "human_followup",
            "reasons": reasons
            + [
                "Quote inquiry; caller is not requesting a booking"
                if facts["request_intent"] == "quote_inquiry"
                else "Message for team; caller is not requesting a booking"
            ],
        }

    units = facts.get("affected_units") or 1
    needs_clarification = units > 4 and not facts.get("scope_confirmed")
    large_scope = units > 4 or facts.get("building_wide")
    review = issue in ("installation", "replacement", "ductwork") or large_scope
    if facts.get("authorized_to_schedule") is False:
        review = True
        reasons.append("Scheduling authorization needs review")
    if review:
        reasons.append("Project or building scope needs dispatch review")
    return {
        "priority": priority,
        "booking_type": "review_first" if review else "direct",
        "reasons": reasons
        or ["Routine maintenance" if priority == "routine" else "Diagnostic visit"],
        "needs_scope_clarification": needs_clarification,
    }


def missing_email_fields(facts: dict) -> list[str]:
    """Refusal or an unconfirmed address cannot satisfy mandatory contact email."""
    missing = []
    if not facts.get("email") or facts.get("email_declined"):
        missing.append("email")
    if not facts.get("email_confirmed") or facts.get("email_declined"):
        missing.append("email_confirmed")
    return missing


def missing_booking_fields(facts: dict) -> list[str]:
    """List only the facts needed to responsibly book a visit."""
    missing = []
    for key in ("name", "phone", "raw_address", "issue_description"):
        if not facts.get(key):
            missing.append(key)
    if not facts.get("callback_confirmed"):
        missing.append("callback_confirmed")
    if facts.get("property_category") not in ("residential", "commercial"):
        missing.append("property_category")
    if facts.get("authorized_to_schedule") is not True:
        missing.append("authorized_to_schedule")
    if not facts.get("postal_code"):
        missing.append("postal_code")
    if facts.get("in_service_area") is not True:
        missing.append("service_area")
    if not facts.get("address_confirmed"):
        missing.append("address_confirmed")
    return missing + missing_email_fields(facts)
