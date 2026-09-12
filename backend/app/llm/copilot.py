"""Operator copilot: three fixed tools, a model that may only choose between them.

BUILD_SPEC Phase 7 allows EXACTLY three tools and no others::

    query_sessions(filters)
    get_site_performance(site_id, days)
    get_carbon_history(hours)

All three are implemented here, in our own code, against the database and the grid provider.
The model never sees the database, never writes SQL, and never receives a free-form argument:
its tool choice is parsed into ``ToolCall`` (a Literal-discriminated union, so an unknown tool
name cannot validate), its arguments into per-tool Pydantic models with ``extra="forbid"``, and
the chosen name is checked against the ``TOOLS`` whitelist a second time before anything runs.

THE RULE ("the LLM never produces a number that appears in the UI"), enforced structurally:

1. Every number in a tool result is computed here -- from the session rows the OCPP layer and the
   Phase 4 accounting wrote, from ``baseline.site_load_curve`` and from the grid provider -- and
   is rounded here. The model is handed those values already formatted.
2. The system prompt forbids arithmetic, unit conversion and any number that is not in the tool
   results.
3. ``_ungrounded_numbers`` re-checks the answer: every numeric token it contains must match a
   number that was in the payload the model was given, within the rounding the token itself
   implies. The operator's question is NOT a grounding source -- otherwise a question could
   launder its own figure into an answer the UI renders as fact. A first violation is sent back
   once as a correction; a second one fails the request rather than showing an invented number.

   A number the answer puts a UNIT on must also come from a field carrying that unit, so a
   soc_current_pct of 62.4 cannot justify "saved 62.4 kg of CO2".

   Known limit, written down so nobody mistakes it for more than it is: a BARE number is still
   checked by value alone, because hours, days, session counts and ids are not units a field name
   spells out. A small integer the model worked out itself (counting the rows of a list, say) can
   therefore still ground against an unrelated integer such as a session id. Rule 1 of the answer
   prompt forbids counting for exactly that reason; every large invented figure is caught outright.
4. The answer itself is parsed into ``CopilotAnswer``.

Two model round-trips per question: one to pick the tools, one to narrate their results. Both run
at temperature 0 through ``app.llm.client``, which returns None instead of raising, so a failure
here is always ``CopilotError`` -> HTTP 503 in ``app.routers.llm``, never a 500 and never a hang.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.clock import clock
from app.config import settings
from app.db import SessionLocal
from app.llm import client
from app.llm.extract import parse_json_object  # the llm package's shared reply parser
from app.models import Charger, Session, Site
from app.orchestrator import baseline
from app.orchestrator import loop as orchestrator
from app.providers import ProviderError, get_provider

logger = logging.getLogger("greencharge.llm.copilot")

# One planning call and one narration call, each with this ceiling (the client's own default is
# used when it is smaller; Gemini answered these prompts in 1.5-14.5 s in testing).
TIMEOUT_S = 20.0

# Both calls are retried once, so TIMEOUT_S alone bounds the worst case at 4 x TIMEOUT_S -- a
# demo screen must never wait that long for a 503. This is the ceiling on the whole question,
# retries and tool work included; past it the operator gets the refusal instead of a spinner.
TOTAL_BUDGET_S = 45.0

# At most two tools per question: enough for "how did the site do and how green was the grid",
# far short of anything that could turn into a crawl.
MAX_CALLS = 2

# The answer the copilot gives when the model reports that none of the three tools fits. It is
# our own sentence, not the model's, so a refusal can never carry an invented number.
OUT_OF_SCOPE_ANSWER = (
    "I can only answer questions about charging sessions, site performance and grid carbon "
    "history. Ask me about one of those."
)

SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"
G_PER_KG = 1000.0

# The longest carbon-history window the copilot will actually read. The providers build (and
# cache) one point per 15-minute slot, so a 720-hour request is thousands of rows of database
# work for a summary of five numbers; a request longer than this is trimmed, not refused.
MAX_HISTORY_HOURS = 168


class CopilotError(RuntimeError):
    """The copilot could not answer (model unavailable, unusable output, or every tool failed).

    ``app.routers.llm`` turns this into HTTP 503 carrying the message.
    """


class ToolError(RuntimeError):
    """One tool could not produce its numbers (unknown site, no grid data).

    Reported to the model as ``{"error": ...}`` for that tool, so it can say what is missing.
    """


# --------------------------------------------------------------------------------------------
# Tool argument shapes -- the ONLY thing the model may fill in
# --------------------------------------------------------------------------------------------


class SessionFilters(BaseModel):
    """Arguments of ``query_sessions``. Nothing outside these four fields is accepted."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "completed", "aborted", "any"] = "any"
    site_id: int | None = None
    hours: int | None = Field(default=None, ge=1, le=720)  # plugged in within the last N hours
    limit: int = Field(default=5, ge=1, le=20)


class SitePerformanceArgs(BaseModel):
    """Arguments of ``get_site_performance``."""

    model_config = ConfigDict(extra="forbid")

    site_id: int
    days: int = Field(default=7, ge=1, le=90)


class CarbonHistoryArgs(BaseModel):
    """Arguments of ``get_carbon_history``."""

    model_config = ConfigDict(extra="forbid")

    hours: int = Field(default=24, ge=1, le=720)


class QuerySessionsCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["query_sessions"]
    arguments: SessionFilters = Field(default_factory=SessionFilters)


class SitePerformanceCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["get_site_performance"]
    arguments: SitePerformanceArgs


class CarbonHistoryCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["get_carbon_history"]
    arguments: CarbonHistoryArgs = Field(default_factory=CarbonHistoryArgs)


ToolCall = Annotated[
    Union[QuerySessionsCall, SitePerformanceCall, CarbonHistoryCall],
    Field(discriminator="tool"),
]


class ToolPlan(BaseModel):
    """The model's tool choice. An empty ``calls`` list means "none of these tools fits".

    ``calls`` is REQUIRED and has no default: a reply that spells the key differently
    (``{"tool_calls": [...]}``) is a malformed plan, not a refusal, and must be retried. With a
    default it validated as an empty list and the operator was told the question was out of scope.
    """

    model_config = ConfigDict(extra="ignore")  # a stray "reason" key is harmless

    calls: list[ToolCall] = Field(max_length=MAX_CALLS)


class CopilotAnswer(BaseModel):
    """The model's narration of the tool results."""

    model_config = ConfigDict(extra="ignore")

    answer: str = Field(min_length=1, max_length=800)


# --------------------------------------------------------------------------------------------
# Formatting helpers (every number the model sees is rounded HERE)
# --------------------------------------------------------------------------------------------


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.site_timezone)


def _local(dt: datetime | None) -> str | None:
    """``dt`` as a site-local "YYYY-MM-DD HH:MM" string, so the model never converts a timezone."""
    if dt is None:
        return None
    return dt.astimezone(_tz()).strftime("%Y-%m-%d %H:%M")


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _pct(soc: float | None) -> float | None:
    """A 0.0-1.0 state of charge as a percentage, so the model never multiplies by 100."""
    return None if soc is None else round(float(soc) * 100.0, 1)


# --------------------------------------------------------------------------------------------
# Tool 1 -- query_sessions(filters)
# --------------------------------------------------------------------------------------------


def _on_time(session: Session, last_unmet_kwh: dict[int, float]) -> bool:
    """The on-time rule of ``accounting.impact_summary`` / ``GET /api/sessions/active``."""
    if session.status == SESSION_ACTIVE:
        return last_unmet_kwh.get(session.id, 0.0) == 0.0
    return session.soc_current >= session.soc_target - settings.soc_tolerance


def _session_row(session: Session, ocpp_id: str, site_id: int, on_time: bool) -> dict[str, Any]:
    co2_saved_g = (session.co2_baseline_g or 0.0) - (session.co2_actual_g or 0.0)
    cost_saved_inr = (session.cost_baseline_inr or 0.0) - (session.cost_actual_inr or 0.0)
    return {
        "session_id": session.id,
        "charger": ocpp_id,
        "site_id": site_id,
        "vehicle_model": session.vehicle_model,
        "status": session.status,
        "soc_start_pct": _pct(session.soc_start),
        "soc_current_pct": _pct(session.soc_current),
        "soc_target_pct": _pct(session.soc_target),
        "energy_delivered_kwh": _r(session.energy_delivered_kwh or 0.0),
        "co2_saved_g_measured": _r(co2_saved_g, 1),
        "co2_saved_kg_measured": _r(co2_saved_g / G_PER_KG),
        "cost_saved_inr_measured": _r(cost_saved_inr),
        "plugged_in_at_local": _local(session.plugged_in_at),
        "deadline_local": _local(session.deadline),
        "on_time": on_time,
    }


def _query_sessions_blocking(
    filters: SessionFilters, now: datetime, last_unmet_kwh: dict[int, float]
) -> dict[str, Any]:
    """The rows for ``query_sessions``. Blocking (database); runs in a worker thread."""
    conditions = []
    if filters.status != "any":
        conditions.append(Session.status == filters.status)
    if filters.site_id is not None:
        conditions.append(Charger.site_id == filters.site_id)
    if filters.hours is not None:
        conditions.append(Session.plugged_in_at >= now - timedelta(hours=filters.hours))

    with SessionLocal() as db:
        total = db.execute(
            select(func.count())
            .select_from(Session)
            .join(Charger, Session.charger_id == Charger.id)
            .where(*conditions)
        ).scalar_one()
        rows = db.execute(
            select(Session, Charger.ocpp_id, Charger.site_id)
            .join(Charger, Session.charger_id == Charger.id)
            .where(*conditions)
            .order_by(Session.id.desc())
            .limit(filters.limit)
        ).all()
        sessions = [
            _session_row(session, ocpp_id, site_id, _on_time(session, last_unmet_kwh))
            for session, ocpp_id, site_id in rows
        ]

    return {
        "filters": filters.model_dump(),
        "matching_sessions": int(total),
        "returned_sessions": len(sessions),
        "now_local": _local(now),
        "sessions": sessions,
        "basis": (
            "co2_saved / cost_saved are baseline minus actual on the energy the meters have "
            "already reported; charging still planned is not included."
        ),
    }


async def query_sessions(filters: SessionFilters) -> dict[str, Any]:
    """Charging sessions matching ``filters``, newest first, with their measured savings."""
    last_tick = orchestrator.state.last_tick or {}
    last_unmet_kwh = dict(last_tick.get("unmet_kwh") or {})
    return await asyncio.to_thread(
        _query_sessions_blocking, filters, clock.now(), last_unmet_kwh
    )


# --------------------------------------------------------------------------------------------
# Tool 2 -- get_site_performance(site_id, days)
# --------------------------------------------------------------------------------------------


def _site_performance_blocking(
    site_id: int, days: int, now: datetime, last_unmet_kwh: dict[int, float]
) -> dict[str, Any]:
    """The aggregates for ``get_site_performance``. Blocking (database); runs in a thread."""
    window_start = now - timedelta(days=days)
    latest_schedules = orchestrator.get_latest_schedules()
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            known = db.execute(select(Site.id, Site.name).order_by(Site.id)).all()
            listed = ", ".join(f"{row.id} ({row.name})" for row in known) or "none"
            raise ToolError(f"There is no site {site_id}. Known sites: {listed}.")

        sessions = list(
            db.scalars(
                select(Session)
                .join(Charger, Session.charger_id == Charger.id)
                .where(Charger.site_id == site_id, Session.plugged_in_at >= window_start)
                .order_by(Session.id)
            ).all()
        )

        by_status: dict[str, int] = {}
        energy_kwh = 0.0
        co2_actual_g = 0.0
        co2_baseline_g = 0.0
        cost_actual_inr = 0.0
        cost_baseline_inr = 0.0
        on_time = 0
        counted = 0
        for session in sessions:
            by_status[session.status] = by_status.get(session.status, 0) + 1
            if session.status not in (SESSION_ACTIVE, SESSION_COMPLETED):
                continue  # aborted sessions are outside the impact summary's rules
            counted += 1
            energy_kwh += session.energy_delivered_kwh or 0.0
            co2_actual_g += session.co2_actual_g or 0.0
            co2_baseline_g += session.co2_baseline_g or 0.0
            cost_actual_inr += session.cost_actual_inr or 0.0
            cost_baseline_inr += session.cost_baseline_inr or 0.0
            if _on_time(session, last_unmet_kwh):
                on_time += 1

        chargers = {"total": len(site.chargers), "by_status": {}}
        for charger in site.chargers:
            chargers["by_status"][charger.status] = (
                chargers["by_status"].get(charger.status, 0) + 1
            )

        # The same computation as GET /api/sites/{id}/load-curve: our code, not the model's.
        peaks: dict[str, Any] = {}
        try:
            curve = baseline.site_load_curve(db, site, now, latest_schedules)
            # Named so a sentence built from them cannot read as "peak cut by 15.3 kW": these are
            # the two peaks themselves (the load curve's optimized_peak_kw / baseline_peak_kw).
            peaks = {
                "peak_kw_with_greencharge": _r(curve["optimized_peak_kw"], 1),
                "peak_kw_if_charged_naively": _r(curve["baseline_peak_kw"], 1),
                "peak_window_start_local": _local(curve["window_start"]),
            }
        except Exception as exc:  # never let the load curve sink the whole answer
            logger.warning("Site %d load curve unavailable for the copilot: %s", site_id, exc)
            peaks = {"peak_kw_with_greencharge": None, "peak_kw_if_charged_naively": None}

    co2_saved_g = co2_baseline_g - co2_actual_g
    return {
        "site_id": site.id,
        "site_name": site.name,
        "site_limit_kw": _r(site.max_power_kw, 1),
        "window_days": days,
        "window_start_local": _local(window_start),
        "now_local": _local(now),
        "sessions_in_window": len(sessions),
        "sessions_by_status": by_status,
        "sessions_counted": counted,
        "sessions_on_time": on_time,
        "energy_delivered_kwh": _r(energy_kwh),
        "co2_saved_kg_measured": _r(co2_saved_g / G_PER_KG),
        "co2_saved_g_measured": _r(co2_saved_g, 1),
        "co2_actual_kg": _r(co2_actual_g / G_PER_KG),
        "co2_baseline_kg": _r(co2_baseline_g / G_PER_KG),
        "cost_saved_inr_measured": _r(cost_baseline_inr - cost_actual_inr),
        "cost_actual_inr": _r(cost_actual_inr),
        "cost_baseline_inr": _r(cost_baseline_inr),
        "chargers": chargers,
        **peaks,
        "basis": (
            "Savings are baseline minus actual on metered energy over the window; aborted "
            "sessions are excluded. The peaks are the current 96-slot load curve."
        ),
    }


async def get_site_performance(site_id: int, days: int = 7) -> dict[str, Any]:
    """One site's sessions, energy, savings and peak load over the last ``days`` days."""
    last_tick = orchestrator.state.last_tick or {}
    last_unmet_kwh = dict(last_tick.get("unmet_kwh") or {})
    return await asyncio.to_thread(
        _site_performance_blocking, site_id, days, clock.now(), last_unmet_kwh
    )


# --------------------------------------------------------------------------------------------
# Tool 3 -- get_carbon_history(hours)
# --------------------------------------------------------------------------------------------


def _carbon_history_blocking(zone: str, hours: int) -> tuple[Any, list[Any]]:
    """The provider's history points. Blocking (database); runs in a worker thread.

    Both providers do their database work synchronously inside an ``async def``, so awaiting one
    on the server's event loop stalls everything sharing it -- the OCPP CSMS and the tick
    scheduler included -- for as long as the query takes. Driving the coroutine on a loop of its
    own inside the worker thread keeps that work off ours, exactly as the other two tools do.
    """
    provider = get_provider()
    points = asyncio.run(provider.get_history(zone, hours=hours))
    return provider, list(points)


async def get_carbon_history(hours: int = 24) -> dict[str, Any]:
    """Grid carbon intensity over the last ``hours``, summarised by our code."""
    zone = settings.electricity_maps_zone
    requested_hours = int(hours)
    hours = min(requested_hours, MAX_HISTORY_HOURS)
    try:
        provider, points = await asyncio.to_thread(_carbon_history_blocking, zone, hours)
    except (ProviderError, ValueError) as exc:
        raise ToolError(f"No carbon-intensity history is available for zone {zone}: {exc}") from exc
    if not points:
        raise ToolError(f"No carbon-intensity history is available for zone {zone}.")

    values = [float(p.carbon_intensity) for p in points]
    greenest = min(points, key=lambda p: p.carbon_intensity)
    dirtiest = max(points, key=lambda p: p.carbon_intensity)
    # At most 24 samples of the curve, evenly spaced, so the shape is visible without handing
    # over hundreds of points (and without the model ever having to aggregate them itself).
    step = max(1, len(points) // 24)
    series = [
        {"time_local": _local(p.ts), "gco2_per_kwh": _r(p.carbon_intensity, 1)}
        for p in points[::step]
    ][:24]
    return {
        "zone": zone,
        "source": provider.source,
        "unit": "gCO2eq/kWh",
        "hours": hours,
        "points": len(points),
        "window_start_local": _local(points[0].ts),
        "window_end_local": _local(points[-1].ts),
        "latest_gco2_per_kwh": _r(values[-1], 1),
        "min_gco2_per_kwh": _r(min(values), 1),
        "max_gco2_per_kwh": _r(max(values), 1),
        "mean_gco2_per_kwh": _r(sum(values) / len(values), 1),
        "greenest_time_local": _local(greenest.ts),
        "dirtiest_time_local": _local(dirtiest.ts),
        "samples": series,
        **(
            {}
            if requested_hours <= hours
            else {
                "note": (
                    f"A window of {requested_hours} hours was asked for; this summary covers the "
                    f"most recent {hours} hours, the longest window this tool reads."
                )
            }
        ),
    }


# The whitelist. The model may name one of these three keys and nothing else; ``_run_call``
# checks membership again before dispatching, on top of the Literal-discriminated union.
TOOLS: dict[str, str] = {
    "query_sessions": "filters",
    "get_site_performance": "site_id, days",
    "get_carbon_history": "hours",
}

TOOL_CATALOGUE = """1. query_sessions - the sessions themselves, as they stand RIGHT NOW. It returns
   "matching_sessions", the COUNT of every session matching the filters, and up to "limit" of
   those sessions one row each: vehicle, charger, status, state of charge, energy delivered, that
   session's own savings, its DEADLINE and whether it is ON TIME ("on_time": false means that car
   is projected to miss its deadline). This is the tool for HOW MANY sessions, cars or vehicles
   there are and for anything happening now, currently or at the moment ("how many cars are
   charging", "how many sessions are active right now", "is anything charging"), and for "which
   cars", "which sessions", "who will miss their deadline", "is anything late", "list the
   sessions". Use status "active" for what is charging at this moment. It does not add savings up
   across sessions.
   arguments: {"status": "active"|"completed"|"aborted"|"any" (default "any"),
               "site_id": integer or omitted, "hours": 1-720 or omitted (plugged in within
               the last N hours), "limit": 1-20 (default 5)}
2. get_site_performance - TOTALS ADDED UP for one site over a past window of whole days: how many
   sessions, how many on time, total energy, total CO2 and rupees saved, and the peak load. Use
   this for "how much in total", "overall", "today", "this week", "how is the site doing" -- not
   for how many cars are charging at this moment, which is query_sessions.
   arguments: {"site_id": integer (required), "days": 1-90 (default 7)}
3. get_carbon_history - grid carbon intensity over the last hours, already summarised (latest,
   minimum, maximum, mean, the greenest and dirtiest times). Use it for how green the grid is now
   or has been.
   arguments: {"hours": 1-168 (default 24)}"""

PLAN_SYSTEM = f"""You choose which read-only tools the GreenCharge operator copilot should run.
You have no database access of your own and you never answer the question here.

Tools (exactly these three, no others):
{TOOL_CATALOGUE}

Reply with JSON only, no prose and no code fences:
{{"calls": [{{"tool": "<tool name>", "arguments": {{...}}}}]}}
Rules:
- At most {MAX_CALLS} calls, and only tools from the list above.
- Use only the argument keys listed for that tool; omit an argument to take its default.
- Nobody downstream may add numbers up, so a question about a total or an average must go to the
  tool whose result already contains it.
- "How many ...?" and anything asked about right now, currently or at the moment is a question
  about sessions: call query_sessions, with status "active" when it asks what is charging now.
- Reply {{"calls": []}} only when the question is about something else entirely (the weather, a
  joke, the code); questions about sessions, cars, chargers, a site or the grid always have a
  tool, so choose the closest one rather than refusing.
"""

ANSWER_SYSTEM = """You are the GreenCharge operator copilot. You are given an operator's question
and the results of tools that the system has already run for you.

Rules, in order of importance:
1. NEVER calculate. Do not add, subtract, average, convert units (g to kg, kWh to %), work out
   durations, or COUNT the items in a list. Arithmetic is forbidden. A count is only yours to
   state if a field already holds it (matching_sessions, sessions_on_time, total).
2. Every number, time and date in your answer must be copied verbatim from the tool results,
   written in digits and never spelled out in words. If a number you would like to state is not
   there, do not state it -- a number in the operator's question is not a number you may repeat
   back as a fact.
3. Give each number the unit its field name (or a "unit" field) states, written the way a person
   writes it: gCO2eq/kWh, kWh, kg, %, INR. Never rewrite the field name itself into the sentence.
4. If the tool results do not answer the question exactly, say plainly what is missing AND give
   the closest figures they do contain, naming the window those figures cover. Never stop at
   "the tool results do not provide that" when a summary of the same quantity is in front of you.
5. Answer in at most three sentences, in the operator's language, plain and factual.

Reply with JSON only, no prose and no code fences: {"answer": "<your answer>"}
"""


# --------------------------------------------------------------------------------------------
# Model output handling
# --------------------------------------------------------------------------------------------

# Digits that start a token, so a digit inside a word (the 2 of "gCO2", the 003 of "CP003") is
# not read as a quantity the answer has to justify.
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*")


def _numbers(text: str) -> list[tuple[str, float, int]]:
    """Numeric tokens of ``text`` as (token, value, decimals).

    Indian digit grouping is the convention here ("1,23,456.78"), so a comma is always a
    thousands separator and a dot is always the decimal point. A token that is still not a
    number (``1.2.3``) is skipped rather than guessed at.
    """
    out: list[tuple[str, float, int]] = []
    for token in _NUMBER_RE.findall(text):
        cleaned = token.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        out.append((token, value, decimals))
    return out


# The unit a number carries in a sentence, taken from what follows it ("62.4 kg", "610
# gCO2eq/kWh", "38%") or, for money, from what precedes it ("Rs 1,240").
_UNIT_AFTER_RE = re.compile(r"[\s\-]*(%|[A-Za-z][A-Za-z0-9]*(?:\s*/\s*[A-Za-z][A-Za-z0-9]*)?)")
_MONEY_BEFORE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*$", re.IGNORECASE)


def _answer_numbers(answer: str) -> list[tuple[str, float, int, str]]:
    """Numeric tokens of ``answer`` as (token, value, decimals, unit).

    The unit is lowercased with its spaces removed ("gco2eq/kwh", "kwh", "kg", "%", "inr") and is
    "" when the number stands alone. It is only ever read, never converted.
    """
    out: list[tuple[str, float, int, str]] = []
    for match in _NUMBER_RE.finditer(answer):
        token = match.group(0)
        cleaned = token.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        if _MONEY_BEFORE_RE.search(answer[: match.start()]):
            unit = "inr"
        else:
            after = _UNIT_AFTER_RE.match(answer, match.end())
            unit = after.group(1).lower().replace(" ", "").rstrip(".") if after else ""
        out.append((token, value, decimals, unit))
    return out


def _unit_fields(unit: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Which payload field names may justify ``unit``: (fragments wanted, fragments forbidden).

    Field names are matched with an underscore glued to each end ("_co2_actual_g_"), so "_g_"
    means "this field is in grams" rather than "a g appears somewhere in it". None means the unit
    is one our field names do not encode (hours, days, sessions, chargers): value alone decides.
    Order matters -- gCO2eq/kWh is recognised before kWh, and kWh before kW.
    """
    if unit.startswith(("gco2", "g/kwh", "gco₂")):
        return ("gco2",), ()
    if unit.startswith(("kwh", "kilowatthour")):
        return ("kwh",), ("gco2",)
    if unit.startswith("kw"):
        return ("kw",), ("kwh", "gco2")
    if unit.startswith(("kg", "kilogram")):
        return ("kg",), ()
    if unit == "g" or unit.startswith("gram"):
        return ("_g_",), ()
    if unit.startswith(("%", "percent", "pct")):
        return ("pct",), ()
    if unit.startswith(("inr", "rupee", "rs")):
        return ("inr",), ()
    return None


def _payload_numbers(payload: str) -> list[tuple[str | None, float]]:
    """Every number in the tool-result JSON as (field name, value), newest rows and all.

    The field name is the JSON key the number sits under, lowercased and wrapped in underscores
    so ``_unit_fields`` can match whole name parts. It is None when the payload could not be
    parsed at all, which means "field unknown, ground on the value alone".
    """
    try:
        data = json.loads(payload)
    except ValueError:  # never expected: we built this string ourselves
        return [(None, value) for _token, value, _d in _numbers(payload)]

    out: list[tuple[str | None, float]] = []

    def walk(node: Any, field: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"_{str(key).lower()}_")
        elif isinstance(node, list):
            for item in node:
                walk(item, field)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            out.append((field, float(node)))
        elif isinstance(node, str):
            out.extend((field, value) for _token, value, _d in _numbers(node))

    walk(data, "_")
    return out


def _ungrounded_numbers(answer: str, payload: str) -> list[str]:
    """Numeric tokens of ``answer`` that are not in the tool results.

    A token grounds against a payload number when it is that number rounded to the token's own
    number of decimals, whatever rounding rule was used ("703" grounds against 703.25, "62%"
    against a soc_current_pct of 62.4). Anything else -- a converted unit, a summed total, an
    invented figure -- is reported and the answer is refused.

    The operator's QUESTION is deliberately not a grounding source. It used to be, and that let a
    question launder its own figure into an answer the UI renders as fact: ask "did we really save
    999 kg this week?" and "yes, the site saved 999 kg" passed the check, because 999 was in the
    question. Every number an answer legitimately echoes from the question is already in the
    payload anyway -- the tool arguments the planner derived from it ("hours": 24, "site_id": 1,
    "days": 7) are dumped into the payload alongside each result.

    A number the answer gives a UNIT to must additionally come from a field that carries that
    unit, so "saved 62.4 kg of CO2" can no longer be justified by a soc_current_pct of 62.4: it
    has to match a field whose name says kg. Units the field names do not encode (hours, days,
    sessions, ids, times) constrain nothing and ground on value alone, as before.
    """
    grounded = _payload_numbers(payload)
    bad: list[str] = []
    for token, value, decimals, unit in _answer_numbers(answer):
        tolerance = 0.5 * (10.0**-decimals) + 1e-9
        rule = _unit_fields(unit)
        for field, candidate in grounded:
            if abs(value - candidate) > tolerance:
                continue
            if field is None or rule is None:  # unknown field, or a unit we do not police
                break
            wanted, forbidden = rule
            if any(f in field for f in wanted) and not any(f in field for f in forbidden):
                break
        else:
            bad.append(f"{token} {unit}".strip())
    return bad


# --------------------------------------------------------------------------------------------
# The copilot
# --------------------------------------------------------------------------------------------


def _known_sites_blocking() -> str:
    """"Sites in this deployment: 1 (Adani Shantigram)." -- so the planner does not guess an id.

    The ids come from the database, not from the model. Blocking; runs in a worker thread.
    """
    with SessionLocal() as db:
        rows = db.execute(select(Site.id, Site.name).order_by(Site.id)).all()
    if not rows:
        return "No sites are configured."
    listed = ", ".join(f"{row.id} ({row.name})" for row in rows)
    return f"Sites in this deployment: {listed}."


async def _plan(question: str, now_local: str) -> ToolPlan:
    """Ask the model which tools to run. One retry, then ``CopilotError``."""
    try:
        sites = await asyncio.to_thread(_known_sites_blocking)
    except Exception:
        # The tools will report the database problem themselves; do not lose the question here.
        logger.exception("Could not list the sites for the copilot plan")
        sites = ""
    prompt = f"{sites}\nCurrent site-local time: {now_local}\nOperator question: {question}"
    for attempt in (1, 2):
        raw = await client.complete(prompt, system=PLAN_SYSTEM, timeout_s=TIMEOUT_S)
        data = parse_json_object(raw or "")
        if data is None:
            logger.warning("Copilot plan was not usable JSON (attempt %d)", attempt)
            continue
        try:
            plan = ToolPlan.model_validate(data)
        except ValidationError as exc:
            logger.warning("Copilot plan did not validate (attempt %d): %s", attempt, exc)
            prompt = (
                f"{prompt}\n\nYour previous reply was rejected: it must be JSON of the form "
                '{"calls": [{"tool": "<one of the three tool names>", "arguments": {...}}]} '
                "using only the listed argument keys."
            )
            continue
        # Belt and braces: the discriminated union already refuses an unknown tool name.
        unknown = [call.tool for call in plan.calls if call.tool not in TOOLS]
        if unknown:
            logger.warning("Copilot plan named tools outside the whitelist: %s", unknown)
            continue
        return plan
    raise CopilotError("The language model did not return a usable tool plan.")


async def _run_call(call: Any) -> dict[str, Any]:
    """Run one whitelisted tool call, isolated: any failure becomes an ``{"error": ...}`` result.

    Not only ``ToolError``: a dropped database connection inside one tool used to abort the whole
    answer even when the other tool had its numbers ready. Each call fails on its own here, and
    ``_answer_question`` still raises ``CopilotError`` when every call failed.
    """
    if call.tool not in TOOLS:  # unreachable through ToolPlan; kept as the last gate
        raise CopilotError(f"Tool {call.tool!r} is not one of {sorted(TOOLS)}.")
    try:
        if call.tool == "query_sessions":
            return await query_sessions(call.arguments)
        if call.tool == "get_site_performance":
            return await get_site_performance(call.arguments.site_id, call.arguments.days)
        return await get_carbon_history(call.arguments.hours)
    except ToolError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # database, provider or programming error in this one tool
        logger.exception("Copilot tool %s failed", call.tool)
        # One short line for the model: a driver's traceback-length message (SQL and all) would
        # drown the other tool's numbers in the payload. The full detail is in the log above.
        detail = " ".join(str(exc).split())[:160] or type(exc).__name__
        return {"error": f"{call.tool} could not be run: {detail}"}


async def _narrate(question: str, payload: str) -> str:
    """Ask the model to narrate the tool results, then check every number it used."""
    prompt = f"Operator question: {question}\n\nTool results (JSON):\n{payload}"
    for attempt in (1, 2):
        raw = await client.complete(prompt, system=ANSWER_SYSTEM, timeout_s=TIMEOUT_S)
        data = parse_json_object(raw or "")
        if data is None:
            logger.warning("Copilot answer was not usable JSON (attempt %d)", attempt)
            continue
        try:
            answer = CopilotAnswer.model_validate(data).answer.strip()
        except ValidationError as exc:
            logger.warning("Copilot answer did not validate (attempt %d): %s", attempt, exc)
            continue
        ungrounded = _ungrounded_numbers(answer, payload)
        if not ungrounded:
            return answer
        logger.warning(
            "Copilot answer used %d number(s) that are not in the tool results (attempt %d): %s",
            len(ungrounded), attempt, ungrounded,
        )
        prompt = (
            f"{prompt}\n\nYour previous answer was rejected because it contained "
            f"{', '.join(ungrounded)}, which do not appear in the tool results. Copy numbers "
            "exactly as they appear there, with their own units, or leave them out."
        )
    raise CopilotError("The language model did not return a usable answer.")


async def answer_question(question: str) -> dict[str, Any]:
    """Answer an operator question from tool results only.

    Returns ``{"answer": str, "tools_used": list[str]}``. Raises ``CopilotError`` when the model
    is unavailable or unusable, when every tool it chose failed, or when the whole question took
    longer than ``TOTAL_BUDGET_S``; ``app.routers.llm`` maps that to HTTP 503.
    """
    if not client.is_configured():
        raise CopilotError("No LLM API key is configured, so the copilot is unavailable.")
    try:
        async with asyncio.timeout(TOTAL_BUDGET_S):
            return await _answer_question(question)
    except TimeoutError as exc:
        logger.warning("Copilot gave up after %.0fs on: %s", TOTAL_BUDGET_S, question)
        raise CopilotError(
            f"The copilot did not finish within {TOTAL_BUDGET_S:.0f} seconds; "
            f"please ask again."
        ) from exc


async def _answer_question(question: str) -> dict[str, Any]:
    """``answer_question`` without the overall time budget wrapped around it."""
    now_local = _local(clock.now())
    plan = await _plan(question, now_local or "")
    if not plan.calls:
        logger.info("Copilot: no tool fits the question, answering out of scope")
        return {"answer": OUT_OF_SCOPE_ANSWER, "tools_used": []}

    results = []
    tools_used: list[str] = []
    for call in plan.calls:
        result = await _run_call(call)
        tools_used.append(call.tool)
        results.append(
            {"tool": call.tool, "arguments": call.arguments.model_dump(), "result": result}
        )
    if all("error" in entry["result"] for entry in results):
        raise CopilotError("; ".join(str(entry["result"]["error"]) for entry in results))

    payload = json.dumps(
        {"now_local": now_local, "tool_results": results},
        ensure_ascii=False,
        default=str,
    )
    answer = await _narrate(question, payload)
    logger.info("Copilot answered using %s", ", ".join(tools_used))
    return {"answer": answer, "tools_used": tools_used}
