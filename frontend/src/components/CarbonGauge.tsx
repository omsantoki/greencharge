import { useMemo } from 'react';

/**
 * Current grid carbon intensity, on the same green → amber → red scale the
 * Gantt uses for its background.
 *
 * `data` is `GET /api/grid/latest` verbatim; the optional `forecast` is
 * `GET /api/grid/forecast?hours=24` and is only used to place the reading on
 * the same min → mean → max scale as the Gantt. Without it the reading is shown
 * without a colour claim, because there is nothing honest to rank it against.
 *
 * Honesty rule: a reading whose `source` is "estimated" comes from the
 * synthetic profile and is badged as such — never presented as measured.
 */

export type CarbonPoint = {
  ts: string;
  carbon_intensity: number;
  renewable_pct: number | null;
  fossil_pct: number | null;
  source: string;
};

type Props = {
  data: CarbonPoint | null | undefined;
  /** The 24 h forecast the Gantt is drawn from, for the shared colour scale. */
  forecast?: CarbonPoint[] | null;
  className?: string;
};

const SITE_TZ = 'Asia/Kolkata';
const UNIT = 'gCO₂eq/kWh';

const LOW: [number, number, number] = [22, 163, 74]; // #16a34a
const MID: [number, number, number] = [245, 158, 11]; // #f59e0b
const HIGH: [number, number, number] = [220, 38, 38]; // #dc2626

const dayHhmm = new Intl.DateTimeFormat('en-GB', {
  timeZone: SITE_TZ,
  weekday: 'short',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function rgb(c: [number, number, number]): string {
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

function mix(a: [number, number, number], b: [number, number, number], t: number): string {
  const k = clamp01(t);
  const ch = (i: number) => Math.round(a[i] + (b[i] - a[i]) * k);
  return `rgb(${ch(0)}, ${ch(1)}, ${ch(2)})`;
}

/** Diverging 3-stop scale over the horizon's own min → mean → max. */
function carbonColor(value: number, min: number, mean: number, max: number): string {
  if (!(max > min)) return rgb(MID);
  if (value <= mean) return mix(LOW, MID, mean > min ? (value - min) / (mean - min) : 1);
  return mix(MID, HIGH, max > mean ? (value - mean) / (max - mean) : 0);
}

type Scale = { min: number; mean: number; max: number };

function buildScale(forecast: CarbonPoint[] | null | undefined): Scale | null {
  const values = (forecast ?? [])
    .map((p) => p?.carbon_intensity)
    .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (!(max > min)) return null;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  return { min, mean, max };
}

export function CarbonGauge({ data, forecast, className = '' }: Props) {
  const scale = useMemo(() => buildScale(forecast), [forecast]);

  const value = data && Number.isFinite(data.carbon_intensity) ? data.carbon_intensity : null;
  const estimated = data?.source === 'estimated';
  const color = value !== null && scale ? carbonColor(value, scale.min, scale.mean, scale.max) : null;
  const position = value !== null && scale ? clamp01((value - scale.min) / (scale.max - scale.min)) : null;
  const meanStop = scale ? clamp01((scale.mean - scale.min) / (scale.max - scale.min)) : 0.5;
  const readingMs = data ? Date.parse(data.ts) : NaN;

  return (
    <section
      className={`flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-sm ${className}`}
    >
      <div className="mb-2 flex shrink-0 items-start justify-between gap-3">
        <h2 className="text-sm font-semibold text-slate-900">Grid carbon intensity</h2>
        {data ? (
          estimated ? (
            <span
              className="shrink-0 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700"
              title="Synthetic carbon profile — an estimate, not a measured reading."
            >
              estimated
            </span>
          ) : (
            <span
              className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] font-medium text-slate-600"
              title={`Source: ${data.source}`}
            >
              {data.source}
            </span>
          )
        ) : null}
      </div>

      {value === null ? (
        <div
          className="flex min-h-0 flex-1 items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 text-center text-sm text-slate-500"
          style={{ minHeight: 96 }}
        >
          No grid reading yet.
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col justify-between gap-2" style={{ minHeight: 104 }}>
          <div className="flex items-center gap-3">
            <span
              className="h-9 w-2.5 shrink-0 rounded-full border"
              style={{
                backgroundColor: color ?? '#cbd5e1',
                borderColor: color ? 'rgba(15, 23, 42, 0.15)' : '#94a3b8',
              }}
              aria-hidden="true"
            />
            <div className="flex items-baseline gap-1.5">
              <span className="text-4xl font-semibold leading-none tabular-nums text-slate-900">
                {Math.round(value)}
              </span>
              <span className="text-xs text-slate-500">{UNIT}</span>
            </div>
          </div>

          <div className="text-xs text-slate-500">
            reading at{' '}
            <span className="font-medium text-slate-700">
              {Number.isFinite(readingMs) ? `${dayHhmm.format(readingMs)} IST` : '--:--'}
            </span>
            {data && data.renewable_pct !== null && Number.isFinite(data.renewable_pct) ? (
              <>
                {' · '}
                <span className="font-medium text-slate-700">{Math.round(data.renewable_pct)}%</span>{' '}
                renewable
              </>
            ) : null}
          </div>

          {scale && position !== null ? (
            <div>
              <div
                className="relative h-2.5 w-full rounded-full"
                style={{
                  background: `linear-gradient(to right, ${rgb(LOW)} 0%, ${rgb(MID)} ${(
                    meanStop * 100
                  ).toFixed(1)}%, ${rgb(HIGH)} 100%)`,
                }}
                role="img"
                aria-label={`Carbon intensity ${Math.round(value)} ${UNIT}, between ${Math.round(
                  scale.min,
                )} and ${Math.round(scale.max)} over the next 24 hours`}
              >
                <div
                  className="absolute top-1/2 h-[18px] w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-slate-900 ring-2 ring-white"
                  style={{ left: `${(position * 100).toFixed(2)}%` }}
                />
              </div>
              <div className="mt-1.5 flex justify-between text-[11px] text-slate-500">
                <span>cleanest {Math.round(scale.min)}</span>
                <span className="text-slate-400">next 24 h</span>
                <span>dirtiest {Math.round(scale.max)}</span>
              </div>
            </div>
          ) : (
            <div className="text-[11px] text-slate-400">
              24 h range unavailable — no colour ranking for this reading.
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default CarbonGauge;
