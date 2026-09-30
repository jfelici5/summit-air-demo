"""Authenticated Retell tools, provider webhooks, and a private dispatch view."""

import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from retell.lib.webhook_auth import verify
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.database import Appointment, ServiceRequest, WebhookEvent, load_request, make_database
from app.engine import CallEngine
from app.models import AvailabilityInput, BookingInput, EscalationInput, FactsPatch
from app.providers import Providers, parse_timestamp

logger = logging.getLogger(__name__)
app_logger = logging.getLogger("app")
if not app_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    app_logger.addHandler(handler)
app_logger.setLevel(logging.INFO)
app_logger.propagate = False


def create_app(settings: Settings | None = None, provider_override=None) -> FastAPI:
    """Build the app; dependency overrides permit failure and safety tests without live accounts."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.environment == "production":
            if not settings.retell_api_key or not settings.admin_api_key:
                raise RuntimeError("Production requires RETELL_API_KEY and ADMIN_API_KEY")
            if settings.database_url.startswith("sqlite"):
                raise RuntimeError("Use persistent Postgres for production")
        engine, sessions = make_database(settings.database_url)
        async with httpx.AsyncClient() as client:
            providers = provider_override or Providers(settings, client)
            app.state.sessions = sessions
            app.state.engine = CallEngine(settings, sessions, providers)
            yield
        engine.dispose()

    app = FastAPI(title="Summit Air Phone Agent", lifespan=lifespan)

    @app.get("/health")
    def health():
        with app.state.sessions() as session:
            session.execute(select(1))
        return {"status": "ok"}

    async def verified_body(request: Request) -> tuple[bytes, dict]:
        body = await request.body()
        if len(body) > 2_000_000:
            raise HTTPException(413, "Request too large")
        signature = request.headers.get("x-retell-signature", "")
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Invalid JSON") from exc
        if (
            not settings.retell_signing_key
            or not signature
            or not verify(decoded, settings.retell_signing_key, signature)
        ):
            logger.warning(
                "retell_auth_rejected path=%s signature_present=%s dedicated_signing_key=%s",
                request.url.path,
                bool(signature),
                bool(settings.retell_webhook_api_key),
            )
            raise HTTPException(401, "Invalid Retell signature")
        try:
            return body, json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid JSON") from exc

    @app.post("/tools/{tool_name}")
    async def tools(tool_name: str, request: Request):
        _, payload = await verified_body(request)
        call = payload.get("call", {})
        if not isinstance(call, dict) or not isinstance(call.get("call_id"), str):
            raise HTTPException(400, "Signed Retell call metadata is required")
        if settings.retell_agent_id and call.get("agent_id") != settings.retell_agent_id:
            raise HTTPException(403, "Unexpected agent")
        call_id = call["call_id"]
        args = payload.get("args", {})
        engine = app.state.engine
        try:
            if tool_name == "update_request":
                result = await engine.update_request(call, FactsPatch.model_validate(args))
            elif tool_name == "find_openings":
                result = await engine.find_openings(call_id, AvailabilityInput.model_validate(args))
            elif tool_name == "book_selected":
                result = await engine.book_selected(call_id, BookingInput.model_validate(args))
            elif tool_name == "escalate_concern":
                result = await engine.escalate(call, EscalationInput.model_validate(args))
            elif tool_name == "get_request":
                result = engine.get_request(call_id)
            elif tool_name == "send_inquiry_email":
                result = await engine.send_inquiry_email(call_id)
            else:
                raise HTTPException(404, "Unknown tool")
        except ValidationError as exc:
            # Do not echo input values: caller details may be sensitive.
            return {
                "status": "needs_correction",
                "saved": False,
                "errors": [
                    {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                    for e in exc.errors()
                ],
                "next_action": "ask_only_for_the_invalid_field",
            }
        except ValueError as exc:
            return {"status": "needs_correction", "message": str(exc)}
        except SQLAlchemyError:
            logger.error("persistence_failure call_id=%s tool=%s", call_id, tool_name)
            return JSONResponse(
                status_code=503,
                content={
                    "saved": False,
                    "next_action": "explain_system_error",
                    "message": "Could not confirm details or booking. "
                    "Do not claim saved or booked.",
                },
            )
        logger.info("tool_completed call_id=%s tool=%s", call_id, tool_name)
        return result

    @app.post("/webhooks/retell")
    async def retell_webhook(request: Request):
        body, payload = await verified_body(request)
        event = payload.get("event")
        call = payload.get("call", {})
        call_id = call.get("call_id")
        if not call_id or event not in ("call_started", "call_ended", "call_analyzed"):
            return {"received": True}
        event_id = hashlib.sha256(body).hexdigest()
        with app.state.sessions() as session:
            if session.get(WebhookEvent, event_id):
                return {"received": True}
            record = load_request(session, call_id)
            if not record:
                record = ServiceRequest(call_id=call_id, facts={})
                session.add(record)
            record.call_status = call.get("call_status")
            summary = (call.get("call_analysis") or {}).get("call_summary")
            if summary:
                record.call_summary = summary[:5000]
            if event == "call_ended" and record.status == "intake":
                record.status = "incomplete_followup" if record.facts.get("phone") else "abandoned"
            session.add(WebhookEvent(id=event_id, provider="retell"))
            session.commit()
        return {"received": True}

    @app.post("/webhooks/cal")
    async def cal_webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("x-cal-signature-256", "")
        expected = hmac.new(settings.cal_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not settings.cal_webhook_secret or not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid Cal.com signature")
        payload = json.loads(body)
        event = payload.get("triggerEvent")
        data = payload.get("payload", {})
        event_id = hashlib.sha256(body).hexdigest()
        with app.state.sessions() as session:
            if session.get(WebhookEvent, event_id):
                return {"received": True}
            uid = data.get("uid")
            appointment = session.scalar(select(Appointment).where(Appointment.cal_uid == uid))
            if not appointment and data.get("rescheduleUid"):
                appointment = session.scalar(
                    select(Appointment).where(Appointment.cal_uid == data["rescheduleUid"])
                )
            if appointment:
                record = session.get(ServiceRequest, appointment.request_id)
                if event == "BOOKING_CANCELLED":
                    appointment.status = "cancelled"
                    record.status = "cancelled"
                elif event in ("BOOKING_RESCHEDULED", "BOOKING_CREATED"):
                    # Providers can deliver CREATED after CANCELLED. Never resurrect
                    # a cancelled reservation just because its initial event arrived late.
                    cancelled = appointment.status == "cancelled" or record.status == "cancelled"
                    if event == "BOOKING_CREATED" and cancelled:
                        appointment.status = "cancelled"
                    else:
                        appointment.cal_uid = uid
                        appointment.start = parse_timestamp(data["startTime"])
                        appointment.end = parse_timestamp(data["endTime"])
                        appointment.status = data.get("status", "accepted").lower()
                        if not appointment.dispatch_hold:
                            record.status = (
                                "booked_urgent" if record.priority == "urgent" else "booked"
                            )
                session.add(WebhookEvent(id=event_id, provider="cal"))
                session.commit()
        return {"received": True}

    def check_admin(request: Request):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not settings.admin_api_key or not hmac.compare_digest(supplied, settings.admin_api_key):
            raise HTTPException(401, "Admin access required")

    @app.get("/admin/requests")
    def requests(request: Request):
        check_admin(request)
        with app.state.sessions() as session:
            records = session.scalars(
                select(ServiceRequest).order_by(ServiceRequest.created_at.desc()).limit(100)
            )
            result = [
                app.state.engine.snapshot(
                    r,
                    session.scalar(select(Appointment).where(Appointment.request_id == r.id)),
                )
                for r in records
            ]
        order = {"emergency": 0, "urgent": 1, "standard": 2, "routine": 3}
        return {"requests": sorted(result, key=lambda r: order.get(r["priority"], 4))}

    @app.get("/dispatch", response_class=HTMLResponse)
    def dispatch():
        return (Path(__file__).parent / "dispatch.html").read_text()

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (
            "<h1>Summit Air</h1><p>Service intake and scheduling API.</p>"
            '<a href="/dispatch">Dispatch view</a>'
        )

    return app


app = create_app()
