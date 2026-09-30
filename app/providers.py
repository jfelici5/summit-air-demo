"""Small HTTP adapters. Provider failures never become fabricated facts or appointments."""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """A provider failed; uncertain means a mutation may already have succeeded."""

    def __init__(self, provider: str, uncertain: bool = False):
        self.provider = provider
        self.uncertain = uncertain
        super().__init__(f"{provider} unavailable")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp; reject times lacking a timezone."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A timezone is required")
    return parsed.astimezone(UTC)


class Providers:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def send_inquiry_email(self, request_id: str, recipient: str, name: str) -> None:
        """Use the owner's authenticated Apps Script sender over HTTPS, not blocked SMTP."""
        try:
            response = await self.client.post(
                self.settings.inquiry_email_url,
                json={
                    "token": self.settings.inquiry_email_token,
                    "request_id": request_id,
                    "recipient": recipient,
                    "first_name": name.split()[0] if name.split() else "there",
                },
                follow_redirects=True,
                timeout=self.settings.provider_timeout_seconds,
            )
            response.raise_for_status()
            status = response.json().get("email_status")
            if status != "accepted":
                raise ProviderError(
                    "inquiry_email", uncertain=status not in ("failed", "unauthorized")
                )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "provider_failure provider=inquiry_email error_type=%s", type(exc).__name__
            )
            raise ProviderError("inquiry_email", uncertain=True) from exc

    async def weather(self, latitude: float, longitude: float) -> dict:
        """Read current Open-Meteo temperature; never overwrite caller-reported conditions."""
        if not self.settings.weather_enabled:
            return {"weather_status": "disabled", "outside_temp_f": None}
        try:
            response = await self.client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m",
                    "temperature_unit": "fahrenheit",
                },
                timeout=2.5,
            )
            response.raise_for_status()
            return {
                "weather_status": "available",
                "outside_temp_f": response.json()["current"]["temperature_2m"],
            }
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            logger.warning("provider_failure provider=weather")
            return {"weather_status": "unavailable", "outside_temp_f": None}

    def cal_headers(self, version: str) -> dict:
        """Authenticate to Cal.com using the endpoint's documented version."""
        if not self.settings.cal_api_key or not self.settings.cal_event_type_id:
            raise ProviderError("cal_configuration")
        return {"Authorization": f"Bearer {self.settings.cal_api_key}", "cal-api-version": version}

    async def available_slots(self, start: datetime, end: datetime) -> list[dict]:
        """Fetch real Cal.com slots and discard any past timestamp."""
        try:
            response = await self.client.get(
                "https://api.cal.com/v2/slots",
                headers=self.cal_headers("2024-09-04"),
                params={
                    "eventTypeId": self.settings.cal_event_type_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timeZone": self.settings.service_timezone,
                    "format": "range",
                },
                timeout=self.settings.provider_timeout_seconds,
            )
            response.raise_for_status()
            slots = []
            for day in response.json()["data"].values():
                for slot in day:
                    slot_start = parse_timestamp(slot["start"])
                    slot_end = (
                        parse_timestamp(slot["end"])
                        if slot.get("end")
                        else slot_start + timedelta(minutes=self.settings.appointment_minutes)
                    )
                    if slot_start > datetime.now(UTC) and slot_end > slot_start:
                        slots.append({"start": slot_start.isoformat(), "end": slot_end.isoformat()})
            return sorted(slots, key=lambda s: s["start"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            response = getattr(exc, "response", None)
            logger.warning(
                "provider_failure provider=cal_slots error_type=%s http_status=%s",
                type(exc).__name__,
                getattr(response, "status_code", None),
            )
            raise ProviderError("cal") from exc

    async def book(self, facts: dict, slot: dict, operation_id: str, request_id: str) -> dict:
        """Create a Cal.com booking once; mark transport/server errors as uncertain outcomes."""
        attendee = {
            "name": facts["name"],
            "phoneNumber": facts["phone"],
            "timeZone": self.settings.service_timezone,
            "language": "en",
        }
        if facts.get("email") and facts.get("email_confirmed") and not facts.get("email_declined"):
            attendee["email"] = facts["email"]
        address = facts["formatted_address"]
        if facts.get("unit"):
            address += ", " + facts["unit"]
        notes = [facts.get("issue_description", "")]
        for label, key in (
            ("Property", "property_category"),
            ("Site type", "property_type"),
            ("Business/site", "business_name"),
            ("Onsite contact", "onsite_contact_name"),
            ("Onsite phone", "onsite_contact_phone"),
            ("Access instructions", "access_notes"),
        ):
            if facts.get(key):
                notes.append(f"{label}: {facts[key]}")
        if facts.get("onsite_contact_phone") and not facts.get("onsite_contact_phone_confirmed"):
            notes.append("Onsite phone has not been confirmed; use the confirmed caller callback.")
        payload = {
            "eventTypeId": self.settings.cal_event_type_id,
            "start": parse_timestamp(slot["start"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attendee": attendee,
            "location": {"type": "attendeeAddress", "address": address},
            "bookingFieldsResponses": {"notes": "\n".join(notes)},
            "metadata": {"summit_request_id": request_id, "summit_operation_id": operation_id},
        }
        try:
            response = await self.client.post(
                "https://api.cal.com/v2/bookings",
                json=payload,
                headers=self.cal_headers("2026-02-25"),
                timeout=self.settings.provider_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("cal", uncertain=True) from exc
        if not response.is_success:
            logger.warning(
                "provider_failure provider=cal_booking http_status=%s", response.status_code
            )
            raise ProviderError("cal", uncertain=response.status_code >= 500)
        try:
            booking = response.json()["data"]
            return {
                "uid": booking["uid"],
                "status": booking["status"],
                "start": booking["start"],
                "end": booking["end"],
                "email_requested": "email" in attendee,
            }
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError("cal", uncertain=True) from exc


def local_window(start_date, end_date, timezone: str) -> tuple[datetime, datetime]:
    """Translate a caller's local date range to timezone-aware boundaries."""
    zone = ZoneInfo(timezone)
    today = datetime.now(zone).date()
    first = max(start_date or today, today)
    last = end_date or first + timedelta(days=7)
    if last < first or (last - first).days > 30:
        raise ValueError("Please choose a date range of no more than 30 days")
    return (
        datetime.combine(first, datetime.min.time(), zone),
        datetime.combine(last + timedelta(days=1), datetime.min.time(), zone),
    )
