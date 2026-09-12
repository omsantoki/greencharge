import { useMemo, useState } from 'react';
import type { ActiveSession, CarbonPoint } from '../api/client';

/**
 * ScheduleGantt — the hero component.
 *
 * Hand-rolled SVG (no chart library). 96 quarter-hour slots across the next 24 h,
 * taken from the carbon forecast timestamps. The background is one rect per slot
 * coloured by forecast carbon intensity; each active session gets a row whose
 * blocks show the planned kW. The visual claim the operator must be able to make
 * at a glance: "the charging is avoiding the red parts".
 *
 * All timestamps come from the API (simulated time) and are rendered in the site
 * timezone. The browser clock is never read.
 */

const SITE_TZ = 'Asia/Kolkata';
const SLOT_COUNT = 96;
const SLOT_MS_FALLBACK = 15 * 60 * 1000;
const LABEL_EVERY = 4; // hour labels every 4 slots

/* ---- geometry (SVG user units; the <svg> scales to the container width) ---- */
const VB_W = 1200;
const LABEL_W = 240;
const RIGHT_PAD = 14;
const PLOT_X = LABEL_W;
const PLOT_W = VB_W - LABEL_W - RIGHT_PAD;
const PAD_TOP = 10;
const AXIS_H = 24;
const PAD_BOTTOM = 16; // room under the band for the "now" label, so it never covers the first row
const BAND_TOP = PAD_TOP + AXIS_H;
const EMPTY_BAND_H = 76;

/* ---- carbon colour scale (spec: low green, mid amber, high red) ---- */
type RGB = [number, number, number];
const CI_LOW: RGB = [22, 163, 74]; // #16a34a
const CI_MID: RGB = [245, 158, 11]; // #f59e0b
const CI_HIGH: RGB = [220, 38, 38]; // #dc2626
const BG_OPACITY = 0.45;
const NEUTRAL_SLOT = '#cbd5e1';

/* ---- charging blocks ---- */
const BLOCK_FILL = '#0f172a';
const BLOCK_MIN_OPACITY = 0.15;

const hhmm = new Intl.DateTimeFormat('en-GB', {
  timeZone: SITE_TZ,
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

function fmtTime(ms: number): string {
  return Number.isFinite(ms) ? hhmm.format(new Date(ms)) : '--:--';
}

function rgbOf(c: RGB): string {
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

function mix(a: RGB, b: RGB, t: number): string {
  const k = t < 0 ? 0 : t > 1 ? 1 : t;
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * k)}, ${Math.round(
    a[1] + (b[1] - a[1]) * k,
  )}, ${Math.round(a[2] + (b[2] - a[2]) * k)})`;
}

type CiStats = { min: number; mean: number; max: number; count: number };

/** Diverging low→mid→high scale over the horizon's OWN range, midpoint at the mean. */
function ciColor(v: number | null, s: CiStats): string {
  if (v === null || !Number.isFinite(v) || s.count === 0) return NEUTRAL_SLOT;
  if (s.max - s.min < 1e-9) return rgbOf(CI_MID);
  if (v <= s.mean) {
    const span = s.mean - s.min;
    return span < 1e-9 ? rgbOf(CI_MID) : mix(CI_LOW, CI_MID, (v - s.min) / span);
  }
  const span = s.max - s.mean;
  return span < 1e-9 ? rgbOf(CI_MID) : mix(CI_MID, CI_HIGH, (v - s.mean) / span);
}

function pct(v: number): string {
  return `${Math.round(v * 100)}%`;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

type Row = {
  key: number;
  top: string; // "CP001 · Tata Nexon EV"
  sub: string; // "SoC 34% → 80% by 07:00"
  maxKw: number;
  deadlineMs: number;
  planned: boolean; // the optimizer has produced a plan for this session (it may be all zeros)
  blocks: { i: number; kw: number }[];
};

type Tip = {
  xPct: number;
  above: boolean;
  yPct: number;
  range: string;
  ci: number | null;
  kw: number | null;
  who: string | null;
};

export type ScheduleGanttProps = {
  sessions: ActiveSession[];
  forecast: CarbonPoint[];
  now: string;
};

export function ScheduleGantt({ sessions, forecast, now }: ScheduleGanttProps) {
  const [tip, setTip] = useState<Tip | null>(null);

  const model = useMemo(() => {
    const list = Array.isArray(forecast) ? forecast : [];
    const pts = list.slice(0, SLOT_COUNT);
    const stamps = pts.map((p) => Date.parse(p?.ts as unknown as string));

    // Slot length comes from the forecast itself; fall back to the 15-minute slot.
    let slotMs = SLOT_MS_FALLBACK;
    if (stamps.length >= 2 && Number.isFinite(stamps[0]) && Number.isFinite(stamps[1])) {
      const d = stamps[1] - stamps[0];
      if (d > 0) slotMs = d;
    }

    const nowMs = Date.parse(now);
    const hasForecast = stamps.length > 0 && Number.isFinite(stamps[0]);
    const startMs = hasForecast
      ? stamps[0]
      : Number.isFinite(nowMs)
        ? Math.floor(nowMs / slotMs) * slotMs
        : NaN;
    const n = hasForecast ? pts.length : SLOT_COUNT;

    const ci: (number | null)[] = [];
    for (let i = 0; i < n; i += 1) {
      const v = hasForecast ? Number(pts[i]?.carbon_intensity) : NaN;
      ci.push(Number.isFinite(v) ? v : null);
    }

    let min = Infinity;
    let max = -Infinity;
    let sum = 0;
    let count = 0;
    for (const v of ci) {
      if (v === null) continue;
      if (v < min) min = v;
      if (v > max) max = v;
      sum += v;
      count += 1;
    }
    const stats: CiStats = count
      ? { min, max, mean: sum / count, count }
      : { min: 0, max: 0, mean: 0, count: 0 };

    const estimated = pts.some((p) => p?.source === 'estimated');

    const ordered = (Array.isArray(sessions) ? sessions.slice() : []).sort((a, b) => {
      const byId = String(a?.ocpp_id ?? '').localeCompare(String(b?.ocpp_id ?? ''));
      return byId !== 0 ? byId : Number(a?.id ?? 0) - Number(b?.id ?? 0);
    });

    const rows: Row[] = ordered.map((s) => {
      const byIndex = new Map<number, number>();
      const slots = Array.isArray(s?.schedule) ? s.schedule : [];
      for (const slot of slots) {
        const t = Date.parse(slot?.slot_start as unknown as string);
        const kw = Number(slot?.power_kw);
        if (!Number.isFinite(t) || !Number.isFinite(startMs) || !Number.isFinite(kw)) continue;
        if (kw <= 0) continue;
        const i = Math.round((t - startMs) / slotMs);
        if (i < 0 || i >= n) continue;
        byIndex.set(i, Math.max(byIndex.get(i) ?? 0, kw));
      }
      const blocks = Array.from(byIndex.entries())
        .map(([i, kw]) => ({ i, kw }))
        .sort((a, b) => a.i - b.i);

      const rated = Number(s?.max_charge_kw);
      const peak = blocks.reduce((m, b) => (b.kw > m ? b.kw : m), 0);
      const maxKw = Number.isFinite(rated) && rated > 0 ? rated : peak > 0 ? peak : 1;

      const socNow = Number(s?.soc_current);
      const socTarget = Number(s?.soc_target);
      const socFrom = Number.isFinite(socNow) ? socNow : Number(s?.soc_start);
      const deadlineMs = Date.parse(s?.deadline as unknown as string);

      return {
        key: Number(s?.id ?? 0),
        top: `${s?.ocpp_id ?? '—'} · ${s?.vehicle_model ?? 'Vehicle'}`,
        sub: `SoC ${Number.isFinite(socFrom) ? pct(socFrom) : '—'} → ${
          Number.isFinite(socTarget) ? pct(socTarget) : '—'
        } by ${fmtTime(deadlineMs)}`,
        maxKw,
        deadlineMs,
        planned: slots.length > 0,
        blocks,
      };
    });

    return { n, slotMs, startMs, ci, stats, estimated, rows, nowMs };
  }, [sessions, forecast, now]);

  const { n, slotMs, startMs, ci, stats, estimated, rows, nowMs } = model;

  const slotW = PLOT_W / Math.max(n, 1);
  const rowH = rows.length <= 1 ? 54 : rows.length === 2 ? 46 : rows.length <= 4 ? 40 : 34;
  const bandH = rows.length ? rows.length * rowH : EMPTY_BAND_H;
  const bandBottom = BAND_TOP + bandH;
  const vbH = bandBottom + PAD_BOTTOM;
  const blockInset = Math.max(4, Math.min(7, (rowH - 20) / 2));
  const blockH = rowH - blockInset * 2;

  const nowX =
    Number.isFinite(nowMs) && Number.isFinite(startMs)
      ? PLOT_X + ((nowMs - startMs) / slotMs) * slotW
      : NaN;
  const nowVisible = Number.isFinite(nowX) && nowX >= PLOT_X - 0.5 && nowX <= PLOT_X + PLOT_W + 0.5;
  const nowFlip = nowVisible && nowX > PLOT_X + PLOT_W - 40;

  const midPct = stats.count && stats.max - stats.min > 1e-9
    ? clamp(((stats.mean - stats.min) / (stats.max - stats.min)) * 100, 4, 96)
    : 50;

  function slotRange(i: number): string {
    if (!Number.isFinite(startMs)) return '--:-- – --:--';
    return `${fmtTime(startMs + i * slotMs)} – ${fmtTime(startMs + (i + 1) * slotMs)}`;
  }

  function showTip(i: number, rowTop: number, rowBottom: number, kw: number | null, who: string | null) {
    const anchorAbove = rowTop - 4;
    const above = anchorAbove >= 62;
    setTip({
      xPct: clamp(((PLOT_X + (i + 0.5) * slotW) / VB_W) * 100, 9, 91),
      above,
      yPct: ((above ? anchorAbove : rowBottom + 4) / vbH) * 100,
      range: slotRange(i),
      ci: ci[i] ?? null,
      kw,
      who,
    });
  }

  const hourTicks: number[] = [];
  for (let i = 0; i < n; i += LABEL_EVERY) hourTicks.push(i);

  return (
    <div className="w-full px-4 pb-2 pt-3">
      {/* legend */}
      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-5 gap-y-2">
        <p className="text-[11px] leading-4 text-slate-500">
          Planned charging over the next 24 h — background is the forecast grid carbon intensity.
        </p>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] font-medium text-slate-500">cleaner</span>
            <span
              className="inline-block h-2.5 w-28 rounded-sm ring-1 ring-inset ring-black/10"
              style={{
                background: `linear-gradient(90deg, ${rgbOf(CI_LOW)} 0%, ${rgbOf(
                  CI_MID,
                )} ${midPct}%, ${rgbOf(CI_HIGH)} 100%)`,
              }}
            />
            <span className="text-[10px] font-medium text-slate-500">dirtier</span>
            {stats.count > 0 ? (
              <span className="ml-1 text-[10px] tabular-nums text-slate-500">
                {Math.round(stats.min)}–{Math.round(stats.max)} gCO₂eq/kWh
              </span>
            ) : null}
          </div>
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-2.5 rounded-[2px]"
              style={{ backgroundColor: BLOCK_FILL }}
            />
            <span className="text-[10px] text-slate-500">planned kW (darker = more power)</span>
          </div>
          {rows.length ? (
            <div className="flex items-center gap-1.5">
              <svg width="12" height="10" aria-hidden="true">
                <line
                  x1="6"
                  x2="6"
                  y1="0"
                  y2="10"
                  stroke="#0f172a"
                  strokeWidth="1"
                  strokeDasharray="2 3"
                  opacity="0.55"
                />
              </svg>
              <span className="text-[10px] text-slate-500">departure deadline</span>
            </div>
          ) : null}
          {rows.length ? (
            <div className="flex items-center gap-1.5">
              <svg width="12" height="10" aria-hidden="true">
                <rect x="0" y="0" width="12" height="10" fill="#94a3b8" opacity="0.38" />
              </svg>
              <span className="text-[10px] text-slate-500">car has left — not chargeable</span>
            </div>
          ) : null}
          {estimated ? (
            <div className="flex items-center gap-1.5">
              <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
                estimated
              </span>
              <span className="text-[10px] text-slate-500">synthetic grid profile — not measured</span>
            </div>
          ) : null}
        </div>
      </div>

      {/* chart + tooltip live in the same relative box so the tooltip can be placed in % */}
      <div className="relative w-full">
        <svg
          viewBox={`0 0 ${VB_W} ${vbH}`}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label={`Charging schedule for ${rows.length} active session${
            rows.length === 1 ? '' : 's'
          } over the next 24 hours, on a carbon-intensity background.`}
          style={{ display: 'block', width: '100%', height: 'auto' }}
          onMouseLeave={() => setTip(null)}
        >
          {/* carbon background: one rect per slot, no stroke */}
          <g shapeRendering="crispEdges">
            {ci.map((v, i) => (
              <rect
                key={`bg-${i}`}
                x={PLOT_X + i * slotW}
                y={BAND_TOP}
                width={slotW}
                height={bandH}
                fill={ciColor(v, stats)}
                opacity={BG_OPACITY}
                onMouseEnter={() => showTip(i, BAND_TOP, BAND_TOP, null, null)}
              />
            ))}
          </g>

          {/* hour labels + ticks */}
          <g>
            {hourTicks.map((i) => (
              <g key={`tick-${i}`}>
                <line
                  x1={PLOT_X + i * slotW}
                  x2={PLOT_X + i * slotW}
                  y1={BAND_TOP - 5}
                  y2={BAND_TOP}
                  stroke="#94a3b8"
                  strokeWidth={0.8}
                />
                <text
                  x={PLOT_X + i * slotW}
                  y={PAD_TOP + 12}
                  textAnchor="middle"
                  fontSize={9.5}
                  fill="#64748b"
                >
                  {Number.isFinite(startMs) ? fmtTime(startMs + i * slotMs) : '--:--'}
                </text>
              </g>
            ))}
            <line
              x1={PLOT_X + PLOT_W}
              x2={PLOT_X + PLOT_W}
              y1={BAND_TOP - 5}
              y2={BAND_TOP}
              stroke="#94a3b8"
              strokeWidth={0.8}
            />
          </g>

          {/* session rows */}
          {rows.map((row, r) => {
            const rowTop = BAND_TOP + r * rowH;
            const rowBottom = rowTop + rowH;
            // the departure deadline this row's plan has to fit inside (already named in the label)
            const dx =
              Number.isFinite(row.deadlineMs) && Number.isFinite(startMs)
                ? PLOT_X + ((row.deadlineMs - startMs) / slotMs) * slotW
                : NaN;
            const dxVisible = Number.isFinite(dx) && dx > PLOT_X + 1 && dx < PLOT_X + PLOT_W - 1;
            return (
              <g key={row.key}>
                {r > 0 ? (
                  <line
                    x1={PLOT_X}
                    x2={PLOT_X + PLOT_W}
                    y1={rowTop}
                    y2={rowTop}
                    stroke="#ffffff"
                    strokeWidth={1}
                    opacity={0.55}
                    shapeRendering="crispEdges"
                  />
                ) : null}
                <text
                  x={0}
                  y={rowTop + rowH / 2 - 1}
                  fontSize={11.5}
                  fontWeight={600}
                  fill="#0f172a"
                >
                  {row.top}
                </text>
                <text x={0} y={rowTop + rowH / 2 + 11} fontSize={10} fill="#64748b">
                  {row.sub}
                </text>

                {/* After its departure the car is gone, so no plan can use that time. Greying it
                    answers the obvious question about the empty midday green window: the cars
                    left at 07:00 and cannot reach it. */}
                {dxVisible ? (
                  <rect
                    x={dx}
                    y={rowTop}
                    width={PLOT_X + PLOT_W - dx}
                    height={rowH}
                    fill="#94a3b8"
                    opacity={0.38}
                    pointerEvents="none"
                    shapeRendering="crispEdges"
                  />
                ) : null}

                <g shapeRendering="crispEdges">
                  {row.blocks.map((b) => (
                    <rect
                      key={`b-${row.key}-${b.i}`}
                      x={PLOT_X + b.i * slotW}
                      y={rowTop + blockInset}
                      width={slotW}
                      height={blockH}
                      fill={BLOCK_FILL}
                      opacity={clamp(b.kw / row.maxKw, BLOCK_MIN_OPACITY, 1)}
                      onMouseEnter={() => showTip(b.i, rowTop, rowBottom, b.kw, row.top)}
                    />
                  ))}
                </g>

                {dxVisible ? (
                  <line
                    x1={dx}
                    x2={dx}
                    y1={rowTop + 2}
                    y2={rowBottom - 2}
                    stroke="#0f172a"
                    strokeWidth={1}
                    strokeDasharray="2 3"
                    opacity={0.55}
                    pointerEvents="none"
                  />
                ) : null}

                {/*
                  Name the deadline once, on the top row: it is the reason the plan stops where it
                  does and never reaches a cleaner window later in the day. Nothing is ever planned
                  after a session's deadline, so a label to the RIGHT of the tick covers no block —
                  and it is skipped on a row that is showing its "nothing planned" message instead.
                */}
                {dxVisible && r === 0 && row.blocks.length > 0 && dx + 70 < PLOT_X + PLOT_W ? (
                  <text
                    x={dx + 4}
                    y={rowTop + rowH / 2 + 3}
                    fontSize={9}
                    fill="#334155"
                    stroke="#ffffff"
                    strokeWidth={2.4}
                    style={{ paintOrder: 'stroke' }}
                    pointerEvents="none"
                  >
                    {`leaves ${fmtTime(row.deadlineMs)}`}
                  </text>
                ) : null}

                {row.blocks.length === 0 ? (
                  <text
                    x={PLOT_X + 8}
                    y={rowTop + rowH / 2 + 3}
                    fontSize={10}
                    fill="#475569"
                    stroke="#ffffff"
                    strokeWidth={2.6}
                    style={{ paintOrder: 'stroke' }}
                  >
                    {row.planned
                      ? 'no charging planned in this window'
                      : 'no plan yet — waiting for the next optimizer tick'}
                  </text>
                ) : null}
              </g>
            );
          })}

          {/* empty state */}
          {rows.length === 0 ? (
            <text
              x={PLOT_X + PLOT_W / 2}
              y={BAND_TOP + bandH / 2 + 4}
              textAnchor="middle"
              fontSize={12}
              fill="#334155"
              stroke="#ffffff"
              strokeWidth={3}
              style={{ paintOrder: 'stroke' }}
            >
              {stats.count === 0
                ? 'Waiting for data — no plan and no carbon forecast have arrived yet.'
                : 'No active sessions — the next 24 h of grid carbon intensity is shown.'}
            </text>
          ) : null}

          {/* "now" line */}
          {nowVisible ? (
            <g pointerEvents="none">
              <line
                x1={nowX}
                x2={nowX}
                y1={BAND_TOP - 8}
                y2={bandBottom}
                stroke="#ffffff"
                strokeWidth={3.4}
                opacity={0.9}
              />
              <line
                x1={nowX}
                x2={nowX}
                y1={BAND_TOP - 8}
                y2={bandBottom}
                stroke="#0f172a"
                strokeWidth={1.6}
              />
              <path
                d={`M ${nowX - 4} ${BAND_TOP - 8} L ${nowX + 4} ${BAND_TOP - 8} L ${nowX} ${
                  BAND_TOP - 2
                } Z`}
                fill="#0f172a"
              />
              {/* the label sits UNDER the band: inside it, it would cover the first row's blocks */}
              <text
                x={nowFlip ? nowX - 5 : nowX + 5}
                y={bandBottom + 11}
                textAnchor={nowFlip ? 'end' : 'start'}
                fontSize={9}
                fontWeight={700}
                fill="#0f172a"
              >
                now
              </text>
            </g>
          ) : null}

          <rect
            x={PLOT_X}
            y={BAND_TOP}
            width={PLOT_W}
            height={bandH}
            fill="none"
            stroke="#cbd5e1"
            strokeWidth={1}
            pointerEvents="none"
            shapeRendering="crispEdges"
          />
        </svg>

        {tip ? (
          <div
            className="pointer-events-none absolute z-10 whitespace-nowrap rounded-md bg-slate-900/95 px-2.5 py-1.5 text-[11px] leading-4 text-white shadow-lg ring-1 ring-black/10"
            style={{
              left: `${tip.xPct}%`,
              top: `${tip.yPct}%`,
              transform: tip.above ? 'translate(-50%, -100%)' : 'translate(-50%, 0)',
            }}
          >
            {tip.who ? <div className="font-semibold">{tip.who}</div> : null}
            <div className="tabular-nums text-slate-200">{tip.range} IST</div>
            <div className="tabular-nums">
              {tip.kw !== null ? `${tip.kw.toFixed(1)} kW planned` : 'no charging planned'}
            </div>
            <div className="tabular-nums text-slate-300">
              {tip.ci !== null ? `${Math.round(tip.ci)} gCO₂eq/kWh` : 'carbon intensity unavailable'}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default ScheduleGantt;
