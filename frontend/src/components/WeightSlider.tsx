import { useCallback, useEffect, useRef, useState } from 'react';
import { postWeights as postWeightsApi } from '../api/client';
import type { TickInfo, WeightsResponse } from '../api/client';

/**
 * WeightSlider — the optimizer's objective weights.
 *
 *   minimise  Σ_s Σ_t p[s][t] · Δt · ( α · carbon[t]/1000 + β · price[t] )
 *
 * Moving a slider POSTs /api/optimizer/weights (debounced ~300 ms). The backend
 * sets the weights, re-runs the tick synchronously and returns it, so the page
 * can refresh the Gantt immediately from `onApplied`.
 */

export type WeightsTick = TickInfo;
export type { WeightsResponse };

export interface WeightSliderProps {
  /** Initial α (carbon weight). The optimizer's default is 1.0. */
  alpha?: number;
  /** Initial β (cost weight). The optimizer's default is 0.001. */
  beta?: number;
  /** Called with the tick returned by the POST, so the page can refetch at once. */
  onApplied?: (tick: TickInfo) => void;
  /** Override the POST (tests, or a different client). */
  postWeights?: (alpha: number, beta: number) => Promise<WeightsResponse>;
  /** Lock the controls (e.g. while the API is unreachable). */
  disabled?: boolean;
}

const DEFAULT_ALPHA = 1.0;
const DEFAULT_BETA = 0.001;
const ALPHA_STEP = 0.05;
const BETA_STEP = 0.005;
const DEBOUNCE_MS = 300;

const TZ = 'Asia/Kolkata';

const timeFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function describeError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  if (err && typeof err === 'object') {
    const candidate = err as { status?: unknown; body?: unknown };
    if (typeof candidate.status === 'number') {
      return `HTTP ${candidate.status} — ${String(candidate.body ?? '').slice(0, 200)}`;
    }
  }
  return String(err);
}

function round(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

const STATUS_TONE: Record<string, string> = {
  optimal: 'border-emerald-300 bg-emerald-50 text-emerald-800',
  relaxed: 'border-amber-300 bg-amber-50 text-amber-800',
  infeasible: 'border-red-300 bg-red-50 text-red-800',
};

const OBJECTIVE =
  'Objective: minimise Σ power × Δt × (α · carbon/1000 + β · tariff). ' +
  'α weights grid CO₂ (gCO₂/kWh), β weights money (₹/kWh). ' +
  'Optimizer defaults: α 1.00, β 0.001.';

export default function WeightSlider({
  alpha: alphaProp = DEFAULT_ALPHA,
  beta: betaProp = DEFAULT_BETA,
  onApplied,
  postWeights = postWeightsApi,
  disabled = false,
}: WeightSliderProps) {
  const [alpha, setAlpha] = useState(alphaProp);
  const [beta, setBeta] = useState(betaProp);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState<TickInfo | null>(null);
  const [applied, setApplied] = useState<{ alpha: number; beta: number } | null>(null);

  const mounted = useRef(true);
  const touched = useRef(false);
  const inFlight = useRef(false);
  const queued = useRef<[number, number] | null>(null);
  const postRef = useRef(postWeights);
  const onAppliedRef = useRef(onApplied);

  useEffect(() => {
    postRef.current = postWeights;
    onAppliedRef.current = onApplied;
  });

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const send = useCallback(async (nextAlpha: number, nextBeta: number) => {
    if (inFlight.current) {
      queued.current = [nextAlpha, nextBeta];
      return;
    }
    inFlight.current = true;
    if (mounted.current) setBusy(true);
    let pending: [number, number] | null = [nextAlpha, nextBeta];
    while (pending) {
      const [a, b] = pending;
      pending = null;
      try {
        const result = await postRef.current(a, b);
        if (mounted.current) {
          setTick(result.tick);
          setApplied({ alpha: result.alpha, beta: result.beta });
          setError(null);
        }
        onAppliedRef.current?.(result.tick);
      } catch (err) {
        if (mounted.current) setError(describeError(err));
      }
      if (queued.current) {
        pending = queued.current;
        queued.current = null;
      }
    }
    inFlight.current = false;
    if (mounted.current) setBusy(false);
  }, []);

  // Debounce: every change restarts the timer, so one drag sends one request.
  useEffect(() => {
    if (!touched.current) return undefined;
    const timer = window.setTimeout(() => {
      void send(alpha, beta);
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [alpha, beta, send]);

  const locked = disabled || busy;
  const atDefaults = alpha === DEFAULT_ALPHA && beta === DEFAULT_BETA;
  const unmet = tick
    ? Object.values(tick.unmet_kwh ?? {}).reduce((sum, value) => sum + (value || 0), 0)
    : 0;
  const settled = applied != null && applied.alpha === alpha && applied.beta === beta;
  const computedAtMs = tick ? Date.parse(tick.computed_at) : Number.NaN;

  const onAlpha = (event: { target: { value: string } }) => {
    touched.current = true;
    setAlpha(round(Number(event.target.value), 3));
  };
  const onBeta = (event: { target: { value: string } }) => {
    touched.current = true;
    setBeta(round(Number(event.target.value), 4));
  };
  const onReset = () => {
    touched.current = true;
    setAlpha(DEFAULT_ALPHA);
    setBeta(DEFAULT_BETA);
  };

  return (
    <div
      className="flex h-full w-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
      title={OBJECTIVE}
    >
      <header className="flex shrink-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <h2 className="text-sm font-semibold text-slate-900">Optimizer weights</h2>
        {tick ? (
          <p className="flex flex-wrap items-baseline gap-x-1.5 text-[11px] text-slate-500 tabular-nums">
            <span
              className={`rounded border px-1 py-px font-semibold ${
                STATUS_TONE[tick.status] ?? 'border-slate-300 bg-slate-50 text-slate-700'
              }`}
            >
              {tick.status}
            </span>
            <span>{tick.solve_ms.toFixed(0)} ms solve</span>
            <span>· {tick.n_sessions} sessions</span>
            {Number.isNaN(computedAtMs) ? null : <span>· {timeFmt.format(new Date(computedAtMs))}</span>}
          </p>
        ) : (
          <p className="text-[11px] text-slate-400">move a slider to re-plan</p>
        )}
      </header>

      <div className="mt-3 shrink-0 space-y-3">
        <div className="flex items-center gap-2">
          <label htmlFor="weight-alpha" className="w-20 shrink-0 text-xs text-slate-700">
            <span className="font-semibold">α</span> carbon
          </label>
          <input
            id="weight-alpha"
            type="range"
            min={0}
            max={1}
            step={ALPHA_STEP}
            value={alpha}
            onChange={onAlpha}
            disabled={locked}
            aria-label="Alpha — carbon weight"
            className="h-4 min-w-0 flex-1 accent-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
          />
          <span className="w-10 shrink-0 text-right font-mono text-xs text-slate-900 tabular-nums">
            {alpha.toFixed(2)}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="weight-beta" className="w-20 shrink-0 text-xs text-slate-700">
            <span className="font-semibold">β</span> cost
          </label>
          <input
            id="weight-beta"
            type="range"
            min={0}
            max={1}
            step={BETA_STEP}
            value={beta}
            onChange={onBeta}
            disabled={locked}
            aria-label="Beta — cost weight"
            className="h-4 min-w-0 flex-1 accent-sky-600 disabled:cursor-not-allowed disabled:opacity-50"
          />
          <span className="w-10 shrink-0 text-right font-mono text-xs text-slate-900 tabular-nums">
            {beta.toFixed(3)}
          </span>
        </div>
      </div>

      <div className="mt-auto flex shrink-0 flex-wrap items-baseline justify-between gap-x-3 gap-y-1 pt-2 text-[10px]">
        <span className="text-slate-500">
          α·CO₂ + β·₹ — raise β above α and the plan chases price, not carbon.
        </span>
        <span className="flex items-baseline gap-2">
          <span className="text-slate-500 tabular-nums">
            {busy ? (
              <span className="text-slate-700">applying…</span>
            ) : applied ? (
              settled ? (
                <>
                  applied α {applied.alpha.toFixed(2)} · β {applied.beta.toFixed(3)}
                  {unmet > 0.001 ? (
                    <span className="ml-1 text-amber-700">{' · '}{unmet.toFixed(1)} kWh unmet</span>
                  ) : null}
                </>
              ) : (
                <span className="text-slate-400">pending…</span>
              )
            ) : (
              <span className="text-slate-400">backend defaults in force</span>
            )}
          </span>
          <button
            type="button"
            onClick={onReset}
            disabled={locked || atDefaults}
            title={`Restore the optimizer defaults (α ${DEFAULT_ALPHA.toFixed(2)}, β ${DEFAULT_BETA.toFixed(
              3,
            )}); the β slider's ${BETA_STEP} step cannot reach 0.001 on its own.`}
            className="rounded border border-slate-300 px-1.5 py-px text-slate-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            defaults
          </button>
        </span>
      </div>

      {error ? (
        <p className="mt-1 shrink-0 rounded border border-red-200 bg-red-50 px-2 py-0.5 text-[11px] text-red-700">
          Weights not applied — {error}
        </p>
      ) : null}
    </div>
  );
}
