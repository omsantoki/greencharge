import { useMemo } from 'react';
import type { ActiveSession, Charger, Site } from '../api/client';

/**
 * ChargerGrid — the site's chargers (GET /api/sites) joined with the live
 * sessions (GET /api/sessions/active).
 *
 * All data arrives as props: this component never fetches and never reads the
 * browser clock. `now` is the SIMULATED clock (GET /api/clock), because every
 * timestamp the backend emits is simulated time.
 *
 * It fills its parent (the dashboard gives each panel a fixed height) and
 * scrolls internally rather than pushing the layout around on refresh. The card
 * chrome (rounded border + shadow, sentence-case title) is the same as every
 * other panel on the dashboard, so the row reads as one set of cards.
 */

export interface ChargerGridProps {
  /** The site from GET /api/sites (first entry). null while it is loading. */
  site?: Site | null;
  /** The chargers, if the caller already unpacked them. Defaults to site.chargers. */
  chargers?: Charger[] | null;
  /** GET /api/sessions/active. */
  sessions?: ActiveSession[] | null;
  /** Simulated "now" (ISO-8601) from GET /api/clock. */
  now?: string | null;
  /**
   * time_scale from GET /api/clock. OCPP heartbeats are sent every 30 REAL
   * seconds, so the age is only meaningful in real time; without this the age
   * is reported in simulated time and no staleness is claimed.
   */
  timeScale?: number | null;
}

const TZ = 'Asia/Kolkata';

const clockFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const stampFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

interface StatusStyle {
  dot: string;
  text: string;
  card: string;
}

/** OCPP 1.6 ChargePointStatus values this system reports, plus a neutral fallback. */
const STATUS_STYLES: Record<string, StatusStyle> = {
  Available: { dot: 'bg-slate-400', text: 'text-slate-600', card: 'border-slate-200' },
  Preparing: { dot: 'bg-amber-400', text: 'text-amber-700', card: 'border-amber-300' },
  Charging: { dot: 'bg-emerald-500', text: 'text-emerald-700', card: 'border-emerald-400' },
  Finishing: { dot: 'bg-sky-500', text: 'text-sky-700', card: 'border-sky-300' },
  Faulted: { dot: 'bg-red-500', text: 'text-red-700', card: 'border-red-400' },
};

const UNKNOWN_STATUS: StatusStyle = {
  dot: 'bg-slate-300',
  text: 'text-slate-500',
  card: 'border-slate-200',
};

function parseTs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/**
 * What the charger has been told to draw right now: the manual override limit
 * if one is set, otherwise the power of the schedule slot containing `now`
 * (the slot the orchestrator pushed as a SetChargingProfile).
 *
 * A planned 0 kW is not a fault — the optimizer parks the car until a better
 * slot — so the next planned slot is reported too, and the card says so.
 */
function currentPowerKw(
  session: ActiveSession,
  nowMs: number | null,
): { kw: number; manual: boolean; nextStartMs: number | null } | null {
  if (session.manual_limit_w != null) {
    return { kw: session.manual_limit_w / 1000, manual: true, nextStartMs: null };
  }
  const slots = session.schedule ?? [];
  if (slots.length === 0) return null;
  let chosen = slots[0];
  if (nowMs != null) {
    for (const slot of slots) {
      const startMs = parseTs(slot.slot_start);
      if (startMs == null || startMs > nowMs) break;
      chosen = slot;
    }
  }
  let nextStartMs: number | null = null;
  if (!(chosen.power_kw > 0)) {
    for (const slot of slots) {
      const startMs = parseTs(slot.slot_start);
      if (startMs == null) continue;
      if (nowMs != null && startMs <= nowMs) continue;
      if (slot.power_kw > 0) {
        nextStartMs = startMs;
        break;
      }
    }
  }
  return { kw: chosen.power_kw, manual: false, nextStartMs };
}

interface Heartbeat {
  label: string;
  title: string;
  tone: string;
  disconnected: boolean;
}

function heartbeat(
  charger: Charger,
  nowMs: number | null,
  timeScale: number | null | undefined,
): Heartbeat {
  const hbMs = parseTs(charger.last_heartbeat);
  if (hbMs == null) {
    return {
      label: 'no heartbeat',
      title: 'This charge point has never sent a Heartbeat — it is not connected to the CSMS.',
      tone: 'text-red-600',
      disconnected: true,
    };
  }
  const at = `${stampFmt.format(new Date(hbMs))} IST (simulated clock)`;
  if (nowMs == null) {
    return {
      label: `hb ${stampFmt.format(new Date(hbMs))}`,
      title: `Last Heartbeat at ${at}.`,
      tone: 'text-slate-500',
      disconnected: false,
    };
  }
  const simSeconds = Math.max(0, (nowMs - hbMs) / 1000);
  const scale = timeScale != null && timeScale > 0 ? timeScale : null;
  if (scale == null) {
    // No TIME_SCALE available: report the simulated age and claim nothing about staleness.
    return {
      label: `hb ${formatAge(simSeconds)} sim`,
      title:
        `Last Heartbeat at ${at} — ${formatAge(simSeconds)} of simulated time ago. ` +
        'Chargers heartbeat every 30 real seconds, which is TIME_SCALE simulated minutes.',
      tone: 'text-slate-500',
      disconnected: false,
    };
  }
  const realSeconds = simSeconds / scale;
  const stale = realSeconds > 90;
  return {
    label: `hb ${formatAge(realSeconds)}${stale ? ' stale' : ''}`,
    title:
      `Last Heartbeat at ${at} — ${formatAge(realSeconds)} of real time ago ` +
      `(${formatAge(simSeconds)} of simulated time). Chargers heartbeat every 30 real seconds.`,
    tone: stale ? 'text-amber-600' : 'text-slate-500',
    disconnected: false,
  };
}

export default function ChargerGrid({
  site = null,
  chargers,
  sessions,
  now = null,
  timeScale = null,
}: ChargerGridProps) {
  const nowMs = parseTs(now);
  const list: Charger[] = (chargers && chargers.length > 0 ? chargers : site?.chargers) ?? [];

  const byCharger = useMemo(() => {
    const map = new Map<number, ActiveSession>();
    for (const session of sessions ?? []) {
      if (!map.has(session.charger_id)) map.set(session.charger_id, session);
    }
    return map;
  }, [sessions]);

  const cards = list.map((charger) => {
    const session = byCharger.get(charger.id) ?? null;
    return { charger, session, power: session ? currentPowerKw(session, nowMs) : null };
  });

  const setKw = cards.reduce((sum, card) => sum + (card.power?.kw ?? 0), 0);
  const chargingCount = cards.filter((card) => card.charger.status === 'Charging').length;
  // Plugged in with an open transaction but planned 0 kW for the current slot — parked, not broken.
  const holdingCount = cards.filter(
    (card) => card.session != null && card.power != null && !(card.power.kw > 0),
  ).length;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <header className="mb-2 flex shrink-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <h2 className="text-sm font-semibold text-slate-900">Chargers</h2>
        <p className="text-[11px] text-slate-500 tabular-nums">
          {chargingCount} charging ·{' '}
          <span title="Sum of the limits currently pushed to the chargers: the manual override, else the plan for the slot containing now.">
            {setKw.toFixed(1)} kW set
          </span>
          {site ? <> of {site.max_power_kw.toFixed(0)} kW site limit</> : null}
          {holdingCount > 0 ? (
            <>
              {' · '}
              <span title="Plugged in with an open OCPP transaction, but the plan gives them 0 kW in the current 15-minute slot: the optimizer is holding them for a better slot later in the window.">
                {holdingCount} holding
              </span>
            </>
          ) : null}
        </p>
      </header>

      {cards.length === 0 ? (
        <p className="flex flex-1 items-center justify-center text-center text-xs text-slate-500">
          {site == null ? 'Waiting for site data…' : 'This site has no chargers.'}
        </p>
      ) : (
        <ul
          className={`grid min-h-0 flex-1 grid-cols-1 content-start gap-2 overflow-y-auto sm:grid-cols-2 xl:grid-cols-3 ${
            // With a full rank of chargers the rows stretch to fill the panel, so the
            // card does not end in a block of empty white next to the load curve.
            cards.length >= 3 ? 'auto-rows-fr' : ''
          }`}
        >
          {cards.map(({ charger, session, power }) => {
            const style = STATUS_STYLES[charger.status] ?? UNKNOWN_STATUS;
            const hb = heartbeat(charger, nowMs, timeScale);
            const socPct = session ? session.soc_current * 100 : 0;
            const targetPct = session ? session.soc_target * 100 : 0;
            const deadlineMs = session ? parseTs(session.deadline) : null;
            return (
              <li
                key={charger.id}
                className={`flex min-h-[6.25rem] flex-col rounded-lg border px-2 py-1.5 ${
                  hb.disconnected ? 'border-dashed border-slate-300 bg-slate-50' : `bg-white ${style.card}`
                }`}
              >
                <div className="flex items-baseline justify-between gap-1">
                  <span className="font-mono text-[13px] font-semibold text-slate-900">
                    {charger.ocpp_id}
                  </span>
                  <span className="flex items-center gap-1 text-[11px] font-medium">
                    <span
                      className={`inline-block h-2 w-2 shrink-0 rounded-full ${
                        hb.disconnected ? 'bg-slate-300' : style.dot
                      }`}
                    />
                    <span className={hb.disconnected ? 'text-slate-400' : style.text}>
                      {charger.status}
                    </span>
                  </span>
                </div>

                {hb.disconnected ? (
                  <p className="mt-1 text-[11px] leading-tight text-slate-500">
                    Disconnected — no OCPP heartbeat.
                  </p>
                ) : session ? (
                  <>
                    <p
                      className="truncate text-[11px] text-slate-700"
                      title={`${session.vehicle_model} · ${session.battery_kwh} kWh battery · max ${session.max_charge_kw} kW`}
                    >
                      {session.vehicle_model}
                    </p>
                    <div className="relative mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                      <div
                        className="h-full bg-emerald-500"
                        style={{ width: `${Math.max(0, Math.min(100, socPct))}%` }}
                      />
                      <div
                        className="absolute top-0 h-full w-0.5 bg-slate-900/70"
                        style={{ left: `${Math.max(0, Math.min(100, targetPct))}%` }}
                      />
                    </div>
                    <p className="mt-0.5 text-[10px] text-slate-600 tabular-nums">
                      SoC {socPct.toFixed(1)}% → {targetPct.toFixed(0)}%
                      {deadlineMs != null ? <> · by {clockFmt.format(new Date(deadlineMs))}</> : null}
                    </p>
                  </>
                ) : (
                  <p className="mt-1 text-[11px] text-slate-400">No vehicle plugged in</p>
                )}

                <div className="mt-auto flex flex-wrap items-baseline justify-between gap-x-2 border-t border-slate-100 pt-1.5 text-[10px]">
                  <span
                    className="text-slate-500 tabular-nums"
                    title={
                      power != null && !power.manual && !(power.kw > 0)
                        ? 'The plan gives this car 0 kW in the current 15-minute slot — it is being ' +
                          'held for a better slot, not faulted. Charger rating ' +
                          `${charger.max_power_kw} kW.`
                        : `Charger rating ${charger.max_power_kw} kW`
                    }
                  >
                    {power == null ? (
                      <span className="text-slate-400">idle</span>
                    ) : power.manual || power.kw > 0 ? (
                      <>
                        <span className="text-[12px] font-semibold text-slate-900">
                          {power.kw.toFixed(1)} kW
                        </span>{' '}
                        <span className={power.manual ? 'text-amber-700' : 'text-slate-500'}>
                          {power.manual ? 'override' : 'planned'}
                        </span>
                      </>
                    ) : (
                      <>
                        <span className="text-[12px] font-semibold text-slate-900">0.0 kW</span>{' '}
                        <span className="text-slate-600">
                          {power.nextStartMs != null
                            ? `holding · from ${clockFmt.format(new Date(power.nextStartMs))}`
                            : session != null && session.soc_current >= session.soc_target - 1e-4
                              ? 'target reached'
                              : 'no charging planned'}
                        </span>
                      </>
                    )}
                  </span>
                  <span className={`${hb.tone} tabular-nums`} title={hb.title}>
                    {hb.label}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
