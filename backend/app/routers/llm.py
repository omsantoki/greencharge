"""LLM endpoints (Phase 7). Three narrow capabilities, never a general chatbot.

    POST /api/llm/extract  {"text": "..."}                 -> ExtractedConstraints or null
    POST /api/llm/explain  {"session_id": 1, "language"?}  -> {"text", "llm_used"}
    POST /api/llm/copilot  {"question": "..."}             -> {"answer", "tools_used"}

Bodies are read with ``parse_json_body``, so they work without a Content-Type header, exactly
like every other POST in this backend.

THE RULE (BUILD_SPEC Phase 7): the LLM never produces a number that appears in the UI.
``/explain`` is where this router carries it: the values are gathered HERE -- the session row,
``accounting.session_impact`` (the same numbers the driver's own screens show), the
orchestrator's latest plan and the cached carbon forecast -- and handed to
``explain.explain_schedule``, which formats them into display strings and lets the model do
nothing but narrate those strings. ``explain.py`` never touches the database, the providers or
the clock, so it cannot compute anything of its own. ``/extract`` performs extraction, not
calculation, and its output is Pydantic-validated by ``extract.py``. ``/copilot`` answers only
from the results of three tools implemented in our own code (``app.llm.copilot``).

Nothing blocks on the LLM:

- No key (or an unsupported provider, or no model name): every endpoint answers 503 with
  ``client.configuration_problem()``, immediately, never a 500 and never a hang.
- ``/extract`` answers 200 with ``null`` when the model fails or the message states nothing; the
  driver UI falls back to the manual form.
- ``/explain`` always answers 200 with text: ``explain_schedule`` falls back to deterministic
  sentences built from these same gathered values when the model fails. ``llm_used`` says which
  of the two wrote them, reported by the code that chose rather than guessed from the string.
- ``/copilot`` is the one endpoint with nothing to show without the model: it answers 503 when
  the model or its tools cannot produce a grounded answer.

Unknown session -> 404. No cached carbon forecast to price the session with -> 503, the same
answer ``GET /api/sessions/{id}/impact`` gives in that state.

Everything that touches the database runs in a worker thread, so a slow database cannot stall
the event loop the CSMS and the tick scheduler share.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.clock import clock
from app.config import settings
from app.db import SessionLocal
from app.llm import client, copilot, explain
from app.llm.extract import ExtractedConstraints, extract_constraints
from app.models import Charger, Session, Site
from app.orchestrator import accounting
from app.orchestrator import loop as orchestrator
from app.providers import ProviderError
from app.providers.base import floor_to_slot, load_cached
from app.routers import parse_json_body

logger = logging.getLogger("greencharge.routers.llm")

router = APIRouter(prefix="/api/llm", tags=["llm"])

MAX_TEXT_CHARS = 2000      # a driver message; extract.py truncates further on its own
# The 422 for an over-long message echoes the rejected value back, so a huge body would be read
# into memory and then mirrored to the client. This is the same limit in declared bytes, with
# room for 2000 characters of Devanagari or Gujarati escaped as \uXXXX plus the JSON around them.
MAX_TEXT_BYTES = 16 * 1024
MAX_QUESTION_CHARS = 500   # an operator question
MAX_LANGUAGE_CHARS = 40


class ExtractRequest(BaseModel):
    """POST /api/llm/extract: one free-text message from a driver."""

    text: str = Field(max_length=MAX_TEXT_CHARS)


class ExplainRequest(BaseModel):
    """POST /api/llm/explain: which session to explain, and optionally in which language.

    ``language`` is only length-limited, not pattern-limited: ``explain.normalize_language``
    maps it through a fixed table ("en"/"hi"/"gu", "हिंदी", "ગુજરાતી", anything unrecognised
    becomes English) and only that normalised name reaches the prompt, so the string a client
    sends is never a free-text channel into the model.
    """

    session_id: int
    language: str | None = Field(default=None, max_length=MAX_LANGUAGE_CHARS)


class ExplainOut(BaseModel):
    """The sentences, and whether the model or our own deterministic text wrote them.

    ``llm_used`` is reported by ``explain.explain_schedule`` at the point it picks an answer, not
    inferred here from the string: the two paths produce prose that looks alike (they quote the
    same computed numbers), so only the code that chose can say truthfully which one ran.
    """

    text: str
    llm_used: bool


class CopilotRequest(BaseModel):
    """POST /api/llm/copilot: one operator question."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class CopilotOut(BaseModel):
    answer: str
    tools_used: list[str]


def _require_llm() -> None:
    """503 with the reason when the LLM layer is switched off or misconfigured."""
    problem = client.configuration_problem()
    if problem is not None:
        raise HTTPException(status_code=503, detail=problem)


# --------------------------------------------------------------------------------------------
# POST /api/llm/extract
# --------------------------------------------------------------------------------------------


@router.post("/extract")
async def llm_extract(request: Request) -> ExtractedConstraints | None:
    """A driver's sentence -> target SoC and deadline, or null when nothing could be read.

    Relative times are resolved against the SIMULATED clock, which is the time the rest of the
    backend runs on. ``extract_constraints`` never raises and never blocks the driver: a failed
    extraction is ``null`` and the UI falls back to the manual form. An oversized body is refused
    on its declared length, before it is read: a driver's sentence is never that big, and the
    validation error for one would echo the whole rejected value back.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_TEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Message is too long; the limit is {MAX_TEXT_CHARS} characters.",
        )
    body = await parse_json_body(request, ExtractRequest)
    _require_llm()
    return await extract_constraints(body.text, clock.now())


# --------------------------------------------------------------------------------------------
# POST /api/llm/explain -- the router gathers computed values; explain.py only narrates them
# --------------------------------------------------------------------------------------------


def _explain_inputs(session_id: int, now: datetime) -> dict[str, Any] | None:
    """The computed values ``explain_schedule`` narrates, or None when there is no such session.

    Nothing is calculated for the explanation here either: the plan comes from the optimizer
    (the orchestrator's latest schedules), the carbon curve from the forecast the grid provider
    cached, and the savings, the energy still needed, the ETA and whether the car actually makes
    its target (``on_time`` with the SoC that projection reaches by the deadline) from
    ``accounting.session_impact`` -- so the sentences quote the numbers the UI already shows.
    ``on_time`` travels with the rest because it decides WHICH sentence is true: the same
    computation the Gantt and the driver's "Ready at" tile read, so a plan that misses the target
    cannot be narrated as a promise.
    Raises ``ProviderError`` when no forecast is cached to price the session with. Blocking
    (database); runs in a worker thread.
    """
    plan = orchestrator.get_latest_schedules().get(session_id)
    with SessionLocal() as db:
        session = db.get(Session, session_id)
        if session is None:
            return None
        impact = accounting.session_impact(db, session, now, plan)
        zone = db.execute(
            select(Site.grid_zone)
            .join(Charger, Charger.site_id == Site.id)
            .where(Charger.id == session.charger_id)
        ).scalar_one_or_none()

    schedule = [(slot_start, kw) for slot_start, kw in (plan["slots"] if plan else [])]
    slot = timedelta(minutes=settings.slot_minutes)
    # The curve has to cover both what it is used for: the carbon intensity of the planned slots,
    # and the cleanest window between now and the deadline.
    starts = [floor_to_slot(now)] + [slot_start for slot_start, _ in schedule]
    ends = [floor_to_slot(session.deadline) + slot] + [
        slot_start + slot for slot_start, _ in schedule
    ]
    points = load_cached(
        zone or settings.electricity_maps_zone, min(starts), max(ends), is_forecast=True
    )
    eta = impact["eta"]
    return {
        "schedule": schedule,
        "carbon": [(point.ts, point.carbon_intensity) for point in points],
        "deadline": session.deadline,
        "co2_saved_g": impact["co2_saved_g"],
        "cost_saved_inr": impact["cost_saved_inr"],
        "energy_needed_kwh": impact["energy_needed_kwh"],
        "target_soc": session.soc_target,
        "eta": None if eta is None else datetime.fromisoformat(eta),
        "on_time": impact["on_time"],
        "projected_soc": impact["projected_soc_at_deadline"],
        "vehicle_model": session.vehicle_model,
        "now": now,
    }


@router.post("/explain", response_model=ExplainOut)
async def llm_explain(request: Request) -> dict[str, Any]:
    """Explain one session's schedule in 2-3 sentences, from values computed here.

    ``explain_schedule`` always returns sentences -- it builds deterministic ones from the same
    values when the model is slow, unreachable or wrong -- so a driver never waits on a failure.
    The response says which of the two wrote them (``llm_used``).
    That guarantee is the whole endpoint: the driver's plan screen renders this text, so anything
    that goes wrong BELOW the model (``accounting`` raising, a plan without its ``slots``, a
    database that has gone away) answers with the deterministic sentences too, never a 500.
    """
    body = await parse_json_body(request, ExplainRequest)
    _require_llm()
    language = body.language or "English"

    try:
        facts = await asyncio.to_thread(_explain_inputs, body.session_id, clock.now())
    except ProviderError as exc:
        logger.warning("Session %d cannot be explained: %s", body.session_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception:
        # None means "no such session" (404 below); {} means "nothing could be gathered", which
        # narrates as the deterministic sentences for a plan with no known details.
        logger.exception("Session %d could not be gathered for an explanation", body.session_id)
        facts = {}
    if facts is None:
        raise HTTPException(status_code=404, detail=f"Session {body.session_id} not found")

    try:
        result = await explain.explain_schedule(**facts, language=language)
    except Exception:  # explain.py owns this guarantee; this is the belt to its braces
        logger.exception("Explaining session %d failed", body.session_id)
        result = explain.Explanation(
            explain.fallback_text(explain.ExplainFacts(), language), llm_used=False
        )
    return {"text": result.text, "llm_used": result.llm_used}


# --------------------------------------------------------------------------------------------
# POST /api/llm/copilot
# --------------------------------------------------------------------------------------------


@router.post("/copilot", response_model=CopilotOut)
async def llm_copilot(request: Request) -> dict[str, Any]:
    """Answer an operator question from the three tools' results, and nothing else.

    Every number in ``answer`` comes from a tool result computed by GreenCharge; the copilot
    refuses an answer that quotes a number the tools did not return.
    """
    body = await parse_json_body(request, CopilotRequest)
    _require_llm()

    try:
        return await copilot.answer_question(body.question)
    except copilot.CopilotError as exc:
        logger.warning("Copilot could not answer: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # the copilot is optional; the operator gets a cause, never a 500
        logger.exception("Copilot failed unexpectedly")
        raise HTTPException(
            status_code=503,
            detail="The copilot could not answer this question (details in the server log).",
        ) from exc
