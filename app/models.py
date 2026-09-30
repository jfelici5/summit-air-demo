"""Structured caller facts. Unmentioned facts remain unknown."""

import re
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Issue(StrEnum):
    NO_HEAT = "no_heat"
    NO_AC = "no_ac"
    POOR_COOLING = "poor_cooling"
    MAINTENANCE = "maintenance"
    THERMOSTAT = "thermostat"
    NOISE = "noise"
    WATER_LEAK = "water_leak"
    INSTALLATION = "installation"
    REPLACEMENT = "replacement"
    DUCTWORK = "ductwork"
    OTHER = "other"
    UNKNOWN = "unknown"


class Hazard(StrEnum):
    GAS = "gas_smell"
    CO = "carbon_monoxide"
    SMOKE = "smoke"
    FIRE = "fire"
    BURNING = "electrical_burning"
    SPARKING = "sparking"


class PropertyCategory(StrEnum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    UNKNOWN = "unknown"


class FactsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=24)
    callback_confirmed: bool | None = None
    email: EmailStr | None = None
    email_confirmed: bool | None = None
    email_declined: bool | None = None
    postal_code: str | None = None
    raw_address: str | None = Field(default=None, max_length=400)
    unit: str | None = Field(default=None, max_length=80)
    address_confirmed: bool | None = None
    property_category: PropertyCategory | None = None
    property_type: str | None = Field(
        default=None,
        max_length=80,
        description="Secondary site type, such as warehouse, office, restaurant, retail, "
        "hotel, school, medical facility, industrial, or another caller-described type.",
    )
    business_name: str | None = Field(default=None, max_length=160)
    onsite_contact_name: str | None = Field(default=None, max_length=160)
    onsite_contact_phone: str | None = Field(default=None, max_length=24)
    onsite_contact_phone_confirmed: bool | None = None
    issue_type: Issue | None = None
    issue_description: str | None = Field(default=None, max_length=2000)
    request_intent: Literal["service_visit", "quote_inquiry", "team_message"] | None = None
    team_message: str | None = Field(default=None, max_length=2000)
    system_type: str | None = Field(default=None, max_length=80)
    affected_units: int | None = Field(default=None, ge=1, le=10000)
    scope_confirmed: bool | None = None
    building_wide: bool | None = None
    elderly_present: bool | None = None
    medical_risk_present: bool | None = None
    dangerously_cold: bool | None = None
    dangerously_hot: bool | None = None
    winter_reported: bool | None = None
    reported_temperature_f: float | None = Field(default=None, ge=-100, le=160)
    hazards: list[Hazard] | None = None
    safety_cleared: bool | None = None
    safety_clearance_notes: str | None = Field(default=None, max_length=500)
    safe_location: bool | None = None
    human_requested: bool | None = None
    authorized_to_schedule: bool | None = None
    availability_notes: str | None = Field(default=None, max_length=500)
    access_notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Caller-supplied access instructions: floor/suite, front desk, entrance, "
        "building code, parking/loading access, or site access hours; no special access is valid.",
    )

    @field_validator("postal_code")
    @classmethod
    def normalize_postal_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", value):
            raise ValueError("Please provide all five digits of the service ZIP code")
        return value[:5]

    @field_validator("phone", "onsite_contact_phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        """Normalize US contact numbers; reject invented or incomplete numbers."""
        if value is None:
            return None
        digits = "".join(c for c in value if c.isdigit())
        if len(digits) == 10:
            digits = "1" + digits
        if len(digits) != 11 or not digits.startswith("1"):
            raise ValueError("Provide the complete US callback number, including area code")
        if digits[1] in "01" or digits[4] in "01":
            raise ValueError("Please confirm the area code and exchange")
        return "+" + digits


class AvailabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    time_of_day: str = Field(default="any", pattern="^(any|morning|afternoon)$")


class BookingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: str = Field(min_length=1, max_length=100)
    caller_confirmed: bool


class EscalationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)
    hazards: list[Hazard] = Field(default_factory=list)
    safe_location: bool | None = None
