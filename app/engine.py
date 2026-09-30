"""Call-state updates and guarded scheduling actions."""

import logging
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.coverage import check_service_zip, postal_code_from_address
from app.database import Appointment, InquiryEmail, ServiceRequest, load_request, utcnow
from app.models import AvailabilityInput, BookingInput, EscalationInput, FactsPatch
from app.providers import ProviderError, Providers, local_window, parse_timestamp
from app.triage import classify, missing_booking_fields, missing_email_fields

logger = logging.getLogger(__name__)

# These details were sent with the external appointment. Corrections require review.
BOOKING_DETAIL_FIELDS = (
    "name",
    "phone",
    "raw_address",
    "unit",
    "postal_code",
    "property_category",
    "property_type",
    "business_name",
    "onsite_contact_name",
    "onsite_contact_phone",
    "access_notes",
)


class CallEngine:
    def __init__(self, settings: Settings, sessions, providers: Providers):
        self.settings = settings
        self.sessions = sessions
        self.providers = providers

    def save_classification(self, record: ServiceRequest):
        """Reclassify current facts; retain urgency in the dispatch record after booking."""
        result = classify(record.facts, self.settings, utcnow())
        record.priority = result["priority"]
        record.booking_type = result["booking_type"]
        record.reasons = result["reasons"]
        record.updated_at = utcnow()
        if record.priority == "emergency":
            record.status = "safety_escalation"
            record.followup_reason = "; ".join(record.reasons)
        elif record.booking_type in ("review_first", "human_followup"):
            record.status = "needs_review"
            record.followup_reason = "; ".join(record.reasons)
        elif record.facts.get("in_service_area") is False:
            record.status = "outside_service_area"
        elif record.booking_state == "booked" and record.status in (
            "needs_review",
            "pending_confirmation",
            "cancelled",
        ):
            pass
        elif record.priority == "urgent":
            record.status = (
                "booked_urgent" if record.booking_state == "booked" else "urgent_followup"
            )
            record.followup_reason = "; ".join(record.reasons)
        elif record.booking_state == "booked":
            record.status = "booked"
        elif record.booking_state == "idle" and record.status not in ("needs_followup", "ended"):
            record.status = "intake"
        return result

    def snapshot(self, record: ServiceRequest, appointment: Appointment | None = None) -> dict:
        """Give the voice model a compact, authoritative view and honest next steps."""
        classification = classify(record.facts, self.settings, utcnow())
        missing_followup = [
            key for key in ("name", "phone", "callback_confirmed") if not record.facts.get(key)
        ] + missing_email_fields(record.facts)
        action = "collect_missing_details"
        if record.priority == "emergency":
            action = "safety_guidance_first"
        elif record.facts.get("in_service_area") is False:
            action = "explain_outside_service_area"
        elif record.booking_type == "human_followup":
            action = "collect_followup_details" if missing_followup else "confirm_team_review"
        elif record.booking_state in ("submitting", "unknown"):
            action = "booking_unconfirmed_do_not_retry"
        elif appointment:
            action = (
                "confirm_booking" if appointment.status == "accepted" else "confirm_pending_request"
            )
            if appointment.dispatch_hold:
                action = "confirm_team_review"
            if appointment.status == "cancelled":
                action = "appointment_cancelled"
        elif (
            record.facts.get("zip_status") != "checked"
            or record.facts.get("in_service_area") is not True
        ):
            action = "collect_name" if not record.facts.get("name") else "check_service_zip"
        elif record.booking_type == "review_first":
            action = "collect_followup_details" if missing_followup else "confirm_team_review"
        elif not record.facts.get("name"):
            action = "collect_name"
        elif not record.facts.get("raw_address") or not record.facts.get("address_confirmed"):
            action = "collect_service_address"
        elif not record.facts.get("callback_confirmed"):
            action = "confirm_callback"
        elif record.facts.get("property_category") not in ("residential", "commercial"):
            action = "confirm_property_category"
        elif record.facts.get("authorized_to_schedule") is None:
            action = "confirm_authorization"
        elif missing_email_fields(record.facts):
            action = "collect_confirmation_email"
        elif not missing_booking_fields(record.facts):
            action = "check_availability"
        result = {
            "request_id": record.id,
            "revision": record.revision,
            "priority": record.priority,
            "booking_type": record.booking_type,
            "priority_reasons": record.reasons,
            "status": record.status,
            "followup_reason": record.followup_reason,
            "facts": record.facts,
            "missing_fields": missing_booking_fields(record.facts),
            "missing_followup_fields": missing_followup,
            "inquiry_email_enabled": self.settings.inquiry_email_enabled,
            "needs_scope_clarification": classification.get("needs_scope_clarification", False),
            "next_action": action,
            "saved": True,
            "current_datetime": utcnow()
            .astimezone(ZoneInfo(self.settings.service_timezone))
            .isoformat(),
            "timezone": self.settings.service_timezone,
            "service_coverage": {
                "basis": "zip_code",
                "postal_code": record.facts.get("postal_code"),
                "counties": sorted(c.title() for c in self.settings.counties),
                "state": self.settings.service_state,
                "verified": record.facts.get("zip_status") == "checked",
                "eligible": record.facts.get("in_service_area"),
                "intake_can_continue": (
                    record.facts.get("zip_status") == "checked"
                    and record.facts.get("in_service_area") is True
                ),
            },
        }
        if appointment:
            result["appointment"] = {
                "uid": appointment.cal_uid,
                "status": appointment.status,
                "start": appointment.start.isoformat(),
                "end": appointment.end.isoformat(),
                "email_requested": appointment.email_requested,
                "email_delivery_verified": False,
                "dispatch_hold": appointment.dispatch_hold,
                "booked_address": appointment.booked_facts.get("formatted_address"),
                "booked_callback": appointment.booked_facts.get("phone"),
                "booked_business_name": appointment.booked_facts.get("business_name"),
                "booked_property_type": appointment.booked_facts.get("property_type"),
                "booked_onsite_contact_name": appointment.booked_facts.get("onsite_contact_name"),
                "booked_onsite_contact_phone": appointment.booked_facts.get("onsite_contact_phone"),
                "booked_access_notes": appointment.booked_facts.get("access_notes"),
            }
        return result

    async def update_request(self, call: dict, patch: FactsPatch) -> dict:
        """Persist caller details and ZIP eligibility; street matching is never a gate."""
        call_id = call["call_id"]
        changes = patch.model_dump(mode="json", exclude_unset=True)
        if "raw_address" in changes and "postal_code" not in changes:
            embedded_zip = postal_code_from_address(changes["raw_address"])
            if embedded_zip:
                changes["postal_code"] = embedded_zip
        with self.sessions() as session:
            record = session.scalar(
                select(ServiceRequest).where(ServiceRequest.call_id == call_id).with_for_update()
            )
            if not record:
                record = ServiceRequest(call_id=call_id, facts={})
                session.add(record)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    record = load_request(session, call_id)
            facts = dict(record.facts)
            caller_id = call.get("from_number")
            if not facts.get("phone") and caller_id:
                try:
                    facts["phone"] = FactsPatch(phone=caller_id).phone
                except ValueError:
                    pass
            address_changed = "raw_address" in changes and changes["raw_address"] != facts.get(
                "raw_address"
            )
            previous_zip = facts.get("postal_code")
            zip_changed = "postal_code" in changes and changes["postal_code"] != previous_zip
            if "phone" in changes and changes["phone"] != facts.get("phone"):
                facts["callback_confirmed"] = False
            if any(
                key in changes and changes[key] != facts.get(key)
                for key in ("onsite_contact_name", "onsite_contact_phone")
            ):
                facts["onsite_contact_phone_confirmed"] = False
            if "email" in changes and changes["email"] != facts.get("email"):
                facts["email_confirmed"] = False
                if changes["email"]:
                    facts["email_declined"] = False
            if address_changed:
                facts.update(location_status="caller_supplied", address_confirmed=False)
                record.offered_slots = []
            if zip_changed:
                for key in (
                    "county",
                    "zip_city",
                    "state",
                    "latitude",
                    "longitude",
                    "coordinates_basis",
                    "outside_temp_f",
                    "weather_status",
                ):
                    facts.pop(key, None)
                facts["address_confirmed"] = False
                record.offered_slots = []
            facts.update(changes)
            facts.update(check_service_zip(facts.get("postal_code"), self.settings))
            if facts.get("raw_address"):
                # A readable Cal address is caller supplied; this is not address verification.
                address = facts["raw_address"].strip()
                embedded_zip = postal_code_from_address(address)
                if facts.get("postal_code"):
                    if not embedded_zip:
                        address += ", " + facts["postal_code"]
                    elif embedded_zip != facts["postal_code"]:
                        address = re.sub(
                            r"[0-9]{5}(?:-[0-9]{4})?\s*$", facts["postal_code"], address
                        )
                facts.update(formatted_address=address, location_status="caller_supplied")
            else:
                facts.pop("formatted_address", None)
                facts.pop("location_status", None)
            if changes.get("hazards"):
                facts["safety_latched"] = True
            record.facts = facts
            record.revision += 1
            self.save_classification(record)
            appointment = session.scalar(
                select(Appointment).where(Appointment.request_id == record.id)
            )
            if appointment:
                contact_changed = any(
                    record.facts.get(key) != appointment.booked_facts.get(key)
                    for key in BOOKING_DETAIL_FIELDS
                )
                if facts.get("safety_latched") or contact_changed:
                    appointment.dispatch_hold = True
                if contact_changed and record.priority != "emergency":
                    record.status = "needs_review"
                    record.followup_reason = (
                        "Contact, location, or site details changed after booking; "
                        "update external appointment"
                    )
            session.commit()
            should_get_weather = (
                zip_changed
                and facts.get("in_service_area") is True
                and not facts.get("safety_latched")
            )
            postal_code = facts.get("postal_code")
            latitude, longitude = facts.get("latitude"), facts.get("longitude")

        if should_get_weather:
            weather = await self.providers.weather(latitude, longitude)
            with self.sessions() as session:
                record = session.scalar(
                    select(ServiceRequest)
                    .where(ServiceRequest.call_id == call_id)
                    .with_for_update()
                )
                if record.facts.get("postal_code") == postal_code:
                    record.facts = {**record.facts, **weather}
                    self.save_classification(record)
                    session.commit()
        return self.get_request(call_id)

    def get_request(self, call_id: str) -> dict:
        """Recover persisted state after interruption or an uncertain tool response."""
        with self.sessions() as session:
            record = load_request(session, call_id)
            if not record:
                return {
                    "saved": False,
                    "next_action": "save_caller_details",
                    "facts": {},
                    "current_datetime": utcnow()
                    .astimezone(ZoneInfo(self.settings.service_timezone))
                    .isoformat(),
                }
            appointment = session.scalar(
                select(Appointment).where(Appointment.request_id == record.id)
            )
            state = self.snapshot(record, appointment)
            email = session.get(InquiryEmail, record.id)
            if email:
                state["inquiry_email"] = {
                    "status": email.status,
                    "recipient": email.recipient,
                    "delivery_verified": False,
                }
            return state

    async def send_inquiry_email(self, call_id: str) -> dict:
        """Acknowledge a saved inquiry once, to the confirmed recipient from that call."""
        state = self.get_request(call_id)
        facts = state.get("facts", {})
        previous = state.get("inquiry_email")
        if previous:
            status = previous["status"]
            if previous["recipient"] != facts.get("email"):
                status = "previous_recipient_requires_review"
            return {**state, "email_status": status}
        if not self.settings.inquiry_email_enabled:
            return {**state, "email_status": "not_configured"}
        if (
            not state.get("saved")
            or state.get("booking_type") != "human_followup"
            or state.get("appointment")
            or state.get("missing_followup_fields")
            or facts.get("request_intent") not in ("quote_inquiry", "team_message")
            or not facts.get("team_message")
            or not facts.get("email")
            or not facts.get("email_confirmed")
            or facts.get("email_declined")
        ):
            return {**state, "email_status": "not_ready"}
        request_id = state["request_id"]
        with self.sessions() as session:
            email = InquiryEmail(
                request_id=request_id, recipient=facts["email"], status="submitting"
            )
            session.add(email)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                email = session.get(InquiryEmail, request_id)
                return {**self.get_request(call_id), "email_status": email.status}
        try:
            await self.providers.send_inquiry_email(request_id, facts["email"], facts["name"])
            status = "accepted"
        except ProviderError as exc:
            status = "unknown" if exc.uncertain else "failed"
        with self.sessions() as session:
            email = session.get(InquiryEmail, request_id)
            email.status = status
            session.commit()
        logger.info("inquiry_email_result request_id=%s status=%s", request_id, status)
        return {**self.get_request(call_id), "email_status": status}

    async def find_openings(self, call_id: str, arguments: AvailabilityInput) -> dict:
        """Offer only provider-returned future slots; a failed lookup produces a saved follow-up."""
        state = self.get_request(call_id)
        if not state.get("saved") or state["booking_type"] != "direct" or state["missing_fields"]:
            return {**state, "slots": [], "availability_status": "not_ready"}
        if (
            state.get("appointment")
            or state.get("next_action") == "booking_unconfirmed_do_not_retry"
        ):
            return {**state, "slots": [], "availability_status": "existing_booking"}
        start, end = local_window(
            arguments.start_date, arguments.end_date, self.settings.service_timezone
        )
        try:
            slots = await self.providers.available_slots(start, end)
        except ProviderError:
            self.save_followup(
                call_id, "Availability lookup failed; dispatch should confirm a time"
            )
            return {**self.get_request(call_id), "slots": [], "availability_status": "unavailable"}
        zone = ZoneInfo(self.settings.service_timezone)
        selected = []
        for slot in slots:
            local_start = parse_timestamp(slot["start"]).astimezone(zone)
            if arguments.time_of_day == "morning" and local_start.hour >= 12:
                continue
            if arguments.time_of_day == "afternoon" and local_start.hour < 12:
                continue
            selected.append(
                {
                    **slot,
                    "slot_id": str(uuid4()),
                    "display": local_start.strftime("%A, %B %-d at %-I:%M %p"),
                    "timezone": self.settings.service_timezone,
                }
            )
            if len(selected) == 3:
                break
        with self.sessions() as session:
            record = load_request(session, call_id)
            if record.revision != state["revision"] or record.booking_type != "direct":
                return {
                    **self.snapshot(record),
                    "slots": [],
                    "availability_status": "context_changed",
                }
            record.offered_slots = selected
            if not selected:
                record.followup_reason = (
                    "No matching appointment openings; collect alternate availability"
                )
            session.commit()
        return {
            **self.get_request(call_id),
            "slots": selected,
            "availability_status": "available" if selected else "no_matching_slots",
        }

    def save_followup(self, call_id: str, reason: str):
        """Persist an operational follow-up; never promise a response time."""
        with self.sessions() as session:
            record = load_request(session, call_id)
            if record:
                record.followup_reason = reason
                if record.priority != "emergency":
                    record.status = (
                        "urgent_followup" if record.priority == "urgent" else "needs_followup"
                    )
                record.updated_at = utcnow()
                session.commit()

    async def escalate(self, call: dict, args: EscalationInput) -> dict:
        """Create a safety or human-review record even when caller details are incomplete."""
        patch = {"safe_location": args.safe_location}
        if args.hazards:
            patch["hazards"] = args.hazards
        else:
            patch["human_requested"] = True
        await self.update_request(call, FactsPatch(**patch))
        self.save_followup(call["call_id"], args.reason)
        with self.sessions() as session:
            record = load_request(session, call["call_id"])
            appointment = session.scalar(
                select(Appointment).where(Appointment.request_id == record.id)
            )
            if appointment and args.hazards:
                appointment.dispatch_hold = True
                session.commit()
        return self.get_request(call["call_id"])

    async def book_selected(self, call_id: str, args: BookingInput) -> dict:
        """Claim one booking attempt atomically; never repeat an uncertain mutation."""
        with self.sessions() as session:
            record = load_request(session, call_id)
            if not record:
                return {"booking_status": "not_ready", "saved": False}
            appointment = session.scalar(
                select(Appointment).where(Appointment.request_id == record.id)
            )
            if appointment:
                return {**self.snapshot(record, appointment), "booking_status": appointment.status}
            if record.booking_state != "idle":
                return {**self.snapshot(record), "booking_status": "unconfirmed_do_not_retry"}
            self.save_classification(record)
            if (
                record.booking_type != "direct"
                or missing_booking_fields(record.facts)
                or not args.caller_confirmed
            ):
                session.commit()
                return {**self.snapshot(record), "booking_status": "not_ready"}
            slot = next((s for s in record.offered_slots if s["slot_id"] == args.slot_id), None)
            if not slot or parse_timestamp(slot["start"]) <= utcnow():
                return {**self.snapshot(record), "booking_status": "slot_expired_check_again"}
            operation_id = str(uuid4())
            claimed = session.execute(
                update(ServiceRequest)
                .where(
                    ServiceRequest.id == record.id,
                    ServiceRequest.booking_state == "idle",
                    ServiceRequest.revision == record.revision,
                )
                .values(booking_state="submitting", booking_operation_id=operation_id)
            )
            if claimed.rowcount != 1:
                session.rollback()
                return {**self.get_request(call_id), "booking_status": "unconfirmed_do_not_retry"}
            facts, request_id = dict(record.facts), record.id
            session.commit()

        try:
            booking = await self.providers.book(facts, slot, operation_id, request_id)
        except ProviderError as exc:
            with self.sessions() as session:
                record = load_request(session, call_id)
                record.booking_state = "unknown" if exc.uncertain else "idle"
                record.offered_slots = []
                record.followup_reason = (
                    "Booking outcome needs review"
                    if exc.uncertain
                    else "Booking rejected; confirm another opening"
                )
                if record.priority != "emergency":
                    record.status = "needs_followup"
                session.commit()
            return {
                **self.get_request(call_id),
                "booking_status": "unconfirmed" if exc.uncertain else "rejected",
            }

        with self.sessions() as session:
            record = load_request(session, call_id)
            contact_changed = any(
                record.facts.get(key) != facts.get(key) for key in BOOKING_DETAIL_FIELDS
            )
            appointment = Appointment(
                request_id=request_id,
                cal_uid=booking["uid"],
                start=parse_timestamp(booking["start"]),
                end=parse_timestamp(booking["end"]),
                status=booking["status"],
                email_requested=booking["email_requested"],
                dispatch_hold=record.priority == "emergency" or contact_changed,
                booked_facts=facts,
            )
            session.add(appointment)
            record.booking_state = "booked"
            self.save_classification(record)
            if booking["status"] != "accepted":
                record.status = "pending_confirmation"
            if contact_changed and record.priority != "emergency":
                record.status = "needs_review"
                record.followup_reason = (
                    "Contact or location changed while booking; update external appointment"
                )
            session.commit()
            logger.info("booking_saved request_id=%s appointment_id=%s", request_id, appointment.id)
            return {**self.snapshot(record, appointment), "booking_status": booking["status"]}
