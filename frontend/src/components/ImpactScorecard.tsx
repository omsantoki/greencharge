/**
 * Headline impact: CO₂ saved, money saved and how many sessions are on track to
 * meet their target, from `GET /api/impact/summary` verbatim.
 *
 * Honesty rule: completed sessions contribute what actually happened, active
 * sessions contribute their remaining plan priced at the forecast — so the
 * caption says so instead of presenting a projection as banked savings.
 */

export type ImpactSummary = {
  co2_saved_kg: number;
  cost_saved_inr: number;
  sessions_on_time: number;
  total_sessions: number;
};

type Props = {
  data: ImpactSummary | null | undefined;
  className?: string;
};

const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const inrFine = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 });

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Zero at the precision the tile shows. A figure of -0.004 is a rounding residue, not a loss:
 * printed straight it reads "-0" in red, which claims a deficit the site does not have.
 */
function snap(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  const rounded = Math.round(value * factor) / factor;
  return rounded === 0 ? 0 : rounded;
}

function fmtKg(value: number): string {
  const abs = Math.abs(value);
  return abs >= 100 ? snap(value, 0).toFixed(0) : snap(value, 1).toFixed(1);
}

function fmtInr(value: number): string {
  return Math.abs(value) >= 100 ? inr.format(snap(value, 0)) : inrFine.format(snap(value, 1));
}

function toneFor(value: number | null, decimals = 1): string {
  if (value === null) return 'text-slate-400';
  const shown = snap(value, decimals);
  if (shown > 0) return 'text-emerald-600';
  if (shown < 0) return 'text-red-600';
  return 'text-slate-900';
}

function Tile({
  label,
  value,
  unit,
  tone,
  hint,
}: {
  label: string;
  value: string;
  unit?: string;
  tone: string;
  hint?: string;
}) {
  return (
    <div className="flex h-full min-w-0 flex-col justify-center rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
      <div className="truncate text-[11px] font-medium text-slate-500">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={`text-2xl font-semibold leading-none tabular-nums sm:text-3xl ${tone}`}>
          {value}
        </span>
        {unit ? <span className="text-xs text-slate-500">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-1 truncate text-[11px] text-slate-500">{hint}</div> : null}
    </div>
  );
}

export function ImpactScorecard({ data, className = '' }: Props) {
  const co2 = num(data?.co2_saved_kg);
  const cost = num(data?.cost_saved_inr);
  const onTime = num(data?.sessions_on_time);
  const total = num(data?.total_sessions);
  const noSessions = total !== null && total === 0;

  const allOnTime = onTime !== null && total !== null && total > 0 && onTime === total;

  return (
    <section
      className={`flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-sm ${className}`}
    >
      <h2 className="mb-2 shrink-0 text-sm font-semibold text-slate-900">Impact so far</h2>

      {!data ? (
        <div
          className="flex min-h-0 flex-1 items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 text-center text-sm text-slate-500"
          style={{ minHeight: 84 }}
        >
          No impact figures yet.
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-3 gap-2">
          <Tile
            label="CO₂ saved"
            value={noSessions || co2 === null ? '–' : fmtKg(co2)}
            unit="kg"
            tone={noSessions ? 'text-slate-400' : toneFor(co2)}
          />
          <Tile
            label="Cost saved"
            value={noSessions || cost === null ? '–' : `₹${fmtInr(cost)}`}
            tone={noSessions ? 'text-slate-400' : toneFor(cost)}
          />
          <Tile
            label="Sessions on time"
            value={
              noSessions || onTime === null || total === null ? '–' : `${onTime} / ${total}`
            }
            tone={noSessions ? 'text-slate-400' : allOnTime ? 'text-emerald-600' : 'text-slate-900'}
            hint={noSessions ? undefined : 'met or on track'}
          />
        </div>
      )}

      {data ? (
        <p className="mt-2 shrink-0 text-[11px] leading-snug text-slate-500">
          {noSessions
            ? 'No sessions yet — run a scenario to see savings.'
            : 'Against charging every car at full power on arrival — active sessions count their remaining plan at the forecast carbon intensity and tariff, so part of this is still projected.'}
        </p>
      ) : null}
    </section>
  );
}

export default ImpactScorecard;
