import { useMemo } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

/**
 * Site load curve: the optimized site load against the naive baseline, with the
 * site's power limit drawn as a red dashed line.
 *
 * Data shape is `GET /api/sites/{id}/load-curve` verbatim. All timestamps come
 * from the API (simulated time) and are formatted in the site timezone; the
 * browser clock is never used.
 */

export type LoadCurveSlot = {
  slot_start: string;
  optimized_kw: number;
  baseline_kw: number;
  is_past: boolean;
};

export type LoadCurveData = {
  site_id: number;
  max_power_kw: number;
  window_start: string;
  now: string;
  optimized_peak_kw: number;
  baseline_peak_kw: number;
  slots: LoadCurveSlot[];
};

type Props = {
  data: LoadCurveData | null | undefined;
  className?: string;
};

const SITE_TZ = 'Asia/Kolkata';
const MIN_CHART_HEIGHT = 150; // the chart fills its panel; this is the floor if the panel is unsized
const DEFAULT_SLOT_MS = 15 * 60 * 1000;

const OPTIMIZED = '#059669'; // emerald 600
const BASELINE = '#64748b'; // slate 500
const LIMIT = '#dc2626'; // red 600

const hhmm = new Intl.DateTimeFormat('en-GB', {
  timeZone: SITE_TZ,
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});
const dayHhmm = new Intl.DateTimeFormat('en-GB', {
  timeZone: SITE_TZ,
  weekday: 'short',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

function fmtTime(iso: string | number): string {
  const ms = typeof iso === 'number' ? iso : Date.parse(iso);
  return Number.isFinite(ms) ? hhmm.format(ms) : '--:--';
}

function fmtDayTime(iso: string | number): string {
  const ms = typeof iso === 'number' ? iso : Date.parse(iso);
  return Number.isFinite(ms) ? dayHhmm.format(ms) : '--:--';
}

function kw(value: number, digits = 1): string {
  return Number.isFinite(value) ? value.toFixed(digits) : '–';
}

/** A round axis maximum that leaves headroom above the tallest line. */
function niceCeiling(raw: number): { max: number; ticks: number } {
  const target = raw * 1.12;
  const step = target <= 25 ? 5 : target <= 60 ? 10 : target <= 150 ? 20 : 50;
  const max = Math.max(step, Math.ceil(target / step) * step);
  return { max, ticks: Math.round(max / step) + 1 };
}

type Row = {
  i: number;
  optimized: number;
  baseline: number;
  startMs: number;
  endMs: number;
  isPast: boolean;
};

function Placeholder({ message }: { message: string }) {
  return (
    <div
      className="flex min-h-0 flex-1 items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 text-center text-sm text-slate-500"
      style={{ minHeight: MIN_CHART_HEIGHT }}
    >
      {message}
    </div>
  );
}

function LoadCurveTooltip({ active, payload }: any) {
  if (!active || !payload || payload.length === 0) return null;
  const row: Row | undefined = payload[0]?.payload;
  if (!row) return null;
  return (
    <div className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-medium text-slate-700">
        {fmtDayTime(row.startMs)} – {fmtTime(row.endMs)}
        <span className="ml-1 font-normal text-slate-400">{row.isPast ? '(metered)' : '(planned)'}</span>
      </div>
      <div className="flex items-center justify-between gap-4">
        <span className="flex items-center gap-1.5 text-slate-600">
          <span className="inline-block h-0.5 w-3.5 rounded" style={{ backgroundColor: OPTIMIZED }} />
          Optimized
        </span>
        <span className="font-semibold tabular-nums text-slate-900">{kw(row.optimized)} kW</span>
      </div>
      <div className="flex items-center justify-between gap-4">
        <span className="flex items-center gap-1.5 text-slate-600">
          <span className="inline-block h-0.5 w-3.5 rounded" style={{ backgroundColor: BASELINE }} />
          Baseline
        </span>
        <span className="font-semibold tabular-nums text-slate-900">{kw(row.baseline)} kW</span>
      </div>
    </div>
  );
}

export function LoadCurve({ data, className = '' }: Props) {
  const model = useMemo(() => {
    const slots = data?.slots ?? [];
    if (!data || slots.length === 0) return null;

    const firstMs = Date.parse(slots[0].slot_start);
    const secondMs = slots.length > 1 ? Date.parse(slots[1].slot_start) : NaN;
    const slotMs =
      Number.isFinite(firstMs) && Number.isFinite(secondMs) && secondMs > firstMs
        ? secondMs - firstMs
        : DEFAULT_SLOT_MS;

    const rows: Row[] = slots.map((s, i) => {
      const startMs = Date.parse(s.slot_start);
      return {
        i,
        optimized: Number.isFinite(s.optimized_kw) ? s.optimized_kw : 0,
        baseline: Number.isFinite(s.baseline_kw) ? s.baseline_kw : 0,
        startMs,
        endMs: startMs + slotMs,
        isPast: Boolean(s.is_past),
      };
    });

    const limit = Number.isFinite(data.max_power_kw) ? data.max_power_kw : 0;
    const observedPeak = rows.reduce((m, r) => Math.max(m, r.optimized, r.baseline), 0);
    const { max: yMax, ticks: tickCount } = niceCeiling(
      Math.max(observedPeak, data.baseline_peak_kw || 0, data.optimized_peak_kw || 0, limit),
    );

    const xTicks: number[] = [];
    for (let i = 0; i < rows.length; i += 8) xTicks.push(i);

    const nowMs = Date.parse(data.now);
    const nowIndex =
      Number.isFinite(nowMs) && Number.isFinite(rows[0].startMs)
        ? Math.min(rows.length - 1, Math.max(0, (nowMs - rows[0].startMs) / slotMs))
        : null;

    // The API's optimized curve is metered load before `now` and the plan after it. Keeping the
    // two peaks apart lets the header say which half a limit breach came from, without hiding it.
    let plannedPeak = 0;
    for (const row of rows) if (!row.isPast) plannedPeak = Math.max(plannedPeak, row.optimized);

    return { rows, limit, yMax, tickCount, xTicks, nowIndex, slotMs, plannedPeak };
  }, [data]);

  // Any peak above the site limit is flagged, optimized included: the optimized
  // curve is metered load in past slots and can overshoot the plan between ticks.
  const overLimit = (value: number) => Boolean(model) && value > model!.limit + 1e-6;

  // A peak above the limit that no planned slot reaches did not come from the optimizer: it is
  // metered load a charge point drew at its own rating before a schedule reached it (a charge
  // point runs at its rating until the first SetChargingProfile). Keep it red — the site really
  // drew it — but do not publish it under the word "optimized", which reads as the optimizer
  // having failed. Same condition as the annotation below, so label and note always agree.
  const unmanagedPeak =
    Boolean(data) &&
    Boolean(model) &&
    overLimit(data!.optimized_peak_kw) &&
    !overLimit(model!.plannedPeak);

  return (
    <section
      className={`flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-sm ${className}`}
    >
      <div className="mb-1 flex shrink-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-sm font-semibold text-slate-900">Site load — optimized vs. naive baseline</h2>
        {data && model ? (
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-slate-500">
            <span>
              {unmanagedPeak ? 'Peak site load' : 'Optimized peak'}{' '}
              <span
                className={`font-semibold tabular-nums ${
                  overLimit(data.optimized_peak_kw) ? 'text-red-600' : 'text-slate-900'
                }`}
              >
                {kw(data.optimized_peak_kw)} kW
              </span>
              {unmanagedPeak ? (
                <span
                  className="ml-1 text-slate-500"
                  title={
                    model.plannedPeak > 0
                      ? 'The over-limit slot is metered, not planned. A past slot is the sum of ' +
                        "each charger's own average over its 10-second meter samples, and because " +
                        'the chargers sample at staggered instants that sum can land slightly ' +
                        'above the instantaneous site load. Every planned slot is at or below the ' +
                        'site limit.'
                      : 'The over-limit slot is metered, not planned: no schedule is in force yet, ' +
                        'so each charge point is drawing at its own rating. The figure is real ' +
                        'site load, not an optimized peak; it falls under the limit once the ' +
                        'first plan reaches the chargers.'
                  }
                >
                  {model.plannedPeak > 0
                    ? `(metered — planned peak ${kw(model.plannedPeak)} kW)`
                    : '(metered — no plan in force yet)'}
                </span>
              ) : null}
            </span>
            <span aria-hidden="true">·</span>
            <span>
              Baseline peak{' '}
              <span
                className={`font-semibold tabular-nums ${
                  overLimit(data.baseline_peak_kw) ? 'text-red-600' : 'text-slate-900'
                }`}
              >
                {kw(data.baseline_peak_kw)} kW
              </span>
            </span>
          </div>
        ) : null}
      </div>

      <div className="mb-1.5 flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-[3px] w-4 rounded" style={{ backgroundColor: OPTIMIZED }} />
          Optimized
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-[3px] w-4 rounded" style={{ backgroundColor: BASELINE }} />
          Baseline (charge on arrival)
        </span>
        {model ? (
          <span className="flex items-center gap-1.5">
            <svg width="18" height="6" aria-hidden="true">
              <line x1="0" y1="3" x2="18" y2="3" stroke={LIMIT} strokeWidth="2" strokeDasharray="5 3" />
            </svg>
            Site limit {kw(model.limit, 0)} kW
          </span>
        ) : null}
      </div>

      {!data || !model ? (
        <Placeholder message="No load curve yet — it appears once the API reports sessions on this site." />
      ) : (
        <div className="min-h-0 w-full min-w-0 flex-1" style={{ minHeight: MIN_CHART_HEIGHT }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={model.rows} margin={{ top: 16, right: 12, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="#e2e8f0" strokeDasharray="3 3" vertical={false} />
              <ReferenceArea
                y1={model.limit}
                y2={model.yMax}
                fill={LIMIT}
                fillOpacity={0.06}
                stroke="none"
                ifOverflow="hidden"
              />
              <XAxis
                dataKey="i"
                type="number"
                domain={[0, model.rows.length - 1]}
                ticks={model.xTicks}
                interval={0}
                allowDecimals={false}
                tickFormatter={(i: number) => {
                  const row = model.rows[Math.round(i)];
                  return row ? fmtTime(row.startMs) : '';
                }}
                tick={{ fontSize: 11, fill: '#64748b' }}
                tickLine={false}
                axisLine={{ stroke: '#cbd5e1' }}
                minTickGap={0}
              />
              <YAxis
                domain={[0, model.yMax]}
                tickCount={model.tickCount}
                tick={{ fontSize: 11, fill: '#64748b' }}
                tickLine={false}
                axisLine={false}
                width={44}
                label={{
                  value: 'kW',
                  angle: -90,
                  position: 'insideLeft',
                  offset: 14,
                  style: { fontSize: 11, fill: '#94a3b8' },
                }}
              />
              <Tooltip
                content={<LoadCurveTooltip />}
                isAnimationActive={false}
                animationDuration={0}
                cursor={{ stroke: '#94a3b8', strokeDasharray: '3 3' }}
              />
              <ReferenceLine
                y={model.limit}
                stroke={LIMIT}
                strokeWidth={2}
                strokeDasharray="6 4"
                ifOverflow="extendDomain"
                isFront
              />
              {model.nowIndex === null ? null : (
                <ReferenceLine
                  x={model.nowIndex}
                  stroke="#0f172a"
                  strokeWidth={1}
                  strokeDasharray="2 3"
                  label={{ value: 'now', position: 'top', fontSize: 10, fill: '#0f172a' }}
                />
              )}
              <Line
                type="linear"
                dataKey="baseline"
                name="Baseline"
                stroke={BASELINE}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 0 }}
                isAnimationActive={false}
                animationDuration={0}
              />
              <Line
                type="linear"
                dataKey="optimized"
                name="Optimized"
                stroke={OPTIMIZED}
                strokeWidth={2.5}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 0 }}
                isAnimationActive={false}
                animationDuration={0}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <p className="mt-1.5 shrink-0 text-[11px] leading-snug text-slate-500">
        Baseline = every car charging at full power from the moment it plugs in. Before the now line the
        optimized curve is metered site load; after it, the current plan.
      </p>
    </section>
  );
}

export default LoadCurve;
