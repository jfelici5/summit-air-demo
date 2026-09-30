# Summit Air phone agent

AI phone agent for inbound residential and commercial HVAC calls

**Live number: +1 (925) 433-7863**
**Valid zipcodes: app/service_zipcodes.json**

## Architecture

Retell manages the phone connection, transcription, voice, and conversation.
Its prompt extracts caller facts and invokes backend tools. The Python backend
persists those facts, evaluates service coverage and urgency, and controls
availability and booking. Tool results determine what the agent can confirm.

| Component | Responsibility |
| --- | --- |
| [Agent prompt](prompts/summit_air_agent.md) | Conversation flow, clarification, safety guidance, pricing, and closing. |
| [Tool schema](prompts/tools.json) and [agent configuration](prompts/agent-config.json) | Published Retell functions and voice configuration. |
| [API](app/main.py) | Signed Retell tools/events, signed Cal.com webhooks, health endpoint, and authenticated dispatch API. |
| [Call engine](app/engine.py) | Saved call state, corrections, booking claims, uncertainty handling, and inquiry acknowledgments. |
| [Triage](app/triage.py) | Deterministic safety, urgency, scope routing, and required booking fields. |
| [Caller models](app/models.py) | Validated fact patches, contact numbers, email, and tool arguments. |
| [Coverage](app/coverage.py) | Exact membership in the bundled service ZIP allowlist. |
| [Provider adapters](app/providers.py) | Weather context, real Cal.com windows/bookings, and inquiry-email requests. |
| [Database](app/database.py) | Durable requests, appointment snapshots, webhook deduplication, and email-attempt records. |
| [Dispatch page](app/dispatch.html) | Team view of saved requests, urgency, bookings, messages, and commercial details. |
| [Gmail sender](scripts/gmail_inquiry_sender.gs) | Authenticated inquiry acknowledgments from the helpdesk Gmail account. |

## Call flow

The normal booking flow is roughly modeled after the following steps:
1. Identify the issue and assess immediate risk
2. Gather information about the caller, verify whether their address is in scope (project spec mentioned three counties)
3. Gather information about the property (residential or commercial, whether there are special access rules, etc) and affected systems
4. Authorize scheduling, send confirmations to the users, and loop back asking if there is anything else that the agent can do

Quote inquiries and team messages use a shorter path without booking. The
caller's message and confirmed contact details are saved for team review; the
configured Gmail sender can send an acknowledgment. 

## State and booking integrity

Each call has one durable `ServiceRequest`. Fact patches preserve unknown values
and increment its revision. Safety and vulnerability are saved before completing
contact intake. Backend results expose priority, routing, missing fields, service
coverage, and the next action.

Availability comes from Cal.com and is stored with opaque slot IDs. Booking
requires complete intake, a future offered slot, and explicit caller agreement.
An atomic database claim and unique appointment per request prevent overlapping
or repeated booking attempts from creating duplicate reservations. An uncertain
provider response blocks blind retries and preserves the request for review.

Appointments retain the facts sent at booking. Later changes to contact,
location, or commercial site details preserve that snapshot and flag team review;
they do not silently rewrite the external appointment. Signed Cal.com lifecycle
events update local appointment status and are deduplicated. Inquiry emails also
have one persisted attempt per request to prevent duplicate acknowledgments.

## Routing and operating assumptions

Gas smell, carbon monoxide concerns, smoke, fire, burning, and sparking override
intake with immediate safety guidance and block routine booking. No heat in cold
conditions and cooling outages with vulnerable occupants or dangerous heat get
urgent priority. Urgency is a saved dispatch flag; it does not guarantee an
arrival time or automatically page a technician.

Ordinary residential and commercial service can book directly. Installations,
replacements, ductwork, large or building-wide scope, and unresolved authorization
require team review. Outside-area ZIPs stop booking intake immediately.

The demo covers Nassau, Suffolk, and Queens counties in New York using 283 bundled
ZIPs. Street details are caller supplied and confirmed by readback; street lookup
is not a booking gate. [ZIP data](app/service_zipcodes.json) is attributed to
[GeoNames](https://www.geonames.org/) under CC BY 4.0. Weather uses approximate ZIP
coordinates as supporting context but can be overridden by whatever the user says

