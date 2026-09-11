"""Launch simulated OCPP 1.6J charge points against the GreenCharge CSMS.

Run from the project root:

    python simulator/run.py --chargers 6

Starts CP001..CP00N concurrently in one asyncio process. Each one connects to <url>/<ocpp_id> with
the WebSocket subprotocol "ocpp1.6" and, whenever the CSMS is unreachable or the connection drops,
tries again every 2 seconds. An open transaction survives a reconnect (see charge_point.py).

Settings are read from ROOT/.env (found from this file's location and loaded with python-dotenv;
variables already set in the environment take precedence):

  TIME_SCALE  simulated seconds per real second; default 60 (1 real second = 1 simulated minute).
              It compresses simulated time only (battery physics, departure countdown, timestamps);
              Heartbeat and MeterValues cadence stays in real seconds. The backend reads the same
              variable from the same file, so both processes run at the same speed.
  OCPP_PORT   CSMS port used by the default --url; default 9000.

This program is independent of the backend: it never imports the backend's `app` package.
"""
import argparse
import asyncio
import logging
import math
import os
import sys
from pathlib import Path

import websockets
from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from charge_point import RETRY_DELAY_S, ChargerState, SimChargePoint, SimClock

ROOT = Path(__file__).resolve().parents[1]  # greencharge/ (this file is greencharge/simulator/run.py)
OCPP_SUBPROTOCOL = "ocpp1.6"
DEFAULT_TIME_SCALE = 60.0  # spec Phase 2: TIME_SCALE default 60
DEFAULT_OCPP_PORT = 9000  # .env.example OCPP_PORT
DEFAULT_CHARGERS = 6  # spec Phase 2: six simulated chargers
DEFAULT_MAX_KW = 22.0  # charger rating (seeded chargers are 22.0 kW) = the limit before any profile

log = logging.getLogger("greencharge.simulator")


def _env_number(name: str, default: float, cast: type) -> float:
    """Read a positive number from the environment, or exit with a clear message."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return cast(default)
    try:
        value = cast(raw.strip())
    except ValueError:
        raise SystemExit(f"{name} must be a positive number, got {raw!r}") from None
    if not (math.isfinite(value) and value > 0):
        raise SystemExit(f"{name} must be a positive number, got {raw!r}")
    return value


def _parse_args(argv: list[str] | None, ocpp_port: int) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run simulated OCPP 1.6J charge points CP001..CP00N against the GreenCharge CSMS.",
        epilog="TIME_SCALE and OCPP_PORT are read from the environment or from ROOT/.env.",
    )
    parser.add_argument(
        "--chargers",
        type=int,
        default=DEFAULT_CHARGERS,
        help=f"number of charge points, CP001..CP00N (default {DEFAULT_CHARGERS})",
    )
    parser.add_argument(
        "--url",
        default=f"ws://localhost:{ocpp_port}",
        help="CSMS WebSocket base URL; each CP connects to <url>/<ocpp_id> "
        "(default ws://localhost:$OCPP_PORT, currently %(default)s)",
    )
    parser.add_argument(
        "--max-kw",
        type=float,
        default=DEFAULT_MAX_KW,
        help="charger rating in kW, used as the power limit until a SetChargingProfile arrives "
        f"(default {DEFAULT_MAX_KW})",
    )
    args = parser.parse_args(argv)
    if args.chargers < 1:
        parser.error(f"--chargers must be at least 1, got {args.chargers}")
    if not (math.isfinite(args.max_kw) and args.max_kw > 0):
        parser.error(f"--max-kw must be a positive number, got {args.max_kw}")
    if not args.url.startswith(("ws://", "wss://")):
        parser.error(f"--url must start with ws:// or wss://, got {args.url!r}")
    return args


async def run_charger(ocpp_id: str, url: str, clock: SimClock, max_kw: float) -> None:
    """Keep one charge point connected forever, retrying every RETRY_DELAY_S seconds."""
    state = ChargerState()  # outlives each connection, so an open transaction resumes after a reconnect
    uri = f"{url.rstrip('/')}/{ocpp_id}"
    failures = 0
    while True:
        try:
            async with websockets.connect(uri, subprotocols=[OCPP_SUBPROTOCOL]) as ws:
                if ws.subprotocol != OCPP_SUBPROTOCOL:
                    raise InvalidHandshake(
                        f"the CSMS did not select subprotocol {OCPP_SUBPROTOCOL!r} (got {ws.subprotocol!r})"
                    )
                failures = 0
                log.info("%s: connected to %s", ocpp_id, uri)
                await SimChargePoint(ocpp_id, ws, clock=clock, charger_max_kw=max_kw, state=state).run()
        except ConnectionClosed as exc:
            log.warning("%s: connection closed (%s); reconnecting in %.0f s", ocpp_id, exc, RETRY_DELAY_S)
        except (OSError, asyncio.TimeoutError, InvalidHandshake) as exc:
            failures += 1
            # Say it once, then keep quiet while the CSMS stays down.
            level = logging.WARNING if failures == 1 else logging.DEBUG
            log.log(
                level, "%s: cannot connect to %s (%s); retrying every %.0f s",
                ocpp_id, uri, str(exc) or type(exc).__name__, RETRY_DELAY_S,
            )
        except Exception:
            log.exception("%s: unexpected error; reconnecting in %.0f s", ocpp_id, RETRY_DELAY_S)
        await asyncio.sleep(RETRY_DELAY_S)


async def run_all(chargers: int, url: str, time_scale: float, max_kw: float) -> None:
    clock = SimClock(time_scale)  # one simulated clock for the whole fleet, synced by the CSMS replies
    ocpp_ids = [f"CP{i:03d}" for i in range(1, chargers + 1)]
    await asyncio.gather(*(run_charger(ocpp_id, url, clock, max_kw) for ocpp_id in ocpp_ids))


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")  # does not override variables already set in the environment
    ocpp_port = int(_env_number("OCPP_PORT", DEFAULT_OCPP_PORT, int))
    time_scale = _env_number("TIME_SCALE", DEFAULT_TIME_SCALE, float)
    args = _parse_args(argv, ocpp_port)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("ocpp").setLevel(logging.WARNING)  # the library logs every frame at INFO

    log.info(
        "starting %d charge point(s) against %s (TIME_SCALE=%g, rating %.1f kW)",
        args.chargers, args.url, time_scale, args.max_kw,
    )
    try:
        asyncio.run(run_all(args.chargers, args.url, time_scale, args.max_kw))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
