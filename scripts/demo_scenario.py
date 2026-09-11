#!/usr/bin/env python3
"""Run a GreenCharge demo scenario on a running backend and wait until every plug-in is done.

    python scripts/demo_scenario.py --scenario evening_rush [--base-url http://localhost:8000]

Starts the scenario with POST /api/demo/scenario/{name} (the backend first resets the demo state,
then sets the simulation clock and plugs the cars in one by one), then polls GET /api/demo/status
every second until the scenario is no longer running. It prints each plug-in as it happens, a
summary at the end and, after a successful run, each site's baseline and optimized peak power
(GET /api/sites/{id}/load-curve).

Exit status: 0 when every plug-in succeeded; 1 on any error -- the backend refused the scenario
(unknown name, one already running), could not be reached, or reported an error.

The backend (``uvicorn app.main:app --port 8000`` in backend/) and the simulator
(``python simulator/run.py --chargers 6``) must be running. Only the Python standard library is
used, so any Python 3 runs this script, with or without the project virtualenv.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://localhost:8000"
POLL_INTERVAL_S = 1.0  # how often GET /api/demo/status is polled
REQUEST_TIMEOUT_S = 10.0  # per HTTP request
MAX_POLL_FAILURES = 5  # consecutive failed status polls before giving up
PERCENT = 100.0
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130  # conventional exit status after Ctrl-C (128 + SIGINT)

# Event statuses reported by GET /api/demo/status. "pending" (not started yet) and "skipped"
# (the scenario stopped before it) are not worth a progress line.
EVENT_PENDING = "pending"
EVENT_PLUGGED_IN = "plugged_in"
EVENT_SKIPPED = "skipped"
QUIET_EVENT_STATUSES = (EVENT_PENDING, EVENT_SKIPPED)


class ApiError(Exception):
    """The backend answered with an HTTP error, sent something that is not JSON, or could not
    be reached."""


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The ``detail`` of a FastAPI error response, else the raw body or the HTTP reason."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except OSError:
        raw = ""
    try:
        detail = json.loads(raw).get("detail", raw)
    except (ValueError, AttributeError):
        detail = raw
    if isinstance(detail, list):  # validation errors: [{"loc": [...], "msg": "..."}, ...]
        detail = "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                           for item in detail)
    return str(detail).strip() or str(exc.reason)


def call_api(method: str, url: str) -> dict:
    """Send one request without a body and return the decoded JSON answer. Raises ApiError."""
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:  # before URLError, which it subclasses
        raise ApiError(f"{method} {url} -> HTTP {exc.code}: {_error_detail(exc)}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {url} failed: {exc.reason}") from None
    except OSError as exc:  # e.g. a timeout while reading the answer
        raise ApiError(f"{method} {url} failed: {exc}") from None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise ApiError(f"{method} {url} did not return JSON: {body[:200]!r}") from None


def _percent(soc) -> str:
    return f"{float(soc) * PERCENT:.0f}%" if soc is not None else "?"


def describe_event(event: dict, total: int) -> str:
    """One line for a finished (plugged-in or failed) scenario event."""
    head = (
        f"  [{event.get('step')}/{total}] {event.get('local_time', '--:--')}  "
        f"charger {event.get('charger_id')}  {event.get('vehicle_model', '?'):<16} "
        f"{_percent(event.get('soc_start'))} -> {_percent(event.get('soc_target'))}"
    )
    if event.get("status") == EVENT_PLUGGED_IN:
        hours = event.get("hours_until_departure")
        leaves = f", departs in {hours:.2f} h (simulated)" if isinstance(hours, (int, float)) else ""
        return f"{head}  {event.get('ocpp_id')} session {event.get('session_id')}{leaves}"
    outcome = str(event.get("status", "?")).upper()
    detail = event.get("detail")
    return f"{head}  {outcome}" + (f": {detail}" if detail else "")


def print_new_events(status: dict, reported: set) -> None:
    """Print the events that finished since the last poll (each one once)."""
    total = status.get("total_steps", "?")
    for event in status.get("events") or []:
        step = event.get("step")
        if step in reported or event.get("status") in QUIET_EVENT_STATUSES:
            continue
        reported.add(step)
        print(describe_event(event, total), flush=True)


def print_peaks(base: str) -> None:
    """Print each site's optimized and baseline peak (the spec's Phase 4 acceptance asks for
    both). Best effort: a failure here is reported but never changes the exit status."""
    try:
        sites = call_api("GET", f"{base}/api/sites")
        for site in sites:
            curve = call_api("GET", f"{base}/api/sites/{site['id']}/load-curve")
            print(
                f"  site {site['id']} {site.get('name', '')}: baseline peak "
                f"{float(curve['baseline_peak_kw']):.1f} kW vs optimized peak "
                f"{float(curve['optimized_peak_kw']):.1f} kW "
                f"(site limit {float(curve['max_power_kw']):.1f} kW)",
                flush=True,
            )
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        print(f"warning: could not read the load curve: {exc}", file=sys.stderr, flush=True)


def print_summary(name: str, status: dict, elapsed_s: float) -> None:
    total = status.get("total_steps", 0)
    done = status.get("step", 0)
    sessions = [
        str(event.get("session_id"))
        for event in status.get("events") or []
        if event.get("status") == EVENT_PLUGGED_IN
    ]
    if status.get("error"):
        print(
            f"Scenario {name!r} FAILED after {done}/{total} plug-in(s), {elapsed_s:.0f} s: "
            f"{status['error']}",
            flush=True,
        )
        return
    print(
        f"Scenario {name!r} finished in {elapsed_s:.0f} s: {done}/{total} plug-ins done"
        f" (sessions {', '.join(sessions) or 'none'}). The cars keep charging under the"
        " optimizer in simulated time.",
        flush=True,
    )


def run(name: str, base_url: str) -> int:
    base = base_url.rstrip("/")
    start_url = f"{base}/api/demo/scenario/{urllib.parse.quote(name, safe='')}"
    status_url = f"{base}/api/demo/status"

    try:
        started = call_api("POST", start_url)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"Started scenario {name!r}: {started.get('steps', '?')} plug-in(s). The backend resets "
        "the demo state first.",
        flush=True,
    )

    began = time.monotonic()
    reported: set = set()
    failures = 0
    while True:
        try:
            status = call_api("GET", status_url)
        except ApiError as exc:
            failures += 1
            print(f"warning: {exc} ({failures}/{MAX_POLL_FAILURES})", file=sys.stderr, flush=True)
            if failures >= MAX_POLL_FAILURES:
                print("error: the backend stopped answering; giving up", file=sys.stderr)
                return EXIT_ERROR
            time.sleep(POLL_INTERVAL_S)
            continue
        failures = 0
        if status.get("scenario") != name:
            print(
                f"error: the backend now reports scenario {status.get('scenario')!r}; "
                f"the {name!r} run was replaced",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print_new_events(status, reported)
        if not status.get("running"):
            break
        time.sleep(POLL_INTERVAL_S)

    print_summary(name, status, time.monotonic() - began)
    if not status.get("error"):
        print_peaks(base)
        return EXIT_OK
    return EXIT_ERROR


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a GreenCharge demo scenario on a running backend and wait until all of its "
            "plug-ins are done. Exits 1 on any error."
        )
    )
    parser.add_argument(
        "--scenario", required=True, help="scenario name, e.g. evening_rush"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"backend base URL (default: {DEFAULT_BASE_URL})",
    )
    args = parser.parse_args(argv)
    try:
        return run(args.scenario, args.base_url)
    except KeyboardInterrupt:
        print("\ninterrupted; the scenario keeps running on the backend", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
